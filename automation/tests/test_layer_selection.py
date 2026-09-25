"""Selecting layers must filter the plan, never the recipe."""

import unittest

from fabricops.cli import _filter_layers, _keep_layers_in_plan, _keep_with_dependencies, _selected_layers
from fabricops.engine import build_plan
from fabricops.errors import FabricOpsError
from fabricops.recipe import Recipe

GIT = {"provider": "AzureDevOps", "organization": "o", "project": "p", "repository": "r", "branch": "main",
       "credentials": {"source": "ConfiguredConnection", "connection": "Demo-AzureDevOps"}}


def recipe() -> Recipe:
    """Store holds the lakehouse; a solution connection is built from it; Prepare is a plain layer."""
    return Recipe(
        data={
            "display_name_pattern": "Demo - {layer} [{environment}]",
            "defaults": {
                "capacity": "Trial-01",
                "git": GIT,
                "connections": [
                    {"name": "Demo-Curated [{environment}]", "type": "SQL", "auth": "ServicePrincipal",
                     "from_item": {"layer": "Store", "name": "Curated", "type": "Lakehouse"}},
                ],
            },
            "layers": {
                "Store": {"git": {"directory": "solution/store"}},
                "Prepare": {"git": {"directory": "solution/engineering/prepare"}},
            },
        },
        sources=(), solution="demo", environment="dev",
    )


class NarrowingTheRecipeWasTheBug(unittest.TestCase):
    def test_a_recipe_narrowed_to_prepare_cannot_build_its_plan(self):
        # This is the failure the merge-time sync hit: "from_item.layer 'Store' is not a layer".
        narrowed = _filter_layers(recipe(), "Prepare")
        with self.assertRaises(FabricOpsError) as raised:
            build_plan(narrowed)
        self.assertIn("Store", str(raised.exception))


class FilteringThePlanTests(unittest.TestCase):
    def setUp(self):
        self.plan = build_plan(recipe())
        self.kept = _keep_layers_in_plan(self.plan, {"Prepare"})
        self.ids = [a.id for a in self.kept]

    def test_the_full_recipe_builds_a_plan(self):
        self.assertTrue(list(self.plan))

    def test_prepare_actions_are_kept_and_store_actions_are_not(self):
        self.assertIn("workspace:Prepare", self.ids)
        self.assertIn("git:Prepare", self.ids)
        self.assertNotIn("workspace:Store", self.ids)
        self.assertNotIn("git:Store", self.ids)

    def test_the_git_connection_comes_along_as_a_dependency(self):
        connection = [a for a in self.kept if a.kind == "connection" and "AzureDevOps" in a.id]
        self.assertEqual(len(connection), 1)
        self.assertTrue(connection[0].incidental)

    def test_the_store_lakehouse_connection_does_not(self):
        self.assertFalse([a for a in self.kept if "Curated" in a.id])

    def test_prepares_own_actions_are_not_incidental(self):
        by_id = {a.id: a for a in self.kept}
        self.assertFalse(by_id["workspace:Prepare"].incidental)
        self.assertFalse(by_id["git:Prepare"].incidental)

    def test_layer_then_kind_filters_compose(self):
        # What the merge-time sync actually runs: --changed-since (layers) and --only git.
        plan = build_plan(recipe())
        plan.actions = _keep_layers_in_plan(plan, {"Prepare"})
        plan.actions = _keep_with_dependencies(plan, {"git"})
        ids = [a.id for a in plan]
        self.assertIn("git:Prepare", ids)
        self.assertIn("workspace:Prepare", ids)
        self.assertNotIn("workspace:Store", ids)
        by_id = {a.id: a for a in plan}
        self.assertTrue(by_id["workspace:Prepare"].incidental, "under --only git the workspace is a read")


class SelectedLayersTests(unittest.TestCase):
    def test_none_means_all(self):
        self.assertIsNone(_selected_layers(recipe(), None))

    def test_names_are_case_insensitive(self):
        self.assertEqual(_selected_layers(recipe(), "prepare, STORE"), {"Prepare", "Store"})

    def test_no_match_is_an_error_with_the_available_layers(self):
        with self.assertRaises(FabricOpsError) as raised:
            _selected_layers(recipe(), "Nope")
        self.assertIn("Prepare", str(raised.exception))
