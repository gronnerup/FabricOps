"""Release policy, parameter overlay and the fabric-cicd adapter (E09)."""

import json
import pathlib
import tempfile
import unittest

from fabricops.errors import ExitCode, RecipeError
from fabricops.fabric.cli import NO_RETRY, FabricCli
from fabricops.obs.logging import Level, RunLog
from fabricops.recipe import Recipe
from fabricops.release import parameters, policy, runner
from support import FakeFab

PATTERN = "Demo - {layer} [{environment}]"


def make_recipe(layers, *, defaults=None, environment="tst") -> Recipe:
    return Recipe(
        data={
            "display_name_pattern": PATTERN,
            "defaults": defaults or {},
            "layers": layers,
        },
        sources=(),
        solution="demo",
        environment=environment,
    )


# ------------------------------------------------------------------ deploy policy
class DeployPolicyTests(unittest.TestCase):
    def test_layer_overrides_defaults(self):
        loaded = make_recipe(
            {"Store": {"deploy": {"exclude_regex": "^tmp_"}}},
            defaults={"deploy": {"item_types_in_scope": ["Notebook"], "exclude_regex": "^never$"}},
        )
        resolved = policy.resolve(loaded, "Store", environment="tst")
        self.assertEqual(resolved.item_types_in_scope, ("Notebook",))
        self.assertEqual(resolved.exclude_regex, "^tmp_")

    def test_values_can_be_mapped_per_environment(self):
        loaded = make_recipe({"Store": {"deploy": {"unpublish": {"skip": {"prd": True}}}}})
        self.assertTrue(policy.resolve(loaded, "Store", environment="prd").unpublish_skip)
        self.assertFalse(policy.resolve(loaded, "Store", environment="tst").unpublish_skip)

    def test_all_environments_key_is_a_fallback(self):
        loaded = make_recipe({"Store": {"deploy": {"exclude_regex": {"_ALL_": "^x", "prd": "^y"}}}})
        self.assertEqual(policy.resolve(loaded, "Store", environment="tst").exclude_regex, "^x")
        self.assertEqual(policy.resolve(loaded, "Store", environment="prd").exclude_regex, "^y")

    def test_arguments_imply_the_flags_they_need(self):
        """Passing items_to_include without its flag is silently ignored by the library."""
        loaded = make_recipe({"Store": {"deploy": {"items_to_include": ["A.Notebook"]}}})
        resolved = policy.resolve(loaded, "Store")
        self.assertIn("enable_items_to_include", resolved.effective_features)
        self.assertNotIn("enable_items_to_include", resolved.features)

    def test_destructive_flags_are_never_implied(self):
        loaded = make_recipe({"Store": {"deploy": {"unpublish": {"items_to_include": ["Curated.Lakehouse"]}}}})
        resolved = policy.resolve(loaded, "Store")
        self.assertEqual(resolved.destructive_features, ())
        self.assertNotIn("enable_lakehouse_unpublish", resolved.effective_features)

    def test_destructive_flags_are_reported_when_asked_for(self):
        loaded = make_recipe({"Store": {"deploy": {"features": ["enable_lakehouse_unpublish"]}}})
        self.assertEqual(
            policy.resolve(loaded, "Store").destructive_features, ("enable_lakehouse_unpublish",)
        )

    def test_publish_arguments_omit_what_is_unset(self):
        loaded = make_recipe({"Store": {"deploy": {"exclude_regex": "^tmp_"}}})
        self.assertEqual(policy.resolve(loaded, "Store").publish_arguments(), {"item_name_exclude_regex": "^tmp_"})

    def test_unpublish_defaults_to_the_librarys_own_sentinel(self):
        loaded = make_recipe({"Store": {}})
        self.assertEqual(policy.resolve(loaded, "Store").unpublish_arguments(), {"item_name_exclude_regex": "^$"})

    def test_unpublish_must_be_a_mapping(self):
        loaded = make_recipe({"Store": {"deploy": {"unpublish": True}}})
        with self.assertRaises(RecipeError):
            policy.resolve(loaded, "Store")


