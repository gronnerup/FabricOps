"""JSON and YAML are equal citizens; both parse to the same canonical model."""

import pathlib
import shutil
import tempfile
import unittest

from fabricops.errors import RecipeError
from fabricops.recipe import load, loader, normalize

JSON_RECIPE = """
{
  "name": "Sales - {layer} [{environment}]",
  "generic": { "capacity_name": "Trial-01" },
  "layers": {
    "Store": {
      "git_directoryName": "solution/store",
      "items": { "Lakehouse": [ { "item_name": "Curated", "connection_name": "Sales-Curated [{environment}]" } ] }
    }
  }
}
"""

YAML_RECIPE = """
display_name_pattern: "Sales - {layer} [{environment}]"
defaults:
  capacity: Trial-01
layers:
  Store:
    git:
      directory: solution/store
    items:
      - name: Curated
        type: Lakehouse
        connection:
          name: "Sales-Curated [{environment}]"
"""


class TempRepo(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="fabricops-recipe-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, name: str, text: str) -> pathlib.Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


class FormatParityTests(TempRepo):
    def test_json_and_yaml_produce_the_same_model(self):
        json_recipe = load([self.write("platform.json", JSON_RECIPE)], environment="dev")
        yaml_recipe = load([self.write("platform.yml", YAML_RECIPE)], environment="dev")
        self.assertEqual(json_recipe.data, yaml_recipe.data)

    def test_yaml_comments_and_anchors_work(self):
        text = """
# the platform recipe
display_name_pattern: "Sales - {layer} [{environment}]"
defaults: &defaults
  capacity: Trial-01
layers:
  Store:
    <<: *defaults
"""
        recipe = load([self.write("platform.yml", text)], environment="dev")
        self.assertEqual(recipe.layer("Store")["capacity"], "Trial-01")

    def test_unsupported_extension_is_rejected(self):
        with self.assertRaises(RecipeError):
            loader.load_file(self.write("platform.toml", "x = 1"))

    def test_duplicate_keys_are_an_error_in_both_formats(self):
        with self.assertRaises(RecipeError):
            loader.load_file(self.write("dup.json", '{"a": 1, "a": 2}'))
        with self.assertRaises(RecipeError):
            loader.load_file(self.write("dup.yml", "a: 1\na: 2\n"))

    def test_invalid_syntax_reports_position(self):
        with self.assertRaises(RecipeError) as ctx:
            loader.load_file(self.write("bad.json", '{"a": }'))
        self.assertIn("invalid JSON", str(ctx.exception))

    def test_missing_file(self):
        with self.assertRaises(RecipeError):
            loader.load_file(self.root / "nope.yml")


class NormalizeLegacyTests(unittest.TestCase):
    """Decision 4: every legacy spelling keeps working, permanently."""

    def test_root_and_defaults_aliases(self):
        canonical, notes = normalize({"name": "X - {layer}", "generic": {"capacity_name": "Trial-01"}})
        self.assertEqual(canonical["display_name_pattern"], "X - {layer}")
        self.assertEqual(canonical["defaults"]["capacity"], "Trial-01")
        self.assertTrue(any("capacity_name" in note for note in notes))

    def test_items_keyed_by_type_becomes_a_list(self):
        canonical, _ = normalize({"layers": {"Store": {"items": {"Lakehouse": [{"item_name": "Curated"}]}}}})
        items = canonical["layers"]["Store"]["items"]
        self.assertEqual(items, [{"name": "Curated", "type": "Lakehouse"}])

    def test_connection_name_becomes_a_connection_node(self):
        canonical, _ = normalize(
            {"layers": {"Store": {"items": {"Lakehouse": [{"item_name": "C", "connection_name": "conn"}]}}}}
        )
        self.assertEqual(canonical["layers"]["Store"]["items"][0]["connection"], {"name": "conn"})

    def test_spark_settings_become_properties(self):
        canonical, _ = normalize(
            {"layers": {"Prepare": {"spark_settings": {"pool": {"starterPool": {"maxNodeCount": 1}}}}}}
        )
        self.assertEqual(
            canonical["layers"]["Prepare"]["properties"],
            {"sparkSettings.pool.starterPool.maxNodeCount": 1},
        )

    def test_git_settings_are_flattened(self):
        canonical, _ = normalize(
            {
                "generic": {
                    "git_settings": {
                        "gitProviderDetails": {
                            "gitProviderType": "GitHub",
                            "ownerName": "gronnerup",
                            "repositoryName": "FabricOps",
                            "branchName": "main",
                        },
                        "myGitCredentials": {"source": "ConfiguredConnection", "connection_name": "FabricOps-GitHub"},
                    }
                },
                "layers": {"Store": {"git_directoryName": "solution/store"}},
            }
        )
        git = canonical["defaults"]["git"]
        self.assertEqual(git["provider"], "GitHub")
        self.assertEqual(git["owner"], "gronnerup")
        self.assertEqual(git["repository"], "FabricOps")
        self.assertEqual(git["branch"], "main")
        self.assertEqual(git["credentials"]["connection"], "FabricOps-GitHub")
        self.assertEqual(canonical["layers"]["Store"]["git"]["directory"], "solution/store")

    def test_layers_as_a_list_with_names(self):
        canonical, _ = normalize({"layers": [{"name": "Store", "capacity_name": "Trial"}]})
        self.assertEqual(canonical["layers"]["Store"]["capacity"], "Trial")

    def test_canonical_key_wins_over_its_alias(self):
        canonical, _ = normalize({"generic": {"capacity": "New", "capacity_name": "Old"}})
        self.assertEqual(canonical["defaults"]["capacity"], "New")

    def test_feature_recipe_root_keys_fold_into_defaults(self):
        canonical, _ = normalize(
            {"feature_name": "*{feature} ({layer})", "capacity_name": "Trial-01", "permissions": {"admin": []}}
        )
        self.assertEqual(canonical["display_name_pattern"], "*{feature} ({layer})")
        self.assertEqual(canonical["defaults"]["capacity"], "Trial-01")
        self.assertIn("permissions", canonical["defaults"])


if __name__ == "__main__":
    unittest.main()


class StorageDefaultsTests(unittest.TestCase):
    """The storage block defaults to today's behaviour (E07)."""

    def recipe(self, **storage):
        from fabricops.recipe import Recipe

        data = {"display_name_pattern": "S - {layer}", "layers": {}}
        if storage:
            data["storage"] = storage
        return Recipe(data=data, sources=())

    def test_absent_block_means_shared_storage(self):
        storage = self.recipe().storage
        self.assertEqual(storage["default_schema"], "dbo")
        self.assertFalse(storage["feature_schema"]["enabled"])

    def test_values_are_read_through(self):
        storage = self.recipe(
            default_schema="curated",
            feature_schema={"enabled": True, "pattern": "feat_{feature}", "drop_on_teardown": True},
        ).storage
        self.assertEqual(storage["default_schema"], "curated")
        self.assertTrue(storage["feature_schema"]["enabled"])
        self.assertTrue(storage["feature_schema"]["drop_on_teardown"])

    def test_schema_name_is_sanitised_to_fabric_rules(self):
        recipe = self.recipe(feature_schema={"pattern": "dev_{feature}"})
        self.assertEqual(recipe.feature_schema_name(feature="add-orders"), "dev_add_orders")
        self.assertEqual(recipe.feature_schema_name(feature="Add Orders!"), "dev_add_orders")

    def test_developer_patterns_work_too(self):
        recipe = self.recipe(feature_schema={"pattern": "dev_{developer}"})
        self.assertEqual(recipe.feature_schema_name(developer="Peer.G"), "dev_peer_g")
