"""Executing a plan against the fake `fab`: idempotency, waits, dry-run, failures."""

import json
import pathlib
import shutil
import tempfile
import unittest

from fabricops.engine import Manifest, RunContext, build_plan, execute
from fabricops.fabric.cli import NO_RETRY, FabricCli
from fabricops.obs.logging import Level, RunLog
from fabricops.recipe import Recipe
from support import FakeFab

WS = "Demo - Store [dev]"


def make_recipe(layers: dict) -> Recipe:
    return Recipe(
        data={
            "display_name_pattern": "Demo - {layer} [{environment}]",
            "defaults": {"capacity": "Trial-01"},
            "layers": layers,
        },
        sources=(),
        environment="dev",
        solution="demo",
    )


class ExecutorTestCase(unittest.TestCase):
    def setUp(self):
        self.fab = FakeFab()
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="fabricops-engine-"))
        self.console = _Buffer()
        self.log = RunLog(level=Level.DEBUG, stream=self.console, _colour=False)
        self.cli = FabricCli(self.log, executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY)
        self.slept: list[float] = []

    def tearDown(self):
        self.log.close()
        self.fab.cleanup()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_plan(self, recipe, *, dry_run=False, destroy=False):
        cli = FabricCli(
            self.log, executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY, dry_run=dry_run
        )
        self.cli = cli
        manifest = Manifest(run_id="testrun", dry_run=dry_run)
        ctx = RunContext(cli=cli, log=self.log, recipe=recipe, dry_run=dry_run, sleep=self.slept.append)
        report = execute(build_plan(recipe), ctx, manifest=manifest, destroy=destroy)
        return report, manifest, ctx

    def commands(self):
        return self.fab.commands


class IdempotencyTests(ExecutorTestCase):
    def test_missing_workspace_is_created(self):
        self.fab.add(["exists"], stdout="false")
        self.fab.add(["mkdir"], stdout="created")
        self.fab.add(["get"], stdout="ws-id-1")

        report, manifest, ctx = self.run_plan(make_recipe({"Store": {}}))

        self.assertEqual(report.counts.get("created"), 1)
        self.assertEqual(ctx.workspace_id("Store"), "ws-id-1")
        self.assertTrue(any(command.startswith("mkdir") for command in self.commands()))

    def test_existing_workspace_is_left_alone(self):
        self.fab.add(["exists"], stdout="* true")
        self.fab.add(["get"], stdout="ws-id-1")

        report, _manifest, _ctx = self.run_plan(make_recipe({"Store": {}}))

        self.assertEqual(report.counts.get("existed"), 1)
        self.assertFalse(any(command.startswith("mkdir") for command in self.commands()))

    def test_properties_are_only_written_when_different(self):
        self.fab.add(["exists"], stdout="true")
        self.fab.add(["get", "-q", "sparkSettings.pool.starterPool.maxNodeCount"], stdout="1")
        self.fab.add(["get", "-q", "sparkSettings.pool.starterPool.maxExecutors"], stdout="9")
        self.fab.add(["get"], stdout="ws-id-1")
        self.fab.add(["set"], stdout="ok")

        recipe = make_recipe(
            {
                "Prepare": {
                    "properties": {
                        "sparkSettings.pool.starterPool.maxNodeCount": 1,   # already 1 -> no write
                        "sparkSettings.pool.starterPool.maxExecutors": 1,   # currently 9 -> write
                    }
                }
            }
        )
        report, manifest, _ctx = self.run_plan(recipe)

        writes = [command for command in self.commands() if command.startswith("set")]
        self.assertEqual(len(writes), 1)
        self.assertIn("maxExecutors", writes[0])
        properties_action = next(a for a in manifest.actions if a["kind"] == "properties")
        self.assertEqual(properties_action["status"], "updated")

    def test_item_creation_passes_the_creation_payload(self):
        self.fab.add(["exists"], stdout="false")
        self.fab.add(["mkdir"], stdout="created")
        self.fab.add_json(
            ["get", "Curated.Lakehouse"],
            {"id": "lh-1", "properties": {"sqlEndpointProperties": {"provisioningStatus": "Success"}}},
        )
        self.fab.add(["get"], stdout="ws-id-1")

        recipe = make_recipe(
            {"Store": {"items": [{"name": "Curated", "type": "Lakehouse", "creation_payload": {"enableSchemas": True}}]}}
        )
        self.run_plan(recipe)

        creation = [command for command in self.commands() if "Curated.Lakehouse" in command and command.startswith("mkdir")]
        self.assertEqual(len(creation), 1)
        self.assertIn("-P enableSchemas=true", creation[0])

    def test_skip_creation_items_are_skipped_when_absent(self):
        self.fab.add(["exists"], stdout="false")
        self.fab.add(["mkdir"], stdout="created")
        self.fab.add(["get"], stdout="ws-id-1")

        recipe = make_recipe({"Store": {"items": [{"name": "C", "type": "Lakehouse", "skip_creation": True}]}})
        report, manifest, _ctx = self.run_plan(recipe)

        item = next(action for action in manifest.actions if action["kind"] == "item")
        self.assertEqual(item["status"], "skipped")
        self.assertFalse(any("C.Lakehouse" in command and command.startswith("mkdir") for command in self.commands()))