# ------------------------------------------------------------------- layer order
class LayerOrderTests(unittest.TestCase):
    def test_declaration_order_is_kept_when_nothing_depends_on_anything(self):
        loaded = make_recipe({"Store": {}, "Ingest": {}, "Model": {}})
        self.assertEqual(policy.order(loaded), ["Store", "Ingest", "Model"])

    def test_a_report_publishes_after_the_model_it_binds(self):
        loaded = make_recipe({
            "Present": {"items": [
                {"name": "Sales", "type": "Report", "creation_payload": {"semanticModel": "SalesModel"}},
            ]},
            "Model": {"items": [{"name": "SalesModel", "type": "SemanticModel"}]},
        })
        self.assertEqual(policy.order(loaded), ["Model", "Present"])

    def test_selection_is_respected(self):
        loaded = make_recipe({"Store": {}, "Ingest": {}, "Model": {}})
        self.assertEqual(policy.order(loaded, ["Model", "Store"]), ["Store", "Model"])

    def test_unknown_layer_is_a_recipe_error(self):
        with self.assertRaises(RecipeError):
            policy.order(make_recipe({"Store": {}}), ["Nope"])


# -------------------------------------------------------------- parameter overlay
class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="fabricops-overlay-"))
        self.parameter_file = self.tmp / "parameter.yml"

    def test_declared_bindings_become_semantic_model_binding_entries(self):
        loaded = make_recipe({"Model": {"items": [
            {"name": "Sales", "type": "SemanticModel", "binding": {"connection": "Curated [tst]"}},
        ]}})
        overlay = parameters.render(
            loaded, environment="tst", resolve_connection=lambda name: "conn-1" if name == "Curated [tst]" else None
        )
        self.assertEqual(
            overlay.semantic_model_binding,
            {"models": [{"semantic_model_name": ["Sales"], "connection_id": {"tst": "conn-1"}}]},
        )

    def test_a_missing_connection_warns_rather_than_failing(self):
        loaded = make_recipe({"Model": {"items": [
            {"name": "Sales", "type": "SemanticModel", "binding": {"connection": "Gone"}},
        ]}})
        overlay = parameters.render(loaded, environment="tst", resolve_connection=lambda name: None)
        self.assertTrue(overlay.is_empty)
        self.assertIn("not found", overlay.warnings[0])

    def test_items_without_a_binding_are_left_alone(self):
        loaded = make_recipe({"Model": {"items": [{"name": "Sales", "type": "SemanticModel"}]}})
        overlay = parameters.render(loaded, environment="tst", resolve_connection=lambda name: "conn-1")
        self.assertTrue(overlay.is_empty)

    def test_an_empty_overlay_is_still_written(self):
        """A committed `extend:` pointing at a missing file fails the deployment."""
        overlay = parameters.Overlay(environment="tst")
        written = parameters.write(overlay, self.parameter_file)
        self.assertTrue(written.exists())
        self.assertIn("GENERATED", written.read_text())

    def test_the_committed_file_is_never_touched(self):
        self.parameter_file.write_text('extend:\n  - "./generated/dynamic.parameter.yml"\nfind_replace: []\n')
        before = self.parameter_file.read_text()
        parameters.write(parameters.Overlay(environment="tst"), self.parameter_file)
        self.assertEqual(self.parameter_file.read_text(), before)

    def test_a_missing_extend_entry_is_reported(self):
        self.parameter_file.write_text("find_replace: []\n")
        self.assertIn("extend", parameters.check_extends(self.parameter_file) or "")

    def test_a_present_extend_entry_is_silent(self):
        self.parameter_file.write_text('extend:\n  - "./generated/dynamic.parameter.yml"\n')
        self.assertIsNone(parameters.check_extends(self.parameter_file))

    def test_legacy_binding_file_is_translated_with_a_warning(self):
        loaded = make_recipe({
            "Store": {"items": [
                {"name": "Curated", "type": "Lakehouse", "connection": {"name": "Curated [tst]"}},
            ]},
            "Model": {"items": [{"name": "Sales", "type": "SemanticModel"}]},
        })
        legacy = self.tmp / parameters.LEGACY_BINDING_FILE
        legacy.write_text(
            "semantic_model_sqlendpoint_binding:\n"
            "  - lakehouse_name: Curated\n"
            "    lakehouse_layer: Store\n"
            "    semantic_models: [Sales]\n"
        )
        overlay = parameters.merge_legacy_bindings(
            parameters.Overlay(environment="tst"),
            loaded,
            legacy_path=legacy,
            resolve_connection=lambda name: "conn-9",
        )
        self.assertEqual(
            overlay.semantic_model_binding["models"],
            [{"semantic_model_name": ["Sales"], "connection_id": {"tst": "conn-9"}}],
        )
        self.assertIn("deprecated", overlay.warnings[0])

    def test_a_recipe_binding_wins_over_the_legacy_file(self):
        loaded = make_recipe({
            "Store": {"items": [
                {"name": "Curated", "type": "Lakehouse", "connection": {"name": "Curated [tst]"}},
            ]},
            "Model": {"items": [
                {"name": "Sales", "type": "SemanticModel", "binding": {"connection": "Explicit"}},
            ]},
        })
        legacy = self.tmp / parameters.LEGACY_BINDING_FILE
        legacy.write_text(
            "semantic_model_sqlendpoint_binding:\n"
            "  - lakehouse_name: Curated\n"
            "    lakehouse_layer: Store\n"
            "    semantic_models: [Sales]\n"
        )
        overlay = parameters.render(loaded, environment="tst", resolve_connection=lambda name: f"id-{name}")
        overlay = parameters.merge_legacy_bindings(
            overlay, loaded, legacy_path=legacy, resolve_connection=lambda name: f"id-{name}"
        )
        bound = [entry for entry in overlay.semantic_model_binding["models"] if "Sales" in entry["semantic_model_name"]]
        self.assertEqual(len(bound), 1)
        self.assertEqual(bound[0]["connection_id"]["tst"], "id-Explicit")


