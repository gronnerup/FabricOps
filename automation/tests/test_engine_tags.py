"""Tags: registry, budgets, idempotent apply, and the admin-only sync."""

import json
import pathlib
import shutil
import tempfile
import unittest

from fabricops.engine import Manifest, RunContext, build_plan, execute
from fabricops.engine.tags import (
    MANAGED_KEYS,
    MAX_TAGS_PER_OBJECT,
    TagRegistry,
    is_managed,
    sync_registry,
    validate_tags,
)
from fabricops.errors import FabricOpsError, RecipeError
from fabricops.fabric.cli import NO_RETRY, FabricCli
from fabricops.obs.logging import Level, RunLog
from fabricops.recipe import Recipe
from support import FakeFab


class LimitTests(unittest.TestCase):
    def test_ten_tags_is_the_ceiling(self):
        tags = [f"Layer:L{index}" for index in range(MAX_TAGS_PER_OBJECT)]
        self.assertEqual(len(validate_tags(tags, where="test")), MAX_TAGS_PER_OBJECT)
        with self.assertRaises(RecipeError) as ctx:
            validate_tags(tags + ["Env:dev"], where="layers.Store")
        self.assertIn("at most 10", str(ctx.exception))

    def test_forty_characters_is_the_ceiling(self):
        with self.assertRaises(RecipeError) as ctx:
            validate_tags(["Branch:" + "x" * 40], where="test")
        self.assertIn("the limit is 40", str(ctx.exception))

    def test_duplicates_are_collapsed(self):
        self.assertEqual(validate_tags(["Env:dev", "Env:dev"], where="test"), ["Env:dev"])

    def test_only_managed_keys_are_ours(self):
        self.assertTrue(all(is_managed(f"{key}:x") for key in MANAGED_KEYS))
        self.assertFalse(is_managed("Finance 2026"))
        self.assertFalse(is_managed("SomeoneElse:value"))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="fabricops-tags-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_round_trip(self):
        registry = TagRegistry(tags={"Env:dev": "id-1", "ManagedBy:FabricOps": "id-2"})
        path = registry.save(self.root / "tags.yml")
        reloaded = TagRegistry.load(path)
        self.assertEqual(reloaded.tags, registry.tags)
        self.assertEqual(reloaded.scope, {"type": "Tenant"})

    def test_resolve_reports_what_is_missing_with_a_hint(self):
        registry = TagRegistry(tags={"Env:dev": "id-1"})
        with self.assertRaises(FabricOpsError) as ctx:
            registry.resolve(["Env:dev", "Layer:Store"])
        self.assertIn("Layer:Store", str(ctx.exception))
        self.assertIn("tags sync", ctx.exception.hint or "")

    def test_discovery_prefers_the_solution_registry(self):
        resources = self.root / "resources"
        (resources / "solutions" / "spaceparts").mkdir(parents=True)
        (resources / "tags").mkdir(parents=True)
        TagRegistry(tags={"Env:dev": "solution-id"}).save(resources / "solutions" / "spaceparts" / "tags.yml")
        TagRegistry(tags={"Env:dev": "shared-id"}).save(resources / "tags" / "registry.yml")

        self.assertEqual(TagRegistry.discover(resources, "spaceparts").tags["Env:dev"], "solution-id")
        self.assertEqual(TagRegistry.discover(resources, None).tags["Env:dev"], "shared-id")

    def test_a_missing_registry_is_empty_not_an_error(self):
        self.assertEqual(TagRegistry.discover(self.root / "nothing", None).tags, {})