class HandlerTests(ExecutorTestCase):
    def test_a_never_ready_item_gives_up_instead_of_spinning(self):
        self.fab.add(["exists"], stdout="false")
        self.fab.add(["mkdir"], stdout="created")
        self.fab.add_json(["get", "Stuck.Lakehouse"], {"id": "lh-1", "properties": {"sqlEndpointProperties": {"provisioningStatus": "InProgress"}}})
        self.fab.add(["get"], stdout="ws-id-1")

        recipe = make_recipe({"Store": {"items": [{"name": "Stuck", "type": "Lakehouse"}]}})
        from fabricops.engine.handlers import handler_for

        handler = handler_for("Lakehouse")
        original = handler.ready_timeout
        handler.ready_timeout = 6.0  # 3 attempts at the 2s interval
        try:
            report, _manifest, _ctx = self.run_plan(recipe)
        finally:
            handler.ready_timeout = original

        self.assertLessEqual(len(self.slept), 4)
        self.assertEqual(report.exit_code, 0, "a slow endpoint is a warning, not a failure")
        self.assertIn("timed out", self.console.text)


    def test_lakehouse_waits_for_its_sql_endpoint_then_exposes_it(self):
        self.fab.add(["exists"], stdout="false")
        self.fab.add(["mkdir"], stdout="created")
        self.fab.add_sequence(
            ["get", "Curated.Lakehouse"],
            [
                {"stdout": json.dumps({"id": "lh-1", "properties": {"sqlEndpointProperties": {"provisioningStatus": "InProgress"}}})},
                {
                    "stdout": json.dumps(
                        {
                            "id": "lh-1",
                            "properties": {
                                "sqlEndpointProperties": {
                                    "provisioningStatus": "Success",
                                    "connectionString": "abc.datawarehouse.fabric.microsoft.com",
                                    "id": "ep-1",
                                }
                            },
                        }
                    )
                },
            ],
        )
        self.fab.add(["get"], stdout="ws-id-1")

        recipe = make_recipe({"Store": {"items": [{"name": "Curated", "type": "Lakehouse"}]}})
        _report, _manifest, ctx = self.run_plan(recipe)

        self.assertEqual(len(self.slept), 1, "should poll once while InProgress")
        self.assertEqual(ctx.item_output("Store", "Curated", "Lakehouse", "sqlendpoint"), "abc.datawarehouse.fabric.microsoft.com")
        self.assertEqual(ctx.item_output("Store", "Curated", "Lakehouse", "sqlendpointid"), "ep-1")

    def test_sql_database_outputs_server_and_database(self):
        self.fab.add(["exists"], stdout="false")
        self.fab.add(["mkdir"], stdout="created")
        self.fab.add_json(
            ["get", "Metadata.SQLDatabase"],
            {"id": "db-1", "properties": {"serverFqdn": "srv.database.fabric.microsoft.com", "databaseName": "Metadata-abc"}},
        )
        self.fab.add(["get"], stdout="ws-id-1")

        recipe = make_recipe({"Core": {"items": [{"name": "Metadata", "type": "SQLDatabase"}]}})
        _report, _manifest, ctx = self.run_plan(recipe)

        self.assertEqual(ctx.item_output("Core", "Metadata", "SQLDatabase", "database_name"), "Metadata-abc")
        self.assertEqual(ctx.item_output("Core", "Metadata", "SQLDatabase", "sqlendpoint"), "srv.database.fabric.microsoft.com")

    def test_unknown_item_type_uses_the_default_handler(self):
        self.fab.add(["exists"], stdout="false")
        self.fab.add(["mkdir"], stdout="created")
        self.fab.add_json(["get", "Thing.SomeFutureType"], {"id": "x-1"})
        self.fab.add(["get"], stdout="ws-id-1")

        recipe = make_recipe({"Store": {"items": [{"name": "Thing", "type": "SomeFutureType"}]}})
        report, _manifest, ctx = self.run_plan(recipe)

        self.assertEqual(report.counts.get("created"), 2)  # workspace + item
        self.assertEqual(ctx.item_output("Store", "Thing", "SomeFutureType", "id"), "x-1")