# ----------------------------------------------------------- cross-layer id carry
class AccumulationTests(unittest.TestCase):
    class FakeWorkspace:
        def __init__(self, mapping):
            self.repository_items = {
                "Notebook": {
                    name: type("Details", (), {"logical_id": logical, "guid": guid})()
                    for name, (logical, guid) in mapping.items()
                }
            }

    def test_published_ids_are_carried_to_the_next_layer(self):
        accumulated = {}
        runner._accumulate(accumulated, self.FakeWorkspace({"Ingest": ("logical-1", "guid-1")}), "tst")
        self.assertEqual(
            accumulated["find_replace"],
            [{"find_value": "logical-1", "replace_value": {"tst": "guid-1"}}],
        )

    def test_the_same_id_is_not_added_twice(self):
        accumulated = {}
        workspace = self.FakeWorkspace({"Ingest": ("logical-1", "guid-1")})
        runner._accumulate(accumulated, workspace, "tst")
        runner._accumulate(accumulated, workspace, "tst")
        self.assertEqual(len(accumulated["find_replace"]), 1)

    def test_accumulated_entries_are_appended_to_a_layers_own(self):
        merged = runner._merged_parameters(
            {"find_replace": [{"find_value": "own"}]},
            {"find_replace": [{"find_value": "carried"}]},
        )
        self.assertEqual([entry["find_value"] for entry in merged["find_replace"]], ["own", "carried"])

    def test_merging_does_not_mutate_the_layers_own_parameters(self):
        own = {"find_replace": [{"find_value": "own"}]}
        runner._merged_parameters(own, {"find_replace": [{"find_value": "carried"}]})
        self.assertEqual(len(own["find_replace"]), 1)


