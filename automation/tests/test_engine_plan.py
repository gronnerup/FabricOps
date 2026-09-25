"""Planning: dependency order, generic items, folders, permissions."""

import unittest

from fabricops.errors import RecipeError
from fabricops.recipe import Recipe
from fabricops.engine.actions import Action
from fabricops.engine.plan import build_plan, order


def make_recipe(**data) -> Recipe:
    base = {
        "display_name_pattern": "Demo - {layer} [{environment}]",
        "defaults": {"capacity": "Trial-01"},
        "layers": {},
    }
    base.update(data)
    return Recipe(data=base, sources=(), environment="dev", solution="demo")


class PlanShapeTests(unittest.TestCase):
    def test_a_workspace_per_layer(self):
        plan = build_plan(make_recipe(layers={"Store": {}, "Model": {}}))
        self.assertEqual([a.id for a in plan.of_kind("workspace")], ["workspace:Store", "workspace:Model"])
        self.assertEqual(plan.by_id("workspace:Store").capacity, "Trial-01")

    def test_layer_capacity_overrides_the_default(self):
        plan = build_plan(make_recipe(layers={"Store": {"capacity": "Prod-P1"}}))
        self.assertEqual(plan.by_id("workspace:Store").capacity, "Prod-P1")

    def test_any_item_type_is_planned_generically(self):
        layers = {
            "Store": {
                "items": [
                    {"name": "Curated", "type": "Lakehouse"},
                    {"name": "Sales", "type": "Warehouse"},
                    {"name": "Telemetry", "type": "Eventhouse"},
                    {"name": "Api", "type": "GraphQLApi"},
                    {"name": "Config", "type": "VariableLibrary"},
                ]
            }
        }
        plan = build_plan(make_recipe(layers=layers))
        planned = [action.describe() for action in plan.of_kind("item")]
        self.assertEqual(
            planned,
            ["Lakehouse: Curated", "Warehouse: Sales", "Eventhouse: Telemetry", "GraphQLApi: Api", "VariableLibrary: Config"],
        )

    def test_items_depend_on_their_workspace(self):
        plan = build_plan(make_recipe(layers={"Store": {"items": [{"name": "C", "type": "Lakehouse"}]}}))
        item = plan.by_id("item:Store:Lakehouse:C")
        self.assertIn("workspace:Store", item.depends_on)

    def test_missing_name_or_type_is_rejected(self):
        with self.assertRaises(RecipeError):
            build_plan(make_recipe(layers={"Store": {"items": [{"name": "C"}]}}))

    def test_item_properties_become_their_own_action(self):
        layers = {"Store": {"items": [{"name": "C", "type": "Lakehouse", "properties": {"displayName": "C"}}]}}
        plan = build_plan(make_recipe(layers=layers))
        action = plan.by_id("item:Store:Lakehouse:C:properties")
        self.assertIsNotNone(action)
        self.assertEqual(action.depends_on, ("item:Store:Lakehouse:C",))

    def test_workspace_properties_merge_defaults_and_layer(self):
        recipe = make_recipe(
            defaults={"capacity": "Trial-01", "properties": {"sparkSettings.pool.starterPool.maxNodeCount": 2}},
            layers={"Prepare": {"properties": {"sparkSettings.pool.starterPool.maxExecutors": 1}}},
        )
        action = build_plan(recipe).by_id("properties:Prepare")
        self.assertEqual(
            set(action.properties),
            {"sparkSettings.pool.starterPool.maxNodeCount", "sparkSettings.pool.starterPool.maxExecutors"},
        )

    def test_folders_are_created_parents_first(self):
        layers = {"Store": {"items": [{"name": "C", "type": "Lakehouse", "folder": "Zones/Curated/Gold"}]}}
        plan = build_plan(make_recipe(layers=layers))
        folders = [action.folder for action in plan.of_kind("folder")]
        self.assertEqual(folders, ["Zones", "Zones/Curated", "Zones/Curated/Gold"])
        deepest = plan.by_id("folder:Store:Zones/Curated/Gold")
        self.assertIn("folder:Store:Zones/Curated", deepest.depends_on)
        item = plan.by_id("item:Store:Lakehouse:C")
        self.assertIn("folder:Store:Zones/Curated/Gold", item.depends_on)


class DependencyOrderTests(unittest.TestCase):
    def test_workspace_identity_role_waits_for_the_identity(self):
        recipe = make_recipe(
            layers={
                "Model": {
                    "permissions": {
                        "Admin": [{"type": "WorkspaceIdentity", "workspace": "Demo - Orchestrate [dev]"}]
                    }
                },
                "Orchestrate": {"workspace_identity": True},
            }
        )
        plan = build_plan(recipe)
        ids = [action.id for action in plan]
        role = next(action for action in plan.of_kind("role"))
        self.assertIn("identity:Orchestrate", role.depends_on)
        self.assertLess(ids.index("identity:Orchestrate"), ids.index(role.id))

    def test_report_waits_for_its_semantic_model_in_another_layer(self):
        recipe = make_recipe(
            layers={
                "Present": {
                    "items": [
                        {"name": "Sales", "type": "Report", "creation_payload": {"semanticModel": "SalesModel"}}
                    ]
                },
                "Model": {"items": [{"name": "SalesModel", "type": "SemanticModel"}]},
            }
        )
        plan = build_plan(recipe)
        report = plan.by_id("item:Present:Report:Sales")
        self.assertIn("item:Model:SemanticModel:SalesModel", report.depends_on)
        ids = [action.id for action in plan]
        self.assertLess(ids.index("item:Model:SemanticModel:SalesModel"), ids.index(report.id))

    def test_permissions_merge_without_duplicates(self):
        recipe = make_recipe(
            defaults={"permissions": {"Admin": [{"type": "Group", "id": "g1"}]}},
            layers={"Store": {"permissions": {"Admin": [{"type": "Group", "id": "g1"}, {"type": "Group", "id": "g2"}]}}},
        )
        roles = build_plan(recipe).of_kind("role")
        self.assertEqual([role.principal_id for role in roles], ["g1", "g2"])

    def test_cycles_are_reported(self):
        a = Action(id="a", depends_on=("b",))
        b = Action(id="b", depends_on=("a",))
        with self.assertRaises(RecipeError):
            order([a, b])

    def test_unknown_dependencies_do_not_block(self):
        self.assertEqual([action.id for action in order([Action(id="a", depends_on=("nope",))])], ["a"])


