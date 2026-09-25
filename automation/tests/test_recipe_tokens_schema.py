"""Token substitution and validation: mistakes must fail here, not in Fabric."""

import os
import unittest

from fabricops.errors import RecipeError
from fabricops.recipe.schema import validate
from fabricops.recipe.tokens import find_tokens, substitute


class TokenTests(unittest.TestCase):
    def test_substitutes_everywhere_not_just_in_names(self):
        node = {"connection": {"name": "Sales-Curated [{environment}]"}, "tags": ["Env:{environment}"]}
        out = substitute(node, {"environment": "tst"})
        self.assertEqual(out["connection"]["name"], "Sales-Curated [tst]")
        self.assertEqual(out["tags"], ["Env:tst"])

    def test_layer_token_is_deferred_at_the_root(self):
        out = substitute("Sales - {layer} [{environment}]", {"environment": "dev"}, deferred=("layer",))
        self.assertEqual(out, "Sales - {layer} [dev]")

    def test_deferred_token_still_resolves_when_provided(self):
        out = substitute("Sales - {layer} [{environment}]", {"environment": "dev", "layer": "Store"}, deferred=("layer",))
        self.assertEqual(out, "Sales - Store [dev]")

    def test_env_token(self):
        os.environ["FABOPS_TEST_CAPACITY"] = "Trial-01"
        try:
            self.assertEqual(substitute("{env:FABOPS_TEST_CAPACITY}", {}), "Trial-01")
        finally:
            del os.environ["FABOPS_TEST_CAPACITY"]

    def test_missing_env_variable_is_an_error_with_a_hint(self):
        with self.assertRaises(RecipeError) as ctx:
            substitute("{env:FABOPS_DEFINITELY_NOT_SET}", {})
        self.assertIn("FABOPS_DEFINITELY_NOT_SET", str(ctx.exception))

    def test_unknown_token_is_an_error_not_a_literal(self):
        with self.assertRaises(RecipeError) as ctx:
            substitute({"a": "{envrionment}"}, {"environment": "dev"}, path="defaults")
        self.assertIn("unknown token", str(ctx.exception))
        self.assertIn("defaults.a", str(ctx.exception))

    def test_known_token_without_a_value_reports_the_context(self):
        with self.assertRaises(RecipeError) as ctx:
            substitute("{developer}", {"environment": "dev"})
        self.assertIn("not available here", str(ctx.exception))

    def test_legacy_token_names_still_resolve(self):
        """feature.json used {feature_name} and {layer_name} - decision 4 keeps them working."""
        out = substitute("*{feature_name} ({layer_name})", {"feature": "add-orders", "layer": "Prepare"})
        self.assertEqual(out, "*add-orders (Prepare)")

    def test_legacy_layer_token_is_deferred_too(self):
        out = substitute("*{feature_name} ({layer_name})", {"feature": "add-orders"}, deferred=("layer",))
        self.assertEqual(out, "*add-orders ({layer_name})")

    def test_find_tokens(self):
        self.assertEqual(find_tokens({"a": "{environment}", "b": ["{layer}"]}), {"environment", "layer"})


class ValidationTests(unittest.TestCase):
    def valid(self):
        return {
            "display_name_pattern": "Sales - {layer} [{environment}]",
            "defaults": {"capacity": "Trial-01", "tags": ["ManagedBy:FabricOps"]},
            "layers": {"Store": {"items": [{"name": "Curated", "type": "Lakehouse"}]}},
        }

    def test_a_valid_recipe_passes(self):
        self.assertEqual(validate(self.valid()), [])

    def test_unknown_property_suggests_the_intended_one(self):
        recipe = self.valid()
        recipe["layers"]["Store"]["items"][0]["typ"] = "Lakehouse"
        with self.assertRaises(RecipeError) as ctx:
            validate(recipe)
        message = str(ctx.exception)
        self.assertIn("layers.Store.items[0].typ", message)
        self.assertIn("did you mean 'type'", message)

    def test_legacy_key_is_reported_as_legacy_not_unknown(self):
        recipe = {"layers": {"Store": {"items": [{"name": "C", "type": "Lakehouse", "item_name": "C"}]}}}
        with self.assertRaises(RecipeError) as ctx:
            validate(recipe)
        self.assertIn("canonical name is 'name'", str(ctx.exception))

    def test_missing_required_property(self):
        recipe = {"layers": {"Store": {"items": [{"name": "Curated"}]}}}
        with self.assertRaises(RecipeError) as ctx:
            validate(recipe)
        self.assertIn("missing required property 'type'", str(ctx.exception))

    def test_wrong_type(self):
        recipe = {"layers": {"Store": {"items": {"not": "a list or legacy map of lists"}}}}
        with self.assertRaises(RecipeError):
            validate(recipe)

    def test_invalid_choice_lists_the_options(self):
        recipe = {"defaults": {"git": {"provider": "Gitea"}}}
        with self.assertRaises(RecipeError) as ctx:
            validate(recipe)
        self.assertIn("AzureDevOps", str(ctx.exception))

    def test_storage_block_validates(self):
        recipe = {
            "storage": {
                "default_schema": "dbo",
                "feature_schema": {
                    "enabled": True,
                    "pattern": "dev_{feature}",
                    "drop_on_teardown": False,
                    "lakehouses": ["Store/Curated"],
                },
            }
        }
        self.assertEqual(validate(recipe), [])

    def test_a_feature_schema_pattern_must_vary_per_feature(self):
        with self.assertRaises(RecipeError) as ctx:
            validate({"storage": {"feature_schema": {"pattern": "dev_shared"}}})
        self.assertIn("{feature}", str(ctx.exception))

    def test_a_pattern_that_cannot_be_a_schema_name_is_rejected(self):
        with self.assertRaises(RecipeError) as ctx:
            validate({"storage": {"feature_schema": {"pattern": "dev-{feature}"}}})
        self.assertIn("letters, numbers and underscore", str(ctx.exception))

    def test_free_form_nodes_accept_anything(self):
        recipe = {
            "layers": {
                "Prepare": {
                    "properties": {"sparkSettings.pool.starterPool.maxNodeCount": 1},
                    "permissions": {"admin": [{"type": "Group", "id": "abc"}]},
                }
            }
        }
        self.assertEqual(validate(recipe), [])

    def test_all_problems_are_reported_at_once(self):
        recipe = {"layers": {"Store": {"nope": 1, "items": [{"name": "C"}]}}}
        with self.assertRaises(RecipeError) as ctx:
            validate(recipe)
        message = str(ctx.exception)
        self.assertIn("nope", message)
        self.assertIn("missing required property 'type'", message)

    def test_internal_and_merge_keys_are_ignored(self):
        self.assertEqual(validate({"$merge": "replace", "merge_type": 2, "layers": {"Store": {}}}), [])


if __name__ == "__main__":
    unittest.main()
