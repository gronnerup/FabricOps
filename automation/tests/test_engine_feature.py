"""Feature workspaces: branch parsing, layer selection, relations, ownership tags."""

import json
import unittest

from fabricops.engine import Manifest, RunContext, execute
from fabricops.engine.feature import build_feature_plan, parse_branch, participating_layers
from fabricops.engine.tags import TagRegistry
from fabricops.errors import RecipeError
from fabricops.fabric.cli import NO_RETRY, FabricCli
from fabricops.obs.logging import Level, RunLog
from fabricops.recipe import Recipe
from support import FakeFab

GIT = {
    "provider": "GitHub",
    "owner": "gronnerup",
    "repository": "FabricOps",
    "credentials": {"source": "ConfiguredConnection", "connection": "FabricOps-GitHub"},
}


def feature_recipe(**overrides) -> Recipe:
    data = {
        "kind": "Feature",
        "display_name_pattern": "*{feature} ({layer})",
        "defaults": {"capacity": "Trial-01", "git": GIT},
        "layers": {
            "Prepare": {"git": {"directory": "solution/prepare"}},
            "Ingest": {"git": {"directory": "solution/ingest"}},
            "Model": {"always": True, "git": {"directory": "solution/model"}},
        },
        "base_display_name_pattern": "Demo - {layer} [{environment}]",
    }
    data.update(overrides)
    return Recipe(data=data, sources=(), kind="Feature", solution="demo")


class BranchTests(unittest.TestCase):
    def test_topic_is_the_last_segment(self):
        info = parse_branch("feature/peer/add-orders", ["Prepare"])
        self.assertEqual(info.topic, "add-orders")
        self.assertIsNone(info.layer)

    def test_a_leading_layer_segment_is_recognised(self):
        info = parse_branch("feature/prepare/add-orders", ["Prepare", "Model"])
        self.assertEqual(info.layer, "Prepare")

    def test_refs_heads_and_bare_names_are_handled(self):
        for branch in ("refs/heads/feature/peer/add-orders", "feature/peer/add-orders", "peer/add-orders"):
            with self.subTest(branch=branch):
                self.assertEqual(parse_branch(branch, []).topic, "add-orders")

    def test_an_empty_branch_is_an_error(self):
        with self.assertRaises(RecipeError):
            parse_branch("feature/", [])

    def test_branch_slug_is_tag_safe(self):
        self.assertEqual(parse_branch("feature/peer/add_orders!", []).slug, "feature-peer-add-orders")

    def test_layer_selection_includes_always_layers(self):
        recipe = feature_recipe()
        info = parse_branch("feature/prepare/add-orders", list(recipe.layers))
        self.assertEqual(participating_layers(recipe, info), ["Prepare", "Model"])

    def test_no_layer_in_the_branch_means_every_layer(self):
        recipe = feature_recipe()
        info = parse_branch("feature/peer/add-orders", list(recipe.layers))
        self.assertEqual(participating_layers(recipe, info), ["Prepare", "Ingest", "Model"])

    def test_explicit_layers_win(self):
        recipe = feature_recipe()
        info = parse_branch("feature/prepare/add-orders", list(recipe.layers))
        self.assertEqual(participating_layers(recipe, info, ["ingest"]), ["Ingest"])


