"""How actions name themselves in the log and the manifest."""

import unittest

from fabricops.engine.actions import AssignRole, CreateWorkspace

class RoleLabelTests(unittest.TestCase):
    """Two admins on one layer must not print as one action twice."""

    def test_a_group_is_named_by_its_id(self):
        action = AssignRole(
            id="role",
            workspace="ws", role="admin", principal_id="11111111-1111-1111-1111-111111111111"
        )
        self.assertEqual(action.describe(), "Role admin for Group 11111111")

    def test_a_workspace_identity_is_named_by_its_workspace(self):
        action = AssignRole(
            id="role",
            workspace="ws",
            role="admin",
            principal_type="WorkspaceIdentity",
            principal_workspace="Demo - Orchestrate [dev]",
        )
        self.assertEqual(
            action.describe(), "Role admin for the identity of 'Demo - Orchestrate [dev]'"
        )

    def test_the_two_admins_of_one_layer_read_differently(self):
        group = AssignRole(id="role-group", workspace="Model", role="admin", principal_id="11111111-34ad")
        identity = AssignRole(
            id="role-identity",
            workspace="Model",
            role="admin",
            principal_type="WorkspaceIdentity",
            principal_workspace="Demo - Orchestrate [dev]",
        )
        self.assertNotEqual(group.describe(), identity.describe())

    def test_an_unresolved_identity_still_says_which_kind(self):
        action = AssignRole(id="role", workspace="ws", role="admin", identity_from="ws-identity-orchestrate")
        self.assertEqual(action.describe(), "Role admin for a workspace identity")


class IncidentalWorkspaceTests(unittest.TestCase):
    """A workspace kept only as a dependency is read, never created.

    `--only git` keeps each layer's workspace action because the git action reads its id.
    On a fresh tenant that action used to *create* the workspace: no roles, no identity,
    no settings, the service principal as sole admin, invisible to everyone else. The dev
    sync on every merge did exactly that, seven times, after a teardown.
    """

    class Cli:
        def __init__(self, exists):
            self._exists, self.created = exists, []
        def exists(self, path): return self._exists
        def get_value(self, path, key): return "ws-1"
        def mkdir(self, path, params=None): self.created.append(str(path))

    class Ctx:
        def __init__(self, cli): self.cli, self.dry_run = cli, False

    def test_an_existing_workspace_is_read_as_usual(self):
        cli = self.Cli(exists=True)
        result = CreateWorkspace(id="w", kind="workspace", workspace="Demo - Store [dev]", incidental=True).apply(self.Ctx(cli))
        self.assertEqual(result.status, "existed")
        self.assertEqual(cli.created, [])

    def test_a_missing_workspace_fails_instead_of_being_created(self):
        from fabricops.errors import FabricOpsError

        cli = self.Cli(exists=False)
        action = CreateWorkspace(id="w", kind="workspace", workspace="Demo - Store [dev]", incidental=True)
        with self.assertRaises(FabricOpsError) as raised:
            action.apply(self.Ctx(cli))
        self.assertIn("does not create workspaces", str(raised.exception))
        self.assertIn("fabricops setup", str(raised.exception))
        self.assertEqual(cli.created, [], "a sync must not create")

    def test_a_regular_setup_still_creates(self):
        cli = self.Cli(exists=False)
        result = CreateWorkspace(id="w", kind="workspace", workspace="Demo - Store [dev]").apply(self.Ctx(cli))
        self.assertEqual(result.status, "created")
        self.assertEqual(len(cli.created), 1)
