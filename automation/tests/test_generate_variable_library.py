"""Generating variable library definitions from a recipe declaration (E06)."""

import json
import pathlib
import shutil
import tempfile
import unittest

from fabricops.errors import RecipeError
from fabricops.generate import (
    declarations,
    reference_resolver,
    render_all,
    render_variable_library,
    target_layers,
    write,
)
from fabricops.generate.variable_library import VALUE_SET_DIR, logical_id
from fabricops.recipe import Recipe

DECLARATION = {
    "name": "VL_AppConfig",
    "description": "Run-time configuration",
    "workspaces": ["Orchestrate", "Prepare"],
    "value_sets": ["dev", "tst", "prd"],
    "variables": [
        {"name": "landing_path", "type": "String", "note": "Where raw lands",
         "value": "abfss://dev", "values": {"tst": "abfss://tst", "prd": "abfss://prd"}},
        {"name": "max_rows", "type": "Integer", "value": 1000, "values": {"prd": 1000000}},
        {"name": "curated", "type": "ItemReference", "value": {"layer": "Store", "item": "Curated"}},
    ],
}


def resolve_all(environment, spec):
    return {"workspaceId": f"ws-{environment}", "itemId": f"item-{environment}"}


def recipe_with(declaration=DECLARATION) -> Recipe:
    return Recipe(
        data={
            "display_name_pattern": "Demo - {layer} [{environment}]",
            "layers": {
                "Orchestrate": {"variable_libraries": [declaration]},
                "Prepare": {},
                "Store": {},
            },
        },
        sources=(),
        environment="dev",
        solution="demo",
    )


class RenderTests(unittest.TestCase):
    def render(self, declaration=DECLARATION, resolve=resolve_all):
        return render_variable_library(
            declaration, layer="Orchestrate", solution="demo",
            environments=["dev", "tst", "prd"], resolve_reference=resolve,
        )

    def parts(self, plan, name):
        return json.loads(plan.parts[name])

    def test_the_required_parts_are_produced(self):
        plan = self.render()
        self.assertEqual(
            sorted(plan.parts),
            [".platform", "settings.json", f"{VALUE_SET_DIR}/prd.json", f"{VALUE_SET_DIR}/tst.json", "variables.json"],
        )

    def test_the_first_value_set_supplies_the_defaults(self):
        variables = self.parts(self.render(), "variables.json")["variables"]
        by_name = {v["name"]: v for v in variables}
        self.assertEqual(by_name["landing_path"]["value"], "abfss://dev")
        self.assertEqual(by_name["max_rows"]["value"], 1000)
        self.assertEqual(by_name["landing_path"]["note"], "Where raw lands")

    def test_later_value_sets_become_overrides(self):
        prd = self.parts(self.render(), f"{VALUE_SET_DIR}/prd.json")
        self.assertEqual(prd["name"], "prd")
        overrides = {o["name"]: o["value"] for o in prd["variableOverrides"]}
        self.assertEqual(overrides["landing_path"], "abfss://prd")
        self.assertEqual(overrides["max_rows"], 1000000)

    def test_a_variable_without_a_per_set_value_falls_back_to_the_default(self):
        tst = self.parts(self.render(), f"{VALUE_SET_DIR}/tst.json")
        overrides = {o["name"]: o["value"] for o in tst["variableOverrides"]}
        self.assertEqual(overrides["max_rows"], 1000, "no tst value declared, so the default carries")

    def test_item_references_resolve_per_value_set(self):
        variables = {v["name"]: v for v in self.parts(self.render(), "variables.json")["variables"]}
        self.assertEqual(variables["curated"]["value"], {"workspaceId": "ws-dev", "itemId": "item-dev"})
        prd = {o["name"]: o["value"] for o in self.parts(self.render(), f"{VALUE_SET_DIR}/prd.json")["variableOverrides"]}
        self.assertEqual(prd["curated"], {"workspaceId": "ws-prd", "itemId": "item-prd"})

    def test_an_unresolvable_reference_is_left_out_and_reported(self):
        """Better a missing override than a confidently wrong id."""
        plan = self.render(resolve=lambda env, spec: None if env == "prd" else resolve_all(env, spec))
        prd = {o["name"] for o in self.parts(plan, f"{VALUE_SET_DIR}/prd.json")["variableOverrides"]}
        self.assertNotIn("curated", prd)
        self.assertIn("curated@prd", plan.unresolved)

    def test_value_set_order_is_recorded(self):
        self.assertEqual(self.parts(self.render(), "settings.json")["valueSetsOrder"], ["dev", "tst", "prd"])

    def test_platform_file_identifies_the_item(self):
        platform = self.parts(self.render(), ".platform")
        self.assertEqual(platform["metadata"]["type"], "VariableLibrary")
        self.assertEqual(platform["metadata"]["displayName"], "VL_AppConfig")
        self.assertTrue(platform["config"]["logicalId"])

    def test_logical_ids_are_stable_per_workspace_and_distinct_across_them(self):
        self.assertEqual(logical_id("demo", "Prepare", "VL"), logical_id("demo", "Prepare", "VL"))
        self.assertNotEqual(logical_id("demo", "Prepare", "VL"), logical_id("demo", "Ingest", "VL"))

    def test_an_unknown_variable_type_is_rejected(self):
        with self.assertRaises(RecipeError) as ctx:
            self.render({"name": "VL", "variables": [{"name": "x", "type": "Timestamp", "value": 1}]})
        self.assertIn("ItemReference", str(ctx.exception))

    def test_a_library_needs_a_name(self):
        with self.assertRaises(RecipeError):
            self.render({"variables": []})