class DryRunTests(ExecutorTestCase):
    def test_no_writes_reach_the_cli(self):
        self.fab.add(["exists"], stdout="false")
        recipe = make_recipe({"Store": {"items": [{"name": "C", "type": "Lakehouse"}]}})

        report, manifest, _ctx = self.run_plan(recipe, dry_run=True)

        self.assertEqual([c.split()[0] for c in self.commands()], ["exists", "exists"])
        self.assertEqual(report.counts.get("created"), 2)
        self.assertTrue(manifest.dry_run)
        self.assertEqual(len(self.cli.skipped_writes), 2)


class RunOutputTests(ExecutorTestCase):
    def test_each_line_carries_its_scope_and_no_layer_headers(self):
        self.fab.add(["exists"], stdout="false", command="exists")
        self.fab.add(["mkdir"], stdout="created", command="mkdir")
        self.fab.add(["get"], stdout="ws-1", command="get")

        self.run_plan(make_recipe({"Store": {}, "Model": {}}))
        text = self.console.text

        self.assertIn("Store \u00b7 Workspace", text)
        self.assertIn("Model \u00b7 Workspace", text)
        self.assertNotIn("[Store]", text, "a run log carries scope per line, not headers")
        self.assertNotIn("[Model]", text)

    def test_scopes_are_padded_to_a_common_width(self):
        self.fab.add(["exists"], stdout="false", command="exists")
        self.fab.add(["mkdir"], stdout="created", command="mkdir")
        self.fab.add(["get"], stdout="ws-1", command="get")

        self.run_plan(make_recipe({"Store": {}, "Orchestrate": {}}))
        prefixes = [
            line.split("\u00b7")[0]
            for line in self.console.text.splitlines()
            if "\u00b7" in line and "Workspace" in line
        ]
        self.assertGreaterEqual(len(prefixes), 2)
        self.assertEqual(len(set(len(prefix) for prefix in prefixes)), 1, "scope column is aligned")