# ------------------------------------------------------------------- release run
class ReleaseRunTests(unittest.TestCase):
    def setUp(self):
        self.fab = FakeFab()
        self.addCleanup(self.fab.cleanup)
        self.log = RunLog(level=Level.OFF)
        self.cli = FabricCli(
            self.log, executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY, dry_run=True
        )

    def test_a_dry_run_plans_every_layer_and_writes_nothing(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="fabricops-release-"))
        loaded = make_recipe({
            "Store": {"git": {"directory": "solution/store"}},
            "Model": {"git": {"directory": "solution/analytics/model"}},
        })
        options = runner.ReleaseOptions(
            environment="tst", repository_root=tmp, parameter_file=pathlib.Path("parameter.yml"), dry_run=True
        )
        result = runner.run(loaded, options, cli=self.cli, log=self.log)
        self.assertEqual(result.status, runner.Status.PLANNED)
        self.assertEqual(result.exit_code, ExitCode.SUCCESS)
        self.assertEqual([layer.layer for layer in result.layers], ["Store", "Model"])
        self.assertFalse((tmp / "generated").exists())

    def test_the_repository_directory_comes_from_the_git_block(self):
        loaded = make_recipe({"Store": {"git": {"directory": "solution/store"}}})
        directory = runner.repository_directory(loaded, "Store", pathlib.Path("/repo"))
        self.assertEqual(directory, pathlib.Path("/repo/solution/store"))

    def test_a_layer_without_a_directory_is_an_error_not_a_guess(self):
        # There used to be a fallback to solution/<layer>. It matched the flat layout and
        # nothing else, and a directory guessed wrong is a release that deploys nothing.
        loaded = make_recipe({"Store": {}})
        with self.assertRaises(RecipeError) as raised:
            runner.repository_directory(loaded, "Store", pathlib.Path("/repo"))
        self.assertIn("Store", str(raised.exception))
        self.assertIn("git_directoryName", str(raised.exception))

    def test_a_nested_directory_is_used_verbatim(self):
        loaded = make_recipe({"Prepare": {"git": {"directory": "solution/engineering/prepare"}}})
        directory = runner.repository_directory(loaded, "Prepare", pathlib.Path("/repo"))
        self.assertEqual(directory, pathlib.Path("/repo/solution/engineering/prepare"))

    def test_a_failed_layer_stops_the_release_and_sets_the_exit_code(self):
        result = runner.ReleaseResult(environment="tst")
        result.layers.append(runner.LayerResult("Store", "ws", runner.Status.COMPLETED))
        result.layers.append(runner.LayerResult("Model", "ws", runner.Status.FAILED, message="boom"))
        self.assertEqual(result.status, runner.Status.FAILED)
        self.assertEqual(result.exit_code, ExitCode.ACTION_FAILED)
        self.assertIn("Model", result.message)

    def test_a_skipped_layer_does_not_fail_the_release(self):
        result = runner.ReleaseResult(environment="tst")
        result.layers.append(runner.LayerResult("Store", "ws", runner.Status.SKIPPED))
        self.assertEqual(result.status, runner.Status.COMPLETED)
        self.assertEqual(result.exit_code, ExitCode.SUCCESS)

    def test_auth_failures_are_recognised_by_message(self):
        self.assertTrue(runner._is_auth_error(Exception("401 Unauthorized")))
        self.assertTrue(runner._is_auth_error(Exception("the credential was rejected")))
        self.assertFalse(runner._is_auth_error(Exception("item definition invalid")))


