"""`permissions.mode`: additive keeps what the recipe does not declare, strict removes it.

Two things are never removed: declared principals, and the identity running the setup,
because Fabric made it admin when it created the workspace and no recipe lists it.
"""

import json
import pathlib
import shutil
import tempfile
import unittest

from fabricops import recipe as recipe_module
from fabricops.engine import Manifest, RunContext, build_plan, execute
from fabricops.engine.actions import ReconcileRoles
from fabricops.engine.feature import build_feature_plan
from fabricops.engine.permissions import resolve_mode, split_permissions
from fabricops.errors import RecipeError
from fabricops.fabric.cli import NO_RETRY, FabricCli
from fabricops.obs.logging import Level, RunLog
from fabricops.recipe import Recipe
from fabricops.recipe.schema import validate
from support import FakeFab

GROUP = "11111111-1111-1111-1111-111111111111"     # declared in the recipe
STRANGER = "22222222-2222-2222-2222-222222222222"  # added in the portal
ME = "33333333-3333-3333-3333-333333333333"        # the identity running setup


def make_recipe(defaults: dict, layers: dict) -> Recipe:
    return Recipe(
        data={"display_name_pattern": "Demo - {layer} [{environment}]", "defaults": {"capacity": "cap", **defaults}, "layers": layers},
        sources=(),
        environment="dev",
        solution="demo",
    )


def roles_action(plan, layer="Store") -> ReconcileRoles:
    return next(a for a in plan.actions if isinstance(a, ReconcileRoles) and a.layer == layer)


# ------------------------------------------------------------------ the recipe side
class SplitTests(unittest.TestCase):
    def test_mode_is_separated_from_the_roles(self):
        mode, roles = split_permissions({"mode": "Strict", "Admin": [{"id": GROUP}]})
        self.assertEqual(mode, "strict")
        self.assertEqual(roles, {"Admin": [{"id": GROUP}]})

    def test_default_is_additive_and_the_layer_wins(self):
        self.assertEqual(resolve_mode(None, None), "additive")
        self.assertEqual(resolve_mode({"mode": "strict"}, {}), "strict")
        self.assertEqual(resolve_mode({"mode": "strict"}, {"mode": "additive"}), "additive")


class SchemaTests(unittest.TestCase):
    def base(self, permissions):
        return {"display_name_pattern": "D - {layer} [{environment}]", "defaults": {"permissions": permissions}, "layers": {"Store": {}}}

    def test_a_valid_mode_passes(self):
        self.assertEqual(validate(self.base({"mode": "strict", "Admin": [{"type": "Group", "id": GROUP}]})), [])

    def test_an_unknown_mode_is_rejected_with_the_options(self):
        with self.assertRaises(RecipeError) as ctx:
            validate(self.base({"mode": "authoritative"}))
        self.assertIn("additive, strict", str(ctx.exception))

    def test_a_role_must_be_a_list(self):
        with self.assertRaises(RecipeError) as ctx:
            validate(self.base({"Admin": GROUP}))
        self.assertIn("list of principals", str(ctx.exception))


# ------------------------------------------------------------------ the plan side
class PlanTests(unittest.TestCase):
    def test_every_layer_gets_a_roles_check_after_its_role_assignments(self):
        plan = build_plan(make_recipe({"permissions": {"Admin": [{"type": "Group", "id": GROUP}]}}, {"Store": {}}))
        action = roles_action(plan)
        self.assertEqual(action.mode, "additive")
        self.assertEqual(action.declared, ("role:Store:admin:0",))
        self.assertIn("role:Store:admin:0", action.depends_on)
        self.assertIn("workspace:Store", action.depends_on)

    def test_a_layer_overrides_the_default_mode(self):
        plan = build_plan(make_recipe(
            {"permissions": {"mode": "strict", "Admin": [{"type": "Group", "id": GROUP}]}},
            {"Store": {}, "Present": {"permissions": {"mode": "additive"}}},
        ))
        self.assertEqual(roles_action(plan, "Store").mode, "strict")
        self.assertEqual(roles_action(plan, "Present").mode, "additive")

    def test_mode_is_not_mistaken_for_a_role(self):
        plan = build_plan(make_recipe({"permissions": {"mode": "strict", "Admin": [{"type": "Group", "id": GROUP}]}}, {"Store": {}}))
        self.assertFalse([a for a in plan.actions if a.kind == "role" and "mode" in a.id])