class FailureTests(ExecutorTestCase):
    def test_a_failed_workspace_skips_its_items(self):
        self.fab.add(["exists"], stdout="false")
        self.fab.add(["mkdir", "Demo - Store [dev].Workspace"], stderr="ItemAlreadyExists", returncode=1)
        recipe = make_recipe({"Store": {"items": [{"name": "C", "type": "Lakehouse"}]}})

        report, manifest, _ctx = self.run_plan(recipe)

        self.assertEqual(report.exit_code, 1)
        statuses = {action["kind"]: action["status"] for action in manifest.actions}
        self.assertEqual(statuses["workspace"], "failed")
        self.assertEqual(statuses["item"], "skipped")
        self.assertIn("ItemAlreadyExists", manifest.failed[0]["message"])

    def test_a_missing_capacity_says_which_ones_exist(self):
        """The CLI names the capacity it could not find; the useful part is the alternatives."""
        self.fab.add(["exists"], stdout="false")
        self.fab.add(
            ["mkdir", "Demo - Store [dev].Workspace"],
            stdout="x mkdir: [NotFound] The Capacity 'Trial-01.Capacity' could not be found",
            returncode=1,
        )
        self.fab.add(["ls", ".capacities"], stdout="realcapacity.Capacity\nOther One.Capacity")

        report, manifest, _ctx = self.run_plan(make_recipe({"Store": {}}))

        message = manifest.failed[0]["message"]
        self.assertIn("capacity 'Trial-01' does not exist", message)
        self.assertIn("realcapacity", message)
        self.assertIn("Other One", message)

    def test_an_unrelated_failure_is_not_dressed_up_as_a_capacity_problem(self):
        self.fab.add(["exists"], stdout="false")
        self.fab.add(["mkdir"], stdout="x mkdir: [NotFound] The Workspace could not be found", returncode=1)
        _report, manifest, _ctx = self.run_plan(make_recipe({"Store": {}}))
        # The raw command carries `-P capacityname=...`, so assert on the enrichment itself
        # rather than on the word appearing anywhere.
        message = manifest.failed[0]["message"]
        self.assertNotIn("does not exist, or this identity cannot see it", message)
        self.assertIn("The Workspace could not be found", message)

    def test_one_layer_failing_does_not_stop_the_others(self):
        self.fab.add(["exists"], stdout="false")
        self.fab.add(["mkdir", "Demo - Store [dev].Workspace"], stderr="boom", returncode=1)
        self.fab.add(["mkdir"], stdout="created")
        self.fab.add(["get"], stdout="ws-id-2")

        report, manifest, _ctx = self.run_plan(make_recipe({"Store": {}, "Model": {}}))

        self.assertEqual(report.counts.get("failed"), 1)
        self.assertEqual(report.counts.get("created"), 1)
        self.assertEqual(len(report.failures), 1)


class TeardownTests(ExecutorTestCase):
    def test_workspaces_are_deleted_in_reverse_order(self):
        self.fab.add(["exists"], stdout="true")
        self.fab.add(["rm"], stdout="deleted")

        recipe = make_recipe({"Store": {"items": [{"name": "C", "type": "Lakehouse"}]}, "Model": {}})
        report, manifest, _ctx = self.run_plan(recipe, destroy=True)

        removals = [command for command in self.commands() if command.startswith("rm")]
        self.assertEqual(len(removals), 2)
        self.assertIn("Model", removals[0])
        self.assertIn("Store", removals[1])
        self.assertEqual(report.exit_code, 0)

    def test_deleting_something_absent_is_not_a_failure(self):
        self.fab.add(["exists"], stdout="false")
        report, manifest, _ctx = self.run_plan(make_recipe({"Store": {}}), destroy=True)
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(manifest.counts.get("skipped"), 1)


class ManifestTests(ExecutorTestCase):
    def test_manifest_records_actions_and_outputs(self):
        self.fab.add(["exists"], stdout="false")
        self.fab.add(["mkdir"], stdout="created")
        self.fab.add_json(["get", "Curated.Lakehouse"], {"id": "lh-1", "properties": {"sqlEndpointProperties": {"provisioningStatus": "Success", "connectionString": "abc", "id": "ep-1"}}})
        self.fab.add(["get"], stdout="ws-id-1")

        recipe = make_recipe({"Store": {"items": [{"name": "Curated", "type": "Lakehouse"}]}})
        _report, manifest, _ctx = self.run_plan(recipe)
        path = manifest.write(self.tmp)

        data = Manifest.read(path)
        self.assertEqual(data["run_id"], "testrun")
        self.assertEqual(data["counts"]["created"], 2)
        self.assertEqual(data["outputs"]["workspace:Store"]["id"], "ws-id-1")
        self.assertEqual(data["outputs"]["item:Store:Lakehouse:Curated"]["sqlendpoint"], "abc")
        self.assertEqual(Manifest.latest(self.tmp), path)


class _Buffer:
    def __init__(self):
        self.chunks: list[str] = []

    def write(self, text: str) -> int:
        self.chunks.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    @property
    def text(self) -> str:
        return "".join(self.chunks)