# ------------------------------------------------------- connections from items
class ItemConnectionTests(unittest.TestCase):
    """A connection whose source item the repository owns, not the recipe (E03/E09)."""

    def setUp(self):
        self.fab = FakeFab()
        self.addCleanup(self.fab.cleanup)
        self.log = RunLog(level=Level.OFF)

    def cli(self, dry_run=False):
        return FabricCli(
            self.log, executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY, dry_run=dry_run
        )

    def recipe_with_connection(self):
        return Recipe(
            data={
                "display_name_pattern": PATTERN,
                "defaults": {"connections": [{
                    "name": "Demo-Curated [tst]",
                    "type": "SQL",
                    "from_item": {"layer": "Store", "name": "Curated", "type": "Lakehouse"},
                }]},
                "layers": {"Store": {}},
            },
            sources=(),
            solution="demo",
            environment="tst",
        )

    def test_the_connection_is_planned_without_declaring_the_item(self):
        from fabricops.engine import build_plan
        from fabricops.engine.connections import CreateConnection

        plan = build_plan(self.recipe_with_connection())
        connections = [a for a in plan if isinstance(a, CreateConnection) and a.source_item]
        self.assertEqual(len(connections), 1)
        self.assertEqual(connections[0].source_item["workspace"], "Demo - Store [tst]")
        self.assertEqual(connections[0].depends_on, ("workspace:Store",))
        self.assertFalse([a for a in plan if a.kind == "item"], "the recipe declares no items")

    def test_a_missing_item_makes_setup_wait_rather_than_fail(self):
        from fabricops.engine import RunContext, build_plan
        from fabricops.engine.connections import CreateConnection

        self.fab.add(["exists"], stdout="false", command="exists")
        action = next(a for a in build_plan(self.recipe_with_connection()) if isinstance(a, CreateConnection))
        outcome = action.apply(
            RunContext(cli=self.cli(), log=self.log, recipe=self.recipe_with_connection(), dry_run=False)
        )
        self.assertEqual(outcome.status, "skipped")
        self.assertIn("fabricops release", outcome.message)
        self.assertFalse([c for c in self.fab.commands if c.startswith("mkdir")])

    def test_a_dry_run_reports_the_same_reason_as_a_real_run(self):
        """It was saying 'needs credentials' for a connection whose problem was a missing item."""
        from fabricops.engine import RunContext, build_plan
        from fabricops.engine.connections import CreateConnection

        self.fab.add(["exists"], stdout="false", command="exists")
        loaded = self.recipe_with_connection()
        action = next(a for a in build_plan(loaded) if isinstance(a, CreateConnection) and a.source_item)

        wet = action.apply(RunContext(cli=self.cli(), log=self.log, recipe=loaded, dry_run=False))
        dry = action.apply(RunContext(cli=self.cli(dry_run=True), log=self.log, recipe=loaded, dry_run=True))

        self.assertEqual(wet.status, dry.status)
        for outcome in (wet, dry):
            self.assertIn("Curated", outcome.message)
            self.assertNotIn("credentials", outcome.message)

    def test_an_unknown_layer_in_from_item_is_a_recipe_error(self):
        from fabricops.engine import build_plan

        loaded = self.recipe_with_connection()
        loaded.data["defaults"]["connections"][0]["from_item"]["layer"] = "Nope"
        with self.assertRaises(RecipeError):
            build_plan(loaded)

    def test_the_sync_pass_reports_what_is_still_waiting(self):
        self.fab.add(["exists"], stdout="false", command="exists")
        summary = runner.sync_item_connections(self.recipe_with_connection(), cli=self.cli(), log=self.log)
        self.assertEqual(summary.created, [])
        self.assertEqual(len(summary.waiting), 1)

    def git_connected_recipe(self):
        loaded = self.recipe_with_connection()
        loaded.data["defaults"]["git"] = {
            "provider": "GitHub", "owner": "o", "repository": "r", "branch": "main",
            "credentials": {"connection": "Creds"},
        }
        loaded.data["layers"]["Store"] = {"git": {"directory": "solution/store"}}
        return loaded

    def test_a_git_connected_layer_waits_for_the_sync(self):
        """In dev the lakehouse arrives with the git sync, not from release."""
        from fabricops.engine import build_plan
        from fabricops.engine.connections import CreateConnection

        plan = build_plan(self.git_connected_recipe())
        connection = next(a for a in plan if isinstance(a, CreateConnection) and a.source_item)
        self.assertIn("git:Store", connection.depends_on)
        ids = [a.id for a in plan]
        self.assertLess(ids.index("git:Store"), ids.index(connection.id))

    def test_a_layer_without_git_only_waits_for_its_workspace(self):
        from fabricops.engine import build_plan
        from fabricops.engine.connections import CreateConnection

        plan = build_plan(self.recipe_with_connection())
        connection = next(a for a in plan if isinstance(a, CreateConnection) and a.source_item)
        self.assertEqual(connection.depends_on, ("workspace:Store",))

    def test_the_endpoint_is_waited_for_rather_than_read_once(self):
        """A lakehouse fresh from a git sync still has an endpoint provisioning."""
        from fabricops.engine import RunContext, build_plan
        from fabricops.engine.connections import CreateConnection

        self.fab.add(["exists"], stdout="true", command="exists")
        self.fab.add_sequence(["get"], [
            {"stdout": json.dumps({"id": "lh-1", "displayName": "Curated", "properties": {
                "sqlEndpointProperties": {"provisioningStatus": "InProgress"}}})},
            {"stdout": json.dumps({"id": "lh-1", "displayName": "Curated", "properties": {
                "sqlEndpointProperties": {
                    "provisioningStatus": "Success", "connectionString": "endpoint.fabric.microsoft.com",
                    "id": "ep-1"}}})},
        ])
        action = next(
            a for a in build_plan(self.recipe_with_connection()) if isinstance(a, CreateConnection) and a.source_item
        )
        slept: list[float] = []
        ctx = RunContext(
            cli=self.cli(), log=self.log, recipe=self.recipe_with_connection(),
            dry_run=False, sleep=slept.append,
        )
        server, database = action._resolve_from_item(ctx)
        self.assertEqual(server, "endpoint.fabric.microsoft.com")
        self.assertEqual(database, "Curated")
        self.assertTrue(slept, "it must poll rather than accept the first InProgress read")

    def test_the_sync_pass_receives_credentials(self):
        """Without them every connection fails with "needs service principal credentials",
        which reads as a missing credentials file rather than an argument never passed -
        and it fails identically in CI, where the credentials come from environment
        variables."""
        from fabricops.engine.connections import Credentials
        from fabricops.obs.redaction import Secret

        self.fab.add([".Connection"], stdout="false", command="exists")
        self.fab.add(["exists"], stdout="true", command="exists")
        self.fab.add_json(["get"], {
            "id": "lh-1", "displayName": "Curated",
            "properties": {"sqlEndpointProperties": {
                "provisioningStatus": "Success",
                "connectionString": "server.datawarehouse.fabric.microsoft.com",
            }},
        }, command="get")

        credentials = Credentials(
            tenant_id="t-1", client_id="c-1", client_secret=Secret("s-1", "client_secret")
        )
        summary = runner.sync_item_connections(
            self.recipe_with_connection(), cli=self.cli(), log=self.log,
            credentials=credentials, sleep=lambda _: None,
        )
        self.assertEqual(summary.waiting, [], "credentials were not threaded through")

    def test_the_sync_pass_without_credentials_says_so(self):
        self.fab.add([".Connection"], stdout="false", command="exists")
        self.fab.add(["exists"], stdout="true", command="exists")
        self.fab.add_json(["get"], {
            "id": "lh-1", "displayName": "Curated",
            "properties": {"sqlEndpointProperties": {
                "provisioningStatus": "Success",
                "connectionString": "server.datawarehouse.fabric.microsoft.com",
            }},
        }, command="get")

        summary = runner.sync_item_connections(
            self.recipe_with_connection(), cli=self.cli(), log=self.log, sleep=lambda _: None
        )
        self.assertTrue(any("credentials" in w for w in summary.waiting))

    def test_the_sync_pass_is_a_no_op_without_item_connections(self):
        loaded = make_recipe({"Store": {}})
        summary = runner.sync_item_connections(loaded, cli=self.cli(), log=self.log)
        self.assertEqual((summary.created, summary.existed, summary.waiting), ([], [], []))