class FeaturePlanTests(unittest.TestCase):
    """The feature recipe has a mode of its own; it does not inherit the platform's."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        env = pathlib.Path(self.tmp.name) / "environments"; env.mkdir()
        self.env = env

    def load(self, permissions):
        (self.env / "feature.json").write_text(json.dumps({
            "feature_name": "*Demo {feature_name} ({layer_name})",
            "capacity_name": "cap",
            "permissions": permissions,
            "layers": {"Prepare": {"git_directoryName": "solution/engineering/prepare"}},
        }))
        return recipe_module.load_feature(self.tmp.name, developer="peer")

    def test_defaults_to_additive(self):
        plan = build_feature_plan(self.load({"Admin": [{"type": "Group", "id": GROUP}]}), branch="feature/prepare/x")
        self.assertEqual(roles_action(plan, "Prepare").mode, "additive")

    def test_strict_declares_the_group_and_the_developer(self):
        plan = build_feature_plan(
            self.load({"mode": "strict", "Admin": [{"type": "Group", "id": GROUP}]}),
            branch="feature/prepare/x", developer_object_id=ME,
        )
        action = roles_action(plan, "Prepare")
        self.assertEqual(action.mode, "strict")
        self.assertEqual(set(action.declared), {"role:Prepare:Admin:0", "role:Prepare:developer"})


# ------------------------------------------------------------------ the tenant side
class _Buffer:
    def __init__(self):
        self.lines: list[str] = []

    def write(self, text):
        self.lines.append(text)

    def flush(self):
        pass


class ExecuteTests(unittest.TestCase):
    def setUp(self):
        self.fab = FakeFab()
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="fabricops-perm-"))
        self.console = _Buffer()
        self.log = RunLog(level=Level.DEBUG, stream=self.console, _colour=False)

    def tearDown(self):
        self.log.close()
        self.fab.cleanup()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def existing_workspace(self, acl, *, me=ME):
        self.fab.add(["exists"], stdout="* true", command="exists")
        self.fab.add(["acl get"], stdout=json.dumps(acl))
        self.fab.add(["acl set"], stdout="ok")
        self.fab.add(["acl rm"], stdout="ok")
        self.fab.add(["get"], stdout="ws-id-1", command="get")
        if me:
            self.fab.add(["auth status"], stdout=json.dumps({"principal_id": me, "logged_in": True}))
        else:
            self.fab.add(["auth status"], stdout="", returncode=1, stderr="not logged in")

    def setup_with(self, mode, *, dry_run=False):
        recipe = make_recipe({"permissions": {"mode": mode, "Admin": [{"type": "Group", "id": GROUP}]}}, {"Store": {}})
        cli = FabricCli(self.log, executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY, dry_run=dry_run)
        ctx = RunContext(cli=cli, log=self.log, recipe=recipe, dry_run=dry_run)
        report = execute(build_plan(recipe), ctx, manifest=Manifest(run_id="t", dry_run=dry_run))
        return report, ctx, cli

    def removed(self):
        return [c for c in self.fab.commands if c.startswith("acl rm")]

    ACL = [
        {"id": GROUP, "type": "Group", "role": "Admin"},
        {"id": STRANGER, "type": "User", "role": "Member"},
        {"id": ME, "type": "ServicePrincipal", "role": "Admin"},
    ]

    def test_additive_keeps_the_stranger_and_says_so(self):
        self.existing_workspace(self.ACL)
        report, ctx, _ = self.setup_with("additive")
        self.assertEqual(self.removed(), [])
        self.assertEqual(ctx.output("roles:Store", "kept"), [STRANGER, ME])
        self.assertIn("not in the recipe, kept", "".join(self.console.lines))
        self.assertFalse(report.failures)

    def test_strict_removes_the_stranger_and_nobody_else(self):
        self.existing_workspace(self.ACL)
        _, ctx, _ = self.setup_with("strict")
        self.assertEqual(len(self.removed()), 1)
        self.assertIn(STRANGER, self.removed()[0])
        self.assertEqual(ctx.output("roles:Store", "removed"), [STRANGER])
        self.assertEqual(ctx.output("roles:Store", "kept"), [ME])

    def test_strict_never_removes_the_identity_running_the_setup(self):
        self.existing_workspace([{"id": GROUP, "type": "Group", "role": "Admin"}, {"id": ME, "type": "ServicePrincipal", "role": "Admin"}])
        report, ctx, _ = self.setup_with("strict")
        self.assertEqual(self.removed(), [])
        self.assertEqual(ctx.output("roles:Store", "kept"), [ME])
        # the workspace, the group's role, and the roles check: all already as declared
        self.assertEqual(report.counts.get("existed"), 3)

    def test_strict_removes_nothing_when_it_cannot_tell_who_it_is(self):
        self.existing_workspace(self.ACL, me=None)
        report, ctx, _ = self.setup_with("strict")
        self.assertEqual(self.removed(), [])
        self.assertEqual(report.counts.get("skipped"), 1)
        self.assertIn("could not determine the identity", "".join(self.console.lines))

    def test_strict_dry_run_reports_what_it_would_remove_and_writes_nothing(self):
        self.existing_workspace(self.ACL)
        report, _, cli = self.setup_with("strict", dry_run=True)
        self.assertEqual(self.removed(), [], "a dry run never issues acl rm")
        self.assertTrue(any("acl rm" in w and STRANGER in w for w in cli.skipped_writes))
        self.assertEqual(report.counts.get("updated"), 1)
        self.assertIn("would remove", "".join(self.console.lines))

    def test_a_workspace_created_this_run_is_not_read_at_all(self):
        self.fab.add(["exists"], stdout="false", command="exists")
        self.fab.add(["mkdir"], stdout="created")
        self.fab.add(["acl set"], stdout="ok")
        self.fab.add(["get"], stdout="ws-id-1", command="get")
        self.setup_with("strict")
        # One read, and it is the role assignment checking before it writes. The roles
        # check adds none: a workspace this run created cannot hold anything undeclared.
        self.assertEqual(len([c for c in self.fab.commands if c.startswith("acl get")]), 1)


if __name__ == "__main__":
    unittest.main()