if __name__ == "__main__":
    unittest.main()


class TeardownReportingTests(ExecutorTestCase):
    """A teardown that reports "1 created" makes you go and check the portal."""

    def test_a_deletion_reports_as_deleted(self):
        self.fab.add(["exists"], stdout="true")
        self.fab.add(["rm"], stdout="")
        recipe = make_recipe({"Store": {}})

        _report, manifest, _ctx = self.run_plan(recipe, destroy=True)

        statuses = {action["status"] for action in manifest.actions}
        self.assertIn("deleted", statuses)
        self.assertNotIn("created", statuses)

    def test_deleting_something_absent_is_skipped(self):
        self.fab.add(["exists"], stdout="false")
        _report, manifest, _ctx = self.run_plan(make_recipe({"Store": {}}), destroy=True)
        self.assertEqual({a["status"] for a in manifest.actions}, {"skipped"})

    def test_a_dry_run_teardown_says_would_delete(self):
        from fabricops.engine.actions import ActionResult
        from fabricops.engine.executor import _report_result
        from fabricops.obs.logging import Level, RunLog

        lines: list[str] = []
        log = RunLog(level=Level.INFO)
        log.ok = lambda text="": lines.append(text)
        log.skip = lambda text="": lines.append(text)
        _report_result(log, ActionResult("deleted"), dry_run=True)
        self.assertIn("would delete", lines[0])


class AdvisoryWarningGroupingTests(unittest.TestCase):
    """One cause failing in every layer is one line, not one line per layer."""

    def group(self, warnings):
        from fabricops.engine.executor import _group_warnings

        return _group_warnings(warnings)

    def test_the_same_label_collapses_even_when_the_error_text_differs(self):
        # The error carries the workspace name, so the strings are never identical.
        grouped = self.group([
            ("Role admin for User 7a9ac92c", "Prepare", "acl set 'x (Prepare).Workspace' failed"),
            ("Role admin for User 7a9ac92c", "Ingest", "acl set 'x (Ingest).Workspace' failed"),
            ("Role admin for User 7a9ac92c", "Model", "acl set 'x (Model).Workspace' failed"),
        ])
        self.assertEqual(len(grouped), 1)
        message, layers = grouped[0]
        self.assertEqual(layers, ["Prepare", "Ingest", "Model"])
        self.assertIn("Role admin for User 7a9ac92c", message)
        self.assertIn("(Prepare)", message)      # the first error, kept verbatim

    def test_different_causes_stay_separate(self):
        grouped = self.group([
            ("Role admin for User a", "Prepare", "no such principal"),
            ("Tag 'Solution:Demo'", "Prepare", "needs a tenant admin"),
        ])
        self.assertEqual(len(grouped), 2)

    def test_a_single_warning_is_reported_once(self):
        grouped = self.group([("Role admin for User a", "Prepare", "boom")])
        self.assertEqual(grouped, [("Role admin for User a: boom", ["Prepare"])])

    def test_nothing_in_nothing_out(self):
        self.assertEqual(self.group([]), [])


class IncidentalActionTests(unittest.TestCase):
    """Actions kept only to satisfy a dependency should not dominate the log.

    `--only git` has to keep each layer's workspace and the source control connection,
    because the git action reads their ids - but a sync on every merge that lists seven
    workspaces reads as though it is about to provision them.
    """

    def test_an_incidental_action_is_flagged_by_the_filter(self):
        from fabricops.cli import _keep_with_dependencies
        from fabricops.engine.actions import Action

        plan = [
            Action(id="connection:git", kind="connection"),
            Action(id="workspace:Store", kind="workspace"),
            Action(id="git:Store", kind="git", depends_on=("workspace:Store", "connection:git")),
        ]
        kept = {action.id: action for action in _keep_with_dependencies(plan, {"git"})}
        self.assertFalse(kept["git:Store"].incidental)
        self.assertTrue(kept["workspace:Store"].incidental)
        self.assertTrue(kept["connection:git"].incidental)

    def test_an_unfiltered_action_is_never_incidental(self):
        from fabricops.engine.actions import Action

        self.assertFalse(Action(id="x", kind="git").incidental)