class FeaturePlanTests(unittest.TestCase):
    def test_workspace_names_use_the_feature_pattern(self):
        plan = build_feature_plan(feature_recipe(), branch="feature/prepare/add-orders", developer="peer")
        names = [action.workspace for action in plan.of_kind("workspace")]
        self.assertEqual(names, ["*add-orders (Prepare)", "*add-orders (Model)"])

    def test_git_connects_the_feature_branch(self):
        plan = build_feature_plan(feature_recipe(), branch="feature/prepare/add-orders")
        git_action = plan.by_id("git:Prepare")
        self.assertEqual(git_action.branch, "feature/prepare/add-orders")
        self.assertEqual(git_action.directory, "solution/prepare")
        self.assertEqual(git_action.connection_name, "FabricOps-GitHub")

    def test_the_relation_targets_the_base_environment_workspace(self):
        plan = build_feature_plan(feature_recipe(), branch="feature/prepare/add-orders", base_environment="dev")
        relation = plan.by_id("relation:Prepare")
        self.assertEqual(relation.base_workspace, "Demo - Prepare [dev]")
        self.assertIn("git:Prepare", relation.depends_on)

    def test_no_relation_when_the_workspace_is_disconnected_after_initialising(self):
        recipe = feature_recipe(
            layers={"Model": {"git": {"directory": "solution/model", "disconnect_after_initialize": True}}}
        )
        plan = build_feature_plan(recipe, branch="feature/peer/x")
        self.assertEqual(plan.of_kind("relation"), [])

    def test_the_developer_becomes_an_admin_of_their_own_workspace(self):
        plan = build_feature_plan(
            feature_recipe(), branch="feature/prepare/add-orders", developer="peer", developer_object_id="11112222-3333-4444-5555-666677778888"
        )
        role = plan.by_id("role:Prepare:developer")
        self.assertEqual((role.principal_id, role.role, role.principal_type), ("11112222-3333-4444-5555-666677778888", "admin", "User"))

    def test_ownership_tags_are_added_when_tagging_is_in_use(self):
        recipe = feature_recipe()
        recipe.data["defaults"]["tags"] = ["ManagedBy:FabricOps", "Solution:demo"]
        plan = build_feature_plan(recipe, branch="feature/prepare/add-orders", developer="peer")
        tags = plan.by_id("tags:Prepare").tags
        self.assertIn("Lifecycle:Feature", tags)
        self.assertIn("Owner:peer", tags)
        self.assertIn("Layer:Prepare", tags)
        self.assertTrue(any(tag.startswith("Branch:") for tag in tags))

    def test_no_tags_declared_means_no_tag_action(self):
        plan = build_feature_plan(feature_recipe(), branch="feature/prepare/add-orders", developer="peer")
        self.assertEqual(plan.of_kind("tags"), [])

    def test_spark_settings_from_a_legacy_recipe_are_applied(self):
        recipe = feature_recipe(
            layers={"Prepare": {"properties": {"sparkSettings.pool.starterPool.maxNodeCount": 1}}}
        )
        plan = build_feature_plan(recipe, branch="feature/prepare/x")
        self.assertIn("sparkSettings.pool.starterPool.maxNodeCount", plan.by_id("properties:Prepare").properties)

    def test_an_unknown_layer_filter_is_rejected(self):
        with self.assertRaises(RecipeError):
            build_feature_plan(feature_recipe(), branch="feature/peer/x", layers=["Nope"])