class ApplyTagsTests(unittest.TestCase):
    def setUp(self):
        self.fab = FakeFab()
        self.log = RunLog(level=Level.OFF, stream=_Null(), _colour=False)
        self.registry = TagRegistry(
            tags={
                "ManagedBy:FabricOps": "id-managed",
                "Solution:demo": "id-solution",
                "Env:dev": "id-env",
                "Layer:Store": "id-layer",
                "Zone:Curated": "id-zone",
            }
        )

    def tearDown(self):
        self.fab.cleanup()

    def run_plan(self, *, defaults_tags=None, layer_tags=None, item_tags=None, dry_run=False, reconcile=False):
        layers = {"Store": {}}
        if layer_tags:
            layers["Store"]["tags"] = layer_tags
        if item_tags is not None:
            layers["Store"]["items"] = [{"name": "Curated", "type": "Lakehouse", "tags": item_tags}]
        recipe = Recipe(
            data={
                "display_name_pattern": "Demo - {layer} [{environment}]",
                "defaults": {"capacity": "Trial-01", "tags": defaults_tags or []},
                "layers": layers,
            },
            sources=(),
            environment="dev",
            solution="demo",
        )
        cli = FabricCli(self.log, executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY, dry_run=dry_run)
        ctx = RunContext(
            cli=cli, log=self.log, recipe=recipe, dry_run=dry_run, tag_registry=self.registry, sleep=lambda _s: None
        )
        plan = build_plan(recipe)
        for action in plan:
            if action.kind == "tags":
                action.reconcile = reconcile
        report = execute(plan, ctx, manifest=Manifest(run_id="t"))
        return report, ctx

    def base(self, current_tags=None):
        self.fab.add(["exists"], stdout="false", command="exists")
        self.fab.add(["mkdir"], stdout="created", command="mkdir")
        self.fab.add(["get"], stdout="ws-1", command="get")
        self.fab.add_json(["applyTags"], {"status_code": 200, "text": {}, "headers": {}})
        self.fab.add_json(["unapplyTags"], {"status_code": 200, "text": {}, "headers": {}})
        self.fab.add_json(
            ["api", "workspaces/ws-1"],
            {"status_code": 200, "text": {"id": "ws-1", "tags": current_tags or []}, "headers": {}},
        )

    def test_workspace_tags_are_applied_by_id(self):
        self.base()
        report, _ctx = self.run_plan(defaults_tags=["ManagedBy:FabricOps", "Solution:demo"], layer_tags=["Layer:Store"])

        call = next(c for c in self.fab.commands if "applyTags" in c)
        self.assertIn("workspaces/ws-1/applyTags", call)
        body = json.loads(call.split("-i ", 1)[1].split(" --show_headers")[0])
        self.assertEqual(set(body["tags"]), {"id-managed", "id-solution", "id-layer"})

    def test_tags_are_unioned_from_defaults_layer_and_item(self):
        # Registered before base(), because the first matching rule wins and base()
        # installs a catch-all `get`.
        self.fab.add_json(
            ["get", "Curated.Lakehouse"],
            {"id": "lh-1", "properties": {"sqlEndpointProperties": {"provisioningStatus": "Success"}}},
            command="get",
        )
        self.fab.add_json(
            ["items/lh-1"], {"status_code": 200, "text": {"id": "lh-1", "tags": []}, "headers": {}}
        )
        self.base()
        report, _ctx = self.run_plan(
            defaults_tags=["ManagedBy:FabricOps"], layer_tags=["Layer:Store"], item_tags=["Zone:Curated"]
        )
        item_call = next(c for c in self.fab.commands if "items/" in c and "applyTags" in c)
        body = json.loads(item_call.split("-i ", 1)[1].split(" --show_headers")[0])
        self.assertEqual(set(body["tags"]), {"id-managed", "id-layer", "id-zone"})

    def test_already_applied_tags_are_not_reapplied(self):
        self.base(current_tags=[{"id": "id-managed", "displayName": "ManagedBy:FabricOps"}])
        report, _ctx = self.run_plan(defaults_tags=["ManagedBy:FabricOps"])
        self.assertFalse([c for c in self.fab.commands if "applyTags" in c])
        tags_action = next(a for a in report.manifest.actions if a["kind"] == "tags")
        self.assertEqual(tags_action["status"], "existed")

    def test_reconcile_removes_a_managed_tag_that_is_no_longer_declared(self):
        self.base(current_tags=[
            {"id": "id-env", "displayName": "Env:dev"},
            {"id": "id-foreign", "displayName": "Finance 2026"},
        ])
        report, _ctx = self.run_plan(defaults_tags=["ManagedBy:FabricOps"], reconcile=True)

        unapply = next(c for c in self.fab.commands if "unapplyTags" in c)
        body = json.loads(unapply.split("-i ", 1)[1].split(" --show_headers")[0])
        self.assertEqual(body["tags"], ["id-env"], "only managed tags may be removed")

    def test_an_unregistered_tag_fails_with_an_actionable_message(self):
        self.base()
        report, _ctx = self.run_plan(defaults_tags=["Env:prd"])
        self.assertEqual(report.exit_code, 1)
        self.assertIn("tags sync", str(report.failures[0][1]))

    def test_dry_run_applies_nothing(self):
        self.fab.add(["exists"], stdout="false", command="exists")
        report, _ctx = self.run_plan(defaults_tags=["Env:dev"], dry_run=True)
        self.assertFalse([c for c in self.fab.commands if "applyTags" in c])
        tags_action = next(a for a in report.manifest.actions if a["kind"] == "tags")
        self.assertEqual(tags_action["message"], "would apply")

    def test_no_registry_means_tags_are_skipped_not_failed(self):
        self.fab.add(["exists"], stdout="false", command="exists")
        self.fab.add(["mkdir"], stdout="created", command="mkdir")
        self.fab.add(["get"], stdout="ws-1", command="get")
        recipe = Recipe(
            data={
                "display_name_pattern": "Demo - {layer} [{environment}]",
                "defaults": {"tags": ["Env:dev"]},
                "layers": {"Store": {}},
            },
            sources=(),
            environment="dev",
            solution="demo",
        )
        cli = FabricCli(self.log, executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY)
        ctx = RunContext(cli=cli, log=self.log, recipe=recipe, tag_registry=None, sleep=lambda _s: None)
        report = execute(build_plan(recipe), ctx, manifest=Manifest(run_id="t"))
        self.assertEqual(report.exit_code, 0)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.fab = FakeFab()
        self.log = RunLog(level=Level.OFF, stream=_Null(), _colour=False)
        self.cli = FabricCli(self.log, executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY)

    def tearDown(self):
        self.fab.cleanup()

    def test_existing_tags_are_reused_and_missing_ones_created(self):
        self.fab.add_json(
            ["api", "admin/tags", "-X get"],
            {"status_code": 200, "text": {"value": [{"id": "id-env", "displayName": "Env:dev"}]}, "headers": {}},
        )
        self.fab.add_json(
            ["bulkCreateTags"],
            {"status_code": 201, "text": {"tags": [{"id": "id-new", "displayName": "Layer:Store"}]}, "headers": {}},
        )
        registry = TagRegistry()
        result = sync_registry(self.cli, registry, ["Env:dev", "Layer:Store"])

        self.assertEqual(registry.tags["Env:dev"], "id-env")
        self.assertEqual(registry.tags["Layer:Store"], "id-new")
        self.assertEqual(list(result["created"]), ["Layer:Store"])

    def test_creation_is_batched(self):
        self.fab.add_json(["api", "admin/tags", "-X get"], {"status_code": 200, "text": {"value": []}, "headers": {}})
        self.fab.add_json(["bulkCreateTags"], {"status_code": 201, "text": {"tags": []}, "headers": {}})
        sync_registry(self.cli, TagRegistry(), [f"Layer:L{index}" for index in range(45)], batch_size=20)
        self.assertEqual(len([c for c in self.fab.commands if "bulkCreateTags" in c]), 3)

    def test_without_admin_rights_the_error_explains_the_split(self):
        self.fab.add_json(
            ["api", "admin/tags"],
            {"status_code": 403, "text": {"errorCode": "InsufficientPrivileges"}, "headers": {}},
        )
        with self.assertRaises(FabricOpsError) as ctx:
            sync_registry(self.cli, TagRegistry(), ["Env:dev"])
        self.assertIn("administrator", str(ctx.exception))
        self.assertIn("commit the registry", ctx.exception.hint or "")


class _Null:
    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
