"""Golden test: this repository's own recipes must keep loading unchanged.

This is the regression net for decision 4 (permanent aliases). It runs against the real
files in `automation/resources`, so a change to the loader that would break an existing
recipe fails here rather than in a tenant.
"""

import pathlib
import unittest

from fabricops import recipe

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
RESOURCES = REPO_ROOT / "automation" / "resources"

# The internal repository keeps the legacy environments/*.json layout; the public one ships
# solutions/demo/ in canonical YAML. The golden tests run wherever any recipe is defined,
# and only the checks about legacy normalisation need the legacy files.
DEFINED = bool(recipe.resolver.list_solutions(RESOURCES)) if RESOURCES.is_dir() else False
LEGACY = (RESOURCES / "environments").is_dir()


def _solutions():
    """(solution name or None, environment) for everything this repository defines."""
    for entry in recipe.resolver.list_solutions(RESOURCES):
        name = None if entry["name"] == "default" else str(entry["name"])
        for environment in entry["environments"] or [None]:
            yield name, environment


@unittest.skipUnless(DEFINED, "repository recipes not present")
class RepoRecipeTests(unittest.TestCase):
    def test_every_solution_and_environment_loads_and_validates(self):
        solutions = recipe.resolver.list_solutions(RESOURCES)
        self.assertTrue(solutions, "expected at least the default solution")

        checked = 0
        for entry in solutions:
            name = None if entry["name"] == "default" else str(entry["name"])
            for environment in entry["environments"] or [None]:
                with self.subTest(solution=entry["name"], environment=environment):
                    loaded = recipe.load_platform(RESOURCES, solution=name, environment=environment)
                    self.assertTrue(loaded.layers, "a platform recipe must define layers")
                    self.assertTrue(loaded.display_name_pattern)
                    checked += 1
        self.assertGreaterEqual(checked, 1)

    def test_workspace_names_are_unique_per_environment(self):
        for solution, environment in _solutions():
            with self.subTest(solution=solution, environment=environment):
                loaded = recipe.load_platform(RESOURCES, solution=solution, environment=environment)
                names = [loaded.workspace_name(layer) for layer in loaded.layers]
                self.assertEqual(len(names), len(set(names)), "layer workspace names must be unique")
                for name in names:
                    self.assertNotIn("{", name, "every token must be resolved")

    @unittest.skipUnless(LEGACY, "no legacy environments/ recipes here")
    def test_legacy_json_recipes_are_normalised(self):
        loaded = recipe.load_platform(RESOURCES, environment="dev")
        self.assertTrue(loaded.notes, "legacy keys should be reported as folded")

        for layer in loaded.layers:
            for item in loaded.items(layer):
                self.assertIn("name", item, "item_name must be normalised to name")
                self.assertIn("type", item, "the item type must survive normalisation")
                self.assertNotIn("item_name", item)
                self.assertNotIn("skip_item_creation", item)

    def test_git_settings_are_canonical(self):
        loaded = recipe.load_platform(RESOURCES, environment="dev")
        git = loaded.defaults.get("git") or {}
        if git:
            self.assertIn(git.get("provider"), ("GitHub", "AzureDevOps"))
            self.assertNotIn("gitProviderDetails", git)
        for layer in loaded.layers:
            layer_git = loaded.layer(layer).get("git") or {}
            self.assertNotIn("git_directoryName", loaded.layer(layer))
            if layer_git:
                self.assertNotIn("directoryName", layer_git)

    def test_feature_recipe_loads(self):
        loaded = recipe.load_feature(RESOURCES, developer="peer")
        self.assertEqual(loaded.kind, "Feature")
        self.assertTrue(loaded.layers)
        self.assertIn("{layer}", loaded.display_name_pattern + "{layer}")

    def test_spark_settings_became_properties(self):
        loaded = recipe.load_feature(RESOURCES, developer="peer")
        spark_layers = [
            layer
            for layer in loaded.layers
            if any(key.startswith("sparkSettings.") for key in (loaded.layer(layer).get("properties") or {}))
        ]
        for layer in loaded.layers:
            self.assertNotIn("spark_settings", loaded.layer(layer))
        self.assertTrue(spark_layers, "the feature recipe uses spark_settings, so properties should be populated")


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(DEFINED, "repository recipes not present")
class BareValidateTests(unittest.TestCase):
    """`recipe validate` with nothing named must not fail a valid repo.

    It used to validate the base recipe alone, where a `display_name_pattern` of
    "... [{environment}]" cannot resolve, and reported "1 of 1 recipes failed".
    """

    def run_validate(self, *extra):
        import contextlib
        import io

        from fabricops.cli import main

        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(["--resources", str(RESOURCES), "recipe", "validate", *extra])
        return code, out.getvalue()

    def test_a_bare_validate_checks_every_environment(self):
        code, text = self.run_validate()
        self.assertEqual(code, 0, text)
        self.assertIn("[dev]", text)
        self.assertNotIn("failed validation", text)

    def test_naming_one_environment_checks_only_that_one(self):
        code, text = self.run_validate("--environment", "dev")
        self.assertEqual(code, 0, text)
        self.assertIn("[dev]", text)
        self.assertNotIn("[tst]", text)

    def test_all_still_works(self):
        code, text = self.run_validate("--all")
        self.assertEqual(code, 0, text)
        self.assertIn("[dev]", text)