class FeatureExecutionTests(unittest.TestCase):
    def setUp(self):
        self.fab = FakeFab()
        self.log = RunLog(level=Level.OFF, stream=_Null(), _colour=False)

    def tearDown(self):
        self.fab.cleanup()

    def freshly_created_workspace(self):
        """The workspace does not exist yet, so this run creates it.

        The relation is only attempted on a workspace this run created - Fabric establishes
        it as part of branching out, and there is no API to read existing relations - so
        every relation test has to start from here.
        """
        self.fab.add(["exists", "Demo - Prepare [dev]"], stdout="true", command="exists")
        self.fab.add(["exists", "FabricOps-GitHub"], stdout="true", command="exists")
        self.fab.add(["exists"], stdout="false", command="exists")
        self.fab.add(["mkdir"], stdout="created", command="mkdir")
        self.fab.add(["get", "Demo - Prepare [dev]"], stdout="base-ws-1", command="get")
        self.fab.add(["get", "FabricOps-GitHub"], stdout="conn-1", command="get")
        self.fab.add(["get"], stdout="feature-ws-1", command="get")

    def run_feature(self, branch="feature/prepare/add-orders", **kwargs):
        recipe = feature_recipe(layers={"Prepare": {"git": {"directory": "solution/prepare"}}})
        cli = FabricCli(self.log, executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY)
        ctx = RunContext(cli=cli, log=self.log, recipe=recipe, tag_registry=TagRegistry(), sleep=lambda _s: None)
        plan = build_feature_plan(recipe, branch=branch, developer="peer", **kwargs)
        return execute(plan, ctx, manifest=Manifest(run_id="t")), ctx

    def test_a_feature_workspace_is_created_connected_and_related(self):
        self.fab.add(["exists", "Demo - Prepare [dev]"], stdout="true", command="exists")
        self.fab.add(["exists", "FabricOps-GitHub"], stdout="true", command="exists")
        self.fab.add(["exists"], stdout="false", command="exists")
        self.fab.add(["mkdir"], stdout="created", command="mkdir")
        self.fab.add(["get", "Demo - Prepare [dev]"], stdout="base-ws-1", command="get")
        self.fab.add(["get", "FabricOps-GitHub"], stdout="conn-1", command="get")
        self.fab.add(["get"], stdout="feature-ws-1", command="get")
        self.fab.add_json(["git/connection"], {"status_code": 200, "text": {"gitConnectionState": "NotConnected"}, "headers": {}})
        self.fab.add_json(["git/connect"], {"status_code": 200, "text": {}, "headers": {}})
        self.fab.add_json(["initializeConnection"], {"status_code": 200, "text": {"requiredAction": "None"}, "headers": {}})
        self.fab.add_json(["workspaceRelations"], {"status_code": 201, "text": {"id": "rel-1"}, "headers": {}})

        report, ctx = self.run_feature()

        relation = next(c for c in self.fab.commands if "workspaceRelations" in c)
        body = json.loads(relation.split("-i ", 1)[1].split(" --show_headers")[0])
        self.assertEqual(body, {"relatedWorkspaceId": "base-ws-1", "relationType": "Base"})
        connect = next(c for c in self.fab.commands if "git/connect " in c)
        self.assertIn("feature/prepare/add-orders", connect)
        self.assertIn("conn-1", connect)
        self.assertEqual(report.exit_code, 0)

    def test_an_existing_relation_is_not_an_error(self):
        self.freshly_created_workspace()
        self.fab.add_json(["git/connection"], {"status_code": 200, "text": {"gitConnectionState": "Connected"}, "headers": {}})
        self.fab.add_json(
            ["workspaceRelations"],
            {"status_code": 409, "text": {"errorCode": "WorkspaceRelationAlreadyExists"}, "headers": {}},
        )
        report, _ctx = self.run_feature()
        relation = next(a for a in report.manifest.actions if a["kind"] == "relation")
        self.assertEqual(relation["status"], "existed")
        self.assertEqual(report.exit_code, 0)

    def test_a_root_directory_mismatch_is_explained_not_fatal(self):
        self.freshly_created_workspace()
        self.fab.add_json(["git/connection"], {"status_code": 200, "text": {"gitConnectionState": "Connected"}, "headers": {}})
        self.fab.add_json(
            ["workspaceRelations"],
            {"status_code": 400, "text": {"errorCode": "WorkspaceRelationRootDirectoryMismatch"}, "headers": {}},
        )
        report, _ctx = self.run_feature()
        relation = next(a for a in report.manifest.actions if a["kind"] == "relation")
        self.assertEqual(relation["status"], "skipped")
        self.assertIn("same git root directory", relation["message"])
        self.assertEqual(report.exit_code, 0)

    def test_a_missing_base_workspace_is_skipped(self):
        # The feature workspace is created; the base it should relate to is not there.
        self.fab.add(["exists", "Demo - Prepare [dev]"], stdout="false", command="exists")
        self.fab.add(["exists", "FabricOps-GitHub"], stdout="true", command="exists")
        self.fab.add(["exists"], stdout="false", command="exists")
        self.fab.add(["mkdir"], stdout="created", command="mkdir")
        self.fab.add(["get", "FabricOps-GitHub"], stdout="conn-1", command="get")
        self.fab.add(["get"], stdout="feature-ws-1", command="get")
        self.fab.add_json(["git/connection"], {"status_code": 200, "text": {"gitConnectionState": "Connected"}, "headers": {}})
        report, _ctx = self.run_feature()
        relation = next(a for a in report.manifest.actions if a["kind"] == "relation")
        self.assertIn("does not exist", relation["message"])

    def test_teardown_deletes_the_feature_workspaces(self):
        self.fab.add(["exists"], stdout="true", command="exists")
        self.fab.add(["rm"], stdout="deleted", command="rm")
        self.fab.add(["get"], stdout="ws-1", command="get")

        recipe = feature_recipe(layers={"Prepare": {"git": {"directory": "solution/prepare"}}})
        cli = FabricCli(self.log, executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY)
        ctx = RunContext(cli=cli, log=self.log, recipe=recipe, sleep=lambda _s: None)
        plan = build_feature_plan(recipe, branch="feature/prepare/add-orders", developer="peer")
        report = execute(plan, ctx, manifest=Manifest(run_id="t"), destroy=True)

        removals = [c for c in self.fab.commands if c.startswith("rm")]
        self.assertEqual(len(removals), 1)
        self.assertIn("*add-orders (Prepare)", removals[0])
        self.assertEqual(report.exit_code, 0)