if __name__ == "__main__":
    unittest.main()


class EmptyOverlayTests(unittest.TestCase):
    """An overlay of comments alone makes fabric-cicd warn on every release."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="fabricops-overlay-empty-"))
        self.parameter_file = self.tmp / "parameter.yml"

    def test_an_empty_overlay_is_a_valid_document(self):
        written = parameters.write(parameters.Overlay(environment="tst"), self.parameter_file)
        import yaml

        document = yaml.safe_load(written.read_text())
        self.assertEqual(document, {"find_replace": []})

    def test_a_populated_overlay_keeps_its_content(self):
        overlay = parameters.Overlay(
            environment="tst",
            semantic_model_binding={"models": [{"semantic_model_name": ["M"], "connection_id": {"tst": "c"}}]},
        )
        written = parameters.write(overlay, self.parameter_file)
        import yaml

        document = yaml.safe_load(written.read_text())
        self.assertIn("semantic_model_binding", document)
        self.assertNotIn("find_replace", document)


class ParameterFileShapeTests(unittest.TestCase):
    """Rules fabric-cicd enforces that are easy to get wrong in YAML."""

    def test_is_regex_is_never_a_yaml_boolean(self):
        """fabric-cicd validates is_regex as a string; a bool fails the whole file."""
        import yaml

        path = pathlib.Path(__file__).resolve().parents[2] / "automation/resources/parameters/parameter.yml"
        for rule in yaml.safe_load(path.read_text())["find_replace"]:
            if "is_regex" in rule:
                with self.subTest(find_value=rule["find_value"]):
                    self.assertIsInstance(rule["is_regex"], str)
