"""Feature storage: schema references, teardown, and the report (E07)."""

import json
import unittest

from fabricops.engine import Manifest, RunContext, execute
from fabricops.engine.feature import build_feature_plan
from fabricops.engine.storage import (
    DropFeatureSchema,
    LakehouseRef,
    configured_lakehouses,
    list_feature_schemas,
    managed_prefix,
    schema_path,
)
from fabricops.errors import RecipeError
from fabricops.fabric.cli import NO_RETRY, FabricCli
from fabricops.obs.logging import Level, RunLog
from fabricops.recipe import Recipe
from support import FakeFab

BASE_PATTERN = "Demo - {layer} [{environment}]"


def platform_recipe(**feature_schema) -> Recipe:
    storage = {"default_schema": "dbo"}
    if feature_schema:
        storage["feature_schema"] = feature_schema
    return Recipe(
        data={
            "display_name_pattern": BASE_PATTERN,
            "storage": storage,
            "layers": {"Store": {"items": [{"name": "Curated", "type": "Lakehouse"}]}},
        },
        sources=(),
        environment="dev",
        solution="demo",
    )


def feature_recipe() -> Recipe:
    return Recipe(
        data={
            "kind": "Feature",
            "display_name_pattern": "*{feature} ({layer})",
            "layers": {"Prepare": {}},
            "base_display_name_pattern": BASE_PATTERN,
        },
        sources=(),
        kind="Feature",
        solution="demo",
    )


class ReferenceTests(unittest.TestCase):
    def test_parsing(self):
        reference = LakehouseRef.parse("Store/Curated")
        self.assertEqual((reference.layer, reference.item), ("Store", "Curated"))
        self.assertEqual(str(reference), "Store/Curated")

    def test_a_malformed_reference_says_what_it_wants(self):
        with self.assertRaises(RecipeError) as ctx:
            LakehouseRef.parse("Curated")
        self.assertIn("<layer>/<item>", str(ctx.exception))

    def test_schema_is_a_directory_under_tables(self):
        self.assertEqual(
            str(schema_path("Demo - Store [dev]", "Curated", "dev_add_orders")),
            "Demo - Store [dev].Workspace/Curated.Lakehouse/Tables/dev_add_orders",
        )

    def test_managed_prefix_is_the_literal_head(self):
        self.assertEqual(managed_prefix("dev_{feature}"), "dev_")
        self.assertEqual(managed_prefix("feat_{developer}_x"), "feat_")

    def test_configured_lakehouses(self):
        recipe = platform_recipe(enabled=True, lakehouses=["Store/Curated", "Store/Base"])
        self.assertEqual([str(r) for r in configured_lakehouses(recipe)], ["Store/Curated", "Store/Base"])


class PlanTests(unittest.TestCase):
    def plan(self, **feature_schema):
        return build_feature_plan(
            feature_recipe(),
            branch="feature/prepare/add-orders",
            developer="peer",
            base_pattern=BASE_PATTERN,
            storage_recipe=platform_recipe(**feature_schema),
        )

    def test_no_teardown_action_when_the_flag_is_off(self):
        self.assertEqual(self.plan(enabled=False, lakehouses=["Store/Curated"]).of_kind("storage"), [])

    def test_no_teardown_action_when_drop_is_off(self):
        plan = self.plan(enabled=True, drop_on_teardown=False, lakehouses=["Store/Curated"])
        self.assertEqual(plan.of_kind("storage"), [])

    def test_one_action_per_configured_lakehouse(self):
        plan = self.plan(
            enabled=True, drop_on_teardown=True, pattern="dev_{feature}", lakehouses=["Store/Curated", "Store/Base"]
        )
        actions = plan.of_kind("storage")
        self.assertEqual([a.lakehouse for a in actions], ["Curated", "Base"])
        self.assertEqual({a.schema for a in actions}, {"dev_add_orders"})
        self.assertEqual(actions[0].workspace, "Demo - Store [dev]", "targets shared dev, not the feature workspace")


class TeardownTests(unittest.TestCase):
    def setUp(self):
        self.fab = FakeFab()
        self.log = RunLog(level=Level.OFF, stream=_Null(), _colour=False)

    def tearDown(self):
        self.fab.cleanup()

    def run_teardown(self, *, exists=True, dry_run=False):
        self.fab.add(["exists"], stdout="true" if exists else "false", command="exists")
        self.fab.add(["rm"], stdout="deleted", command="rm")
        self.fab.add(["get"], stdout="ws-1", command="get")

        recipe = feature_recipe()
        cli = FabricCli(self.log, executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY, dry_run=dry_run)
        ctx = RunContext(cli=cli, log=self.log, recipe=recipe, dry_run=dry_run, sleep=lambda _s: None)
        plan = build_feature_plan(
            recipe,
            branch="feature/prepare/add-orders",
            developer="peer",
            base_pattern=BASE_PATTERN,
            storage_recipe=platform_recipe(
                enabled=True, drop_on_teardown=True, pattern="dev_{feature}", lakehouses=["Store/Curated"]
            ),
        )
        report = execute(plan, ctx, manifest=Manifest(run_id="t"), destroy=True)
        return report

    def test_the_schema_directory_is_removed_from_shared_storage(self):
        self.run_teardown()
        removals = [c for c in self.fab.commands if c.startswith("rm")]
        schema_removals = [c for c in removals if "Tables/dev_add_orders" in c]
        self.assertEqual(len(schema_removals), 1)
        self.assertIn("Demo - Store [dev].Workspace/Curated.Lakehouse", schema_removals[0])

    def test_an_absent_schema_is_a_skip(self):
        report = self.run_teardown(exists=False)
        storage_action = next(a for a in report.manifest.actions if a["kind"] == "storage")
        self.assertEqual(storage_action["status"], "skipped")
        self.assertIn("does not exist", storage_action["message"])

    def test_dry_run_drops_nothing(self):
        self.run_teardown(dry_run=True)
        self.assertFalse([c for c in self.fab.commands if c.startswith("rm")])

    def test_apply_never_creates_a_schema(self):
        """Creating it is the notebook's job on first write; the engine only cleans up."""
        action = DropFeatureSchema(id="x", workspace="W", lakehouse="L", schema="dev_x")
        cli = FabricCli(self.log, executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY)
        ctx = RunContext(cli=cli, log=self.log, recipe=feature_recipe(), sleep=lambda _s: None)
        self.assertEqual(action.apply(ctx).status, "skipped")
        self.assertEqual(self.fab.commands, [])


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.fab = FakeFab()
        self.log = RunLog(level=Level.OFF, stream=_Null(), _colour=False)

    def tearDown(self):
        self.fab.cleanup()

    def context(self):
        cli = FabricCli(self.log, executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY)
        return RunContext(cli=cli, log=self.log, recipe=platform_recipe(), sleep=lambda _s: None)

    def test_only_managed_schemas_are_listed(self):
        self.fab.add(["ls"], stdout="dbo\ndev_add_orders\ndev_fix_totals\nfinance\n", command="ls")
        found = list_feature_schemas(self.context(), "Demo - Store [dev]", "Curated", "dev_")
        self.assertEqual(found, ["dev_add_orders", "dev_fix_totals"])

    def test_a_failed_listing_is_empty_not_an_error(self):
        self.fab.add(["ls"], stderr="not found", returncode=1, command="ls")
        self.assertEqual(list_feature_schemas(self.context(), "W", "L", "dev_"), [])


class _Null:
    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