class RenderingTests(unittest.TestCase):
    """The plan is read by people: grouped by default, sequence on request."""

    def full_recipe(self) -> Recipe:
        return make_recipe(
            defaults={
                "capacity": "Trial-01",
                "permissions": {"Admin": [{"type": "Group", "id": "g1"}]},
                "git": {"provider": "GitHub", "owner": "o", "repository": "r", "branch": "main"},
            },
            connections=[{"name": "Demo-SemanticModel", "type": "PowerBIDatasets", "auth": "ServicePrincipal"}],
            layers={
                "Store": {
                    "git": {"directory": "solution/store"},
                    "items": [
                        {
                            "name": "Curated",
                            "type": "Lakehouse",
                            "creation_payload": {"enableSchemas": True},
                            "connection": {"name": "Demo-Curated [dev]"},
                        }
                    ],
                },
                "Model": {"properties": {"sparkSettings.pool.starterPool.maxNodeCount": 1}},
            },
        )

    def test_grouped_puts_solution_first_then_one_block_per_layer(self):
        lines = build_plan(self.full_recipe()).describe()
        headers = [line for line in lines if line and not line.startswith(" ")]
        self.assertEqual(headers[0], "solution")
        self.assertEqual(len(headers), 3, "solution + one block per layer, each appearing once")
        self.assertTrue(headers[1].startswith("Store \u2192 Demo - Store [dev]"))

    def test_the_header_names_the_workspace(self):
        lines = build_plan(self.full_recipe()).describe()
        self.assertIn("Model \u2192 Demo - Model [dev]", lines)

    def test_material_facts_appear_in_the_detail_column(self):
        text = "\n".join(build_plan(self.full_recipe()).describe())
        self.assertIn("capacity=Trial-01", text)
        self.assertIn("enableSchemas=true", text)
        self.assertIn("solution/store @ main", text)
        self.assertIn("SQL \u00b7 from item Curated", text)
        self.assertIn("PowerBIDatasets", text)
        self.assertIn("sparkSettings.pool.starterPool.maxNodeCount", text)

    def test_every_action_is_rendered_exactly_once(self):
        plan = build_plan(self.full_recipe())
        rendered = [line for line in plan.describe() if line.startswith("  ")]
        self.assertEqual(len(rendered), len(plan))

    def test_sequence_view_is_numbered_and_shows_dependencies(self):
        recipe = make_recipe(
            layers={
                "Model": {"permissions": {"Admin": [{"type": "WorkspaceIdentity", "workspace": "Demo - Orchestrate [dev]"}]}},
                "Orchestrate": {"workspace_identity": True},
            }
        )
        lines = build_plan(recipe).describe(sequence=True)
        numbered = [line for line in lines if line.strip()[:1].isdigit()]
        self.assertEqual(len(numbered), len(build_plan(recipe)))
        self.assertTrue(numbered[0].strip().startswith("1."))
        self.assertTrue(any("\u2190 workspace:Orchestrate" in line for line in lines))

    def test_sequence_view_carries_the_scope_on_every_line(self):
        lines = build_plan(self.full_recipe()).describe(sequence=True)
        numbered = [line for line in lines if line.strip()[:1].isdigit()]
        self.assertTrue(all("[" in line and "]" in line for line in numbered))


class DetailTests(unittest.TestCase):
    def test_each_action_kind_reports_its_facts(self):
        plan = build_plan(
            make_recipe(
                defaults={"capacity": "Trial-01", "permissions": {"Admin": [{"type": "Group", "id": "g1"}]}},
                layers={
                    "Store": {
                        "items": [{"name": "C", "type": "Lakehouse", "folder": "Zones/Curated"}],
                        "workspace_identity": True,
                    }
                },
            )
        )
        details: dict[str, list[str]] = {}
        for action in plan:
            details.setdefault(action.kind, []).append(action.detail())

        self.assertEqual(details["workspace"], ["capacity=Trial-01"])
        self.assertEqual(details["identity"], ["managed identity"])
        self.assertEqual(details["folder"], ["Zones", "Zones/Curated"], "parents first")
        self.assertIn("Group g1", details["role"][0])
        self.assertIn("folder=Zones/Curated", details["item"][0])

    def test_properties_detail_truncates_long_lists(self):
        recipe = make_recipe(layers={"Prepare": {"properties": {f"key{index}": index for index in range(6)}}})
        detail = build_plan(recipe).by_id("properties:Prepare").detail()
        self.assertIn("+3 more", detail)

    def test_a_plain_action_has_no_detail(self):
        self.assertEqual(Action(id="x").detail(), "")


class TeardownTests(unittest.TestCase):
    def test_teardown_is_reverse_order_and_only_owns_workspaces(self):
        recipe = make_recipe(layers={"Store": {"items": [{"name": "C", "type": "Lakehouse"}]}, "Model": {}})
        plan = build_plan(recipe)
        destroy = [action.id for action in plan.for_destroy()]
        self.assertEqual(destroy, ["workspace:Model", "workspace:Store"])


if __name__ == "__main__":
    unittest.main()