class _Null:
    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass


if __name__ == "__main__":
    unittest.main()


class BranchGroupTests(unittest.TestCase):
    """`feature/<segment>/<topic>`: group recipe first, layer second, everything third."""

    def test_segment_is_extracted_before_any_recipe_is_loaded(self):
        from fabricops.engine.feature import branch_segment

        self.assertEqual(branch_segment("feature/backend/add-orders"), "backend")
        self.assertEqual(branch_segment("refs/heads/feature/backend/add-orders"), "backend")
        self.assertIsNone(branch_segment("feature/add-orders"), "a single segment is the topic, not a group")

    def test_a_group_suppresses_the_layer_filter(self):
        recipe = feature_recipe()
        info = parse_branch("feature/prepare/x", list(recipe.layers), group="prepare")
        self.assertIsNone(info.layer, "a segment consumed as a group must not also filter layers")
        self.assertEqual(info.group, "prepare")

    def test_without_a_group_the_segment_still_matches_a_layer(self):
        recipe = feature_recipe()
        info = parse_branch("feature/prepare/x", list(recipe.layers))
        self.assertEqual(info.layer, "Prepare")

    def test_a_group_plan_uses_the_layers_the_group_recipe_declares(self):
        recipe = feature_recipe(layers={"Ingest": {}, "Prepare": {}})
        plan = build_feature_plan(recipe, branch="feature/backend/add-orders", group="backend")
        self.assertEqual([a.layer for a in plan.of_kind("workspace")], ["Ingest", "Prepare"])


class TeardownSymmetryTests(unittest.TestCase):
    """Delete must touch exactly the layers create touched - the ADO refs/heads bug."""

    def layers_for(self, branch, destroy):
        recipe = feature_recipe()
        plan = build_feature_plan(recipe, branch=branch, developer="peer")
        actions = plan.for_destroy() if destroy else list(plan)
        return sorted({action.layer for action in actions if action.kind == "workspace"})

    def test_create_and_delete_agree_for_a_layer_branch(self):
        self.assertEqual(self.layers_for("feature/prepare/add-orders", False),
                         self.layers_for("feature/prepare/add-orders", True))

    def test_a_refs_heads_branch_still_resolves_to_one_layer(self):
        """Azure DevOps passes refs/heads/…; the first-generation trimming lost the layer."""
        for branch in ("feature/prepare/add-orders", "refs/heads/feature/prepare/add-orders"):
            with self.subTest(branch=branch):
                self.assertEqual(self.layers_for(branch, True), ["Model", "Prepare"])  # Model is always:true

    def test_a_branch_without_a_layer_segment_deletes_every_layer(self):
        self.assertEqual(self.layers_for("feature/peer/add-orders", True), ["Ingest", "Model", "Prepare"])