class FanOutTests(unittest.TestCase):
    def test_one_library_per_target_workspace(self):
        plans = render_all(recipe_with(), environments=["dev"], manifest_root="/nonexistent")
        self.assertEqual([(p.layer, p.name) for p in plans],
                         [("Orchestrate", "VL_AppConfig"), ("Prepare", "VL_AppConfig")])

    def test_declaring_layer_is_the_default_target(self):
        declaration = {k: v for k, v in DECLARATION.items() if k != "workspaces"}
        self.assertEqual(target_layers(recipe_with(declaration), "Orchestrate", declaration), ["Orchestrate"])

    def test_an_unknown_target_workspace_is_rejected(self):
        declaration = {**DECLARATION, "workspaces": ["Nope"]}
        with self.assertRaises(RecipeError) as ctx:
            target_layers(recipe_with(declaration), "Orchestrate", declaration)
        self.assertIn("Known layers", ctx.exception.hint or "")

    def test_declarations_are_found_per_layer(self):
        self.assertEqual([layer for layer, _ in declarations(recipe_with())], ["Orchestrate"])


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="fabricops-manifests-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def manifest(self, environment, dry_run=False, run="r1"):
        folder = self.root / run
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "manifest.json").write_text(json.dumps({
            "run_id": run, "environment": environment, "dry_run": dry_run,
            "outputs": {
                "workspace:Store": {"id": f"ws-{environment}"},
                "item:Store:Lakehouse:Curated": {"id": f"lh-{environment}"},
            },
        }))

    def test_resolves_from_the_latest_real_run(self):
        self.manifest("dev")
        resolve = reference_resolver(["dev"], self.root)
        self.assertEqual(resolve("dev", {"layer": "Store", "item": "Curated"}),
                         {"workspaceId": "ws-dev", "itemId": "lh-dev"})

    def test_dry_runs_are_ignored(self):
        self.manifest("dev", dry_run=True)
        self.assertIsNone(reference_resolver(["dev"], self.root)("dev", {"layer": "Store", "item": "Curated"}))

    def test_an_environment_never_provisioned_resolves_to_nothing(self):
        self.manifest("dev")
        self.assertIsNone(reference_resolver(["dev", "prd"], self.root)("prd", {"layer": "Store", "item": "Curated"}))

    def test_an_item_missing_from_the_manifest_resolves_to_nothing(self):
        self.manifest("dev")
        self.assertIsNone(reference_resolver(["dev"], self.root)("dev", {"layer": "Store", "item": "Ghost"}))


class WriteTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="fabricops-generated-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_layout_separates_generated_from_authored(self):
        plans = render_all(recipe_with(), environments=["dev", "tst"], manifest_root="/nonexistent")
        written = write(plans, self.root, "demo")
        self.assertTrue((self.root / "demo/orchestrate/VL_AppConfig.VariableLibrary/variables.json").exists())
        self.assertTrue((self.root / f"demo/prepare/VL_AppConfig.VariableLibrary/{VALUE_SET_DIR}/tst.json").exists())
        self.assertEqual(len(written), 2)

    def test_rendering_twice_is_byte_identical(self):
        plans = render_all(recipe_with(), environments=["dev"], manifest_root="/nonexistent")
        write(plans, self.root, "demo")
        first = (self.root / "demo/orchestrate/VL_AppConfig.VariableLibrary/variables.json").read_text()
        write(render_all(recipe_with(), environments=["dev"], manifest_root="/nonexistent"), self.root, "demo")
        self.assertEqual((self.root / "demo/orchestrate/VL_AppConfig.VariableLibrary/variables.json").read_text(), first)


if __name__ == "__main__":
    unittest.main()