class DeveloperObjectIdTests(unittest.TestCase):
    """The developer role is a convenience; a bad id must not fail the whole run."""

    def recipe(self):
        from fabricops.recipe import Recipe

        return Recipe(
            data={"display_name_pattern": "*{feature} ({layer})", "layers": {"Prepare": {}}},
            sources=(), solution="demo",
        )

    def plan(self, object_id):
        return build_feature_plan(
            self.recipe(), branch="feature/prepare/x", developer="peer",
            developer_object_id=object_id, register_relation=False,
        )

    def test_a_guid_grants_the_developer_admin(self):
        plan = self.plan("aaaabbbb-cccc-dddd-eeee-ffff00001111")
        self.assertTrue([a for a in plan if a.id.endswith(":developer")])
        self.assertEqual(plan.warnings, [])

    def test_a_ci_actor_id_warns_instead_of_failing(self):
        """GITHUB_ACTOR_ID is GitHub's own numeric id; Fabric cannot resolve it."""
        plan = self.plan("52330973")
        self.assertFalse([a for a in plan if a.id.endswith(":developer")])
        self.assertEqual(len(plan.warnings), 1)
        self.assertIn("not an Entra object id", plan.warnings[0])

    def test_no_id_at_all_is_silent(self):
        plan = self.plan(None)
        self.assertFalse([a for a in plan if a.id.endswith(":developer")])
        self.assertEqual(plan.warnings, [])

    def test_an_email_is_not_an_object_id_either(self):
        self.assertEqual(len(self.plan("peer@example.com").warnings), 1)


class FeatureReferenceDeferralTests(unittest.TestCase):
    """A feature branch carries the same committed ids, so it needs the same guard.

    Without it the feature run attempted the pull and Fabric answered
    `PowerBIEntityNotFound`, naming a report id and nothing a reader could act on.
    """

    LAYERS = {
        "Prepare": {"git": {"directory": "solution/prepare"}},
        "Present": {"git": {"directory": "solution/present"}},
        "Model": {"always": True, "git": {"directory": "solution/model"}},
    }

    def base_recipe(self):
        return Recipe(
            data={
                "kind": "Platform",
                "display_name_pattern": "Demo - {layer} [{environment}]",
                "defaults": {"capacity": "Trial-01"},
                "layers": {
                    "Model": {"git": {"directory": "solution/model"}},
                    "Present": {"git": {"directory": "solution/present"}},
                },
                "references": [
                    {
                        "file": "solution/present/R.Report/definition.pbir",
                        "replace": [
                            {
                                "pattern": "semanticmodelid=([0-9a-fA-F-]{36})",
                                "layer": "Model",
                                "item": "R",
                                "type": "SemanticModel",
                                "label": "report: semantic model",
                                "blocks_sync": True,
                            }
                        ],
                    }
                ],
            },
            sources=(),
            kind="Platform",
            solution="demo",
            environment="dev",
        )

    def setUp(self):
        import pathlib
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        report = self.root / "solution" / "present" / "R.Report"
        report.mkdir(parents=True)
        (report / "definition.pbir").write_text(
            json.dumps(
                {
                    "datasetReference": {
                        "byConnection": {
                            "connectionString": "Data Source=x;semanticmodelid=00000000-0000-0000-0000-00000000a003"
                        }
                    }
                }
            )
        )

    def build(self, base):
        return build_feature_plan(
            feature_recipe(layers=self.LAYERS),
            branch="feature/peer/tweak",
            base_recipe=base,
            repository_root=str(self.root),
        )

    def git_action(self, plan, layer):
        return next((a for a in plan if a.kind == "git" and a.layer == layer), None)

    def test_the_present_layer_carries_the_blocking_reference(self):
        action = self.git_action(self.build(self.base_recipe()), "Present")
        self.assertIsNotNone(action, "Present should have a git action")
        self.assertEqual([r.label for r in action.references], ["report: semantic model"])

    def test_it_resolves_against_the_base_environment_not_the_feature(self):
        base = self.base_recipe()
        self.assertIs(self.git_action(self.build(base), "Present").reference_recipe, base)

    def test_a_layer_that_carries_no_reference_is_not_held_up(self):
        plan = self.build(self.base_recipe())
        for layer in ("Prepare", "Model"):
            with self.subTest(layer=layer):
                action = self.git_action(plan, layer)
                if action is not None:
                    self.assertEqual(action.references, ())

    def test_no_platform_recipe_means_no_guard_rather_than_a_crash(self):
        action = self.git_action(self.build(None), "Present")
        self.assertEqual(action.references, ())
        self.assertIsNone(action.reference_recipe)


class RelationOnlyOnCreateTests(FeatureExecutionTests):
    """The relation is attempted once, when the workspace is created.

    Fabric establishes it as part of branching out, which is a create-time operation, and
    offers no API to read the relations a workspace already has. So on a re-run there is no
    way to tell an existing relation from a missing one - and re-POSTing an existing one
    comes back as `BadRequest: An error occurred in the Entity Framework`, which is
    indistinguishable from a real fault. `WorkspaceRelationAlreadyExists` is documented for
    this and the service does not send it.
    """

    def existing_workspace(self):
        self.fab.add(["exists"], stdout="true", command="exists")
        self.fab.add(["get", "Demo - Prepare [dev]"], stdout="base-ws-1", command="get")
        self.fab.add(["get", "FabricOps-GitHub"], stdout="conn-1", command="get")
        self.fab.add(["get"], stdout="feature-ws-1", command="get")
        self.fab.add_json(
            ["git/connection"],
            {"status_code": 200, "text": {"gitConnectionState": "ConnectedAndInitialized"}, "headers": {}},
        )
        self.fab.add_json(
            ["git/status"],
            {"status_code": 200, "text": {"workspaceHead": "a", "remoteCommitHash": "a", "changes": []}, "headers": {}},
        )

    def relation(self, report):
        return next(a for a in report.manifest.actions if a["kind"] == "relation")

    def test_an_existing_workspace_is_left_alone(self):
        self.existing_workspace()
        report, _ctx = self.run_feature()
        self.assertEqual(self.relation(report)["status"], "skipped")
        self.assertIn("already existed", self.relation(report)["message"])
        self.assertEqual(report.exit_code, 0)

    def test_no_request_is_sent_for_an_existing_workspace(self):
        self.existing_workspace()
        self.run_feature()
        self.assertEqual([c for c in self.fab.commands if "workspaceRelations" in c], [])

    def test_it_is_not_reported_as_existing_since_nothing_checked(self):
        # `existed` would be a status we did not verify - there is no API to verify it with.
        self.existing_workspace()
        report, _ctx = self.run_feature()
        self.assertNotEqual(self.relation(report)["status"], "existed")

    def test_a_created_workspace_still_gets_its_relation(self):
        self.freshly_created_workspace()
        self.fab.add_json(["git/connection"], {"status_code": 200, "text": {"gitConnectionState": "NotConnected"}, "headers": {}})
        self.fab.add_json(["git/connect"], {"status_code": 200, "text": {}, "headers": {}})
        self.fab.add_json(["initializeConnection"], {"status_code": 200, "text": {"requiredAction": "None"}, "headers": {}})
        self.fab.add_json(["workspaceRelations"], {"status_code": 201, "text": {"id": "rel-1"}, "headers": {}})
        report, _ctx = self.run_feature()
        self.assertEqual(self.relation(report)["status"], "created")
        self.assertTrue([c for c in self.fab.commands if "workspaceRelations" in c])


class BaseStampInPlanTests(unittest.TestCase):
    def test_the_workspace_description_names_the_base_template(self):
        plan = build_feature_plan(feature_recipe(), branch="feature/prepare/add-orders",
                                  base_pattern="Demo - {layer} [{environment}]", base_environment="dev")
        props = plan.by_id("properties:Prepare").properties
        self.assertIn("base=Demo - {layer} [dev]", props["description"])

    def test_without_a_base_pattern_there_is_no_base_field(self):
        recipe = feature_recipe()
        recipe.data.pop("base_display_name_pattern", None)
        plan = build_feature_plan(recipe, branch="feature/prepare/add-orders", base_pattern=None)
        self.assertNotIn("base=", plan.by_id("properties:Prepare").properties["description"])
