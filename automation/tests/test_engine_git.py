"""Git integration: one implementation for the platform and feature flows."""

import json
import unittest

from fabricops.engine import Manifest, RunContext, build_plan, execute
from fabricops.engine.connections import Credentials
from fabricops.engine.git import provider_details
from fabricops.errors import FabricOpsError
from fabricops.fabric.cli import NO_RETRY, FabricCli
from fabricops.obs.logging import Level, RunLog
from fabricops.obs.redaction import Secret
from fabricops.recipe import Recipe
from support import FakeFab

GIT = {
    "provider": "GitHub",
    "owner": "gronnerup",
    "repository": "FabricOps",
    "branch": "main",
    "credentials": {"source": "ConfiguredConnection", "connection": "FabricOps-GitHub"},
}


class ProviderDetailTests(unittest.TestCase):
    def test_github_details(self):
        details = provider_details(GIT, directory="solution/store")
        self.assertEqual(details["gitProviderType"], "GitHub")
        self.assertEqual(details["ownerName"], "gronnerup")
        self.assertEqual(details["directoryName"], "solution/store")
        self.assertEqual(details["branchName"], "main")

    def test_azure_devops_details(self):
        git = {"provider": "AzureDevOps", "organization": "org", "project": "proj", "repository": "repo", "branch": "main"}
        details = provider_details(git, directory="solution/store")
        self.assertEqual(details["organizationName"], "org")
        self.assertEqual(details["projectName"], "proj")

    def test_missing_settings_are_named(self):
        with self.assertRaises(FabricOpsError) as ctx:
            provider_details({"provider": "GitHub", "owner": "o", "repository": "r"})
        message = str(ctx.exception)
        self.assertIn("branchName", message)
        self.assertIn("directoryName", message)

    def test_branch_override_wins(self):
        details = provider_details(GIT, branch="feature/peer/add-orders", directory="solution/store")
        self.assertEqual(details["branchName"], "feature/peer/add-orders")


class ConnectGitTests(unittest.TestCase):
    def setUp(self):
        self.fab = FakeFab()
        self.console = _Buffer()
        self.log = RunLog(level=Level.DEBUG, stream=self.console, _colour=False)

    def tearDown(self):
        self.fab.cleanup()

    def run_plan(self, layer_git: dict, *, dry_run: bool = False, references: tuple = ()):
        recipe = Recipe(
            data={
                "display_name_pattern": "Demo - {layer} [{environment}]",
                "defaults": {"capacity": "Trial-01", "git": GIT},
                "layers": {"Store": {"git": layer_git}},
            },
            sources=(),
            environment="dev",
            solution="demo",
        )
        cli = FabricCli(self.log, executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY, dry_run=dry_run)
        credentials = Credentials(
            tenant_id="t-1",
            client_id="c-1",
            client_secret=Secret("client-secret-value-1", "client_secret"),
            github_pat=Secret("ghp_gittesttokenabcdefghijkl", "github_pat"),
        )
        ctx = RunContext(
            cli=cli, log=self.log, recipe=recipe, dry_run=dry_run, credentials=credentials, sleep=lambda _s: None
        )
        plan = build_plan(recipe)
        if references:
            # The planner derives these from the recipe's `references:` block; injecting them
            # keeps the guard's own tests independent of that resolution.
            for action in plan:
                if action.kind == "git":
                    action.references = references
        report = execute(plan, ctx, manifest=Manifest(run_id="t"))
        return report, ctx

    def api(self, fragment, payload, status=200, headers=None):
        self.fab.add_json(["api", fragment], {"status_code": status, "text": payload, "headers": headers or {}})

    def base_responses(self):
        self.fab.add(["exists"], stdout="false", command="exists")
        self.fab.add(["mkdir"], stdout="created", command="mkdir")
        self.fab.add(["get"], stdout="ws-1", command="get")

    def test_connect_then_initialize_then_update(self):
        self.base_responses()
        self.api("git/connection", {"gitConnectionState": "NotConnected"})
        self.api("git/connect", {})
        self.api("git/initializeConnection", {"requiredAction": "UpdateFromGit", "remoteCommitHash": "abc123"})
        self.api("git/updateFromGit", {})

        report, _ctx = self.run_plan({"directory": "solution/store"})

        calls = [c for c in self.fab.commands if c.startswith("api")]
        self.assertTrue(any("git/connect " in c for c in calls))
        self.assertTrue(any("git/initializeConnection" in c for c in calls))
        update = next(c for c in calls if "git/updateFromGit" in c)
        self.assertIn("abc123", update)
        self.assertIn("PreferRemote", update)
        self.assertEqual(report.exit_code, 0)

    def test_initialize_with_nothing_to_do_skips_the_update(self):
        self.base_responses()
        self.api("git/connection", {"gitConnectionState": "NotConnected"})
        self.api("git/connect", {})
        self.api("git/initializeConnection", {"requiredAction": "None"})

        self.run_plan({"directory": "solution/store"})
        self.assertFalse([c for c in self.fab.commands if "updateFromGit" in c])

    def test_already_connected_without_sync_is_left_alone(self):
        self.base_responses()
        self.api("git/connection", {"gitConnectionState": "ConnectedAndInitialized"})

        report, _ctx = self.run_plan({"directory": "solution/store"})

        self.assertFalse([c for c in self.fab.commands if "git/connect " in c], "must not reconnect")
        git_action = next(a for a in report.manifest.actions if a["kind"] == "git")
        self.assertEqual(git_action["status"], "existed")

    def test_sync_on_commit_updates_an_already_connected_workspace(self):
        self.base_responses()
        self.api("git/connection", {"gitConnectionState": "ConnectedAndInitialized"})
        self.api("git/status", {"remoteCommitHash": "remote-1", "workspaceHead": "local-0"})
        self.api("git/updateFromGit", {})

        report, _ctx = self.run_plan({"directory": "solution/store", "sync_on_commit": True})

        update = next(c for c in self.fab.commands if "updateFromGit" in c)
        self.assertIn("remote-1", update)
        self.assertIn("local-0", update, "workspaceHead must be sent when syncing an existing connection")
        git_action = next(a for a in report.manifest.actions if a["kind"] == "git")
        self.assertEqual(git_action["status"], "updated")

    def test_connected_but_not_initialised_is_initialised_not_reconnected(self):
        """What a run that connected and then failed during updateFromGit leaves behind."""
        self.base_responses()
        self.api("git/connection", {"gitConnectionState": "Connected"})
        self.api("git/initializeConnection", {"requiredAction": "None"})

        report, _ctx = self.run_plan({"directory": "solution/store"})

        self.assertFalse(
            [c for c in self.fab.commands if "git/connect " in c],
            "reconnecting an already-connected workspace returns 409",
        )
        self.assertTrue([c for c in self.fab.commands if "initializeConnection" in c])
        git_action = next(a for a in report.manifest.actions if a["kind"] == "git")
        self.assertEqual(git_action["status"], "updated")

    def test_every_connected_state_avoids_reconnecting(self):
        from fabricops.engine.git import _is_connected, _is_initialized

        self.assertFalse(_is_connected("NotConnected"))
        self.assertFalse(_is_connected(None))
        self.assertTrue(_is_connected("Connected"))
        self.assertTrue(_is_connected("ConnectedAndInitialized"))
        self.assertFalse(_is_initialized("Connected"))
        self.assertTrue(_is_initialized("ConnectedAndInitialized"))

    def test_sync_on_commit_when_already_up_to_date_does_nothing(self):
        self.base_responses()
        self.api("git/connection", {"gitConnectionState": "ConnectedAndInitialized"})
        self.api("git/status", {"remoteCommitHash": "same", "workspaceHead": "same"})

        report, _ctx = self.run_plan({"directory": "solution/store", "sync_on_commit": True})
        self.assertFalse([c for c in self.fab.commands if "updateFromGit" in c])
        git_action = next(a for a in report.manifest.actions if a["kind"] == "git")
        self.assertEqual(git_action["status"], "existed")

    def test_disconnect_after_initialize(self):
        self.base_responses()
        self.api("git/connection", {"gitConnectionState": "NotConnected"})
        self.api("git/connect", {})
        self.api("git/initializeConnection", {"requiredAction": "None"})
        self.api("git/disconnect", {})

        report, _ctx = self.run_plan({"directory": "solution/model", "disconnect_after_initialize": True})

        self.assertTrue([c for c in self.fab.commands if "git/disconnect" in c])
        git_action = next(a for a in report.manifest.actions if a["kind"] == "git")
        self.assertIn("disconnected", git_action["message"])

    def test_a_layer_without_a_directory_is_not_connected(self):
        self.base_responses()
        report, _ctx = self.run_plan({})
        kinds = [action["kind"] for action in report.manifest.actions]
        self.assertNotIn("git", kinds)
        self.assertIn("workspace", kinds)

    def test_the_source_control_connection_is_created_before_connecting(self):
        self.base_responses()
        self.api("git/connection", {"gitConnectionState": "NotConnected"})
        self.api("git/connect", {})
        self.api("git/initializeConnection", {"requiredAction": "None"})

        report, _ctx = self.run_plan({"directory": "solution/store"})

        kinds = [action["kind"] for action in report.manifest.actions]
        self.assertLess(kinds.index("connection"), kinds.index("git"))
        connection = next(c for c in self.fab.commands if c.startswith("mkdir .connections/"))
        self.assertIn("connectionDetails.type=GitHubSourceControl", connection)
        self.assertIn("credentialDetails.key=***", connection.replace("ghp_gittesttokenabcdefghijkl", "***"))

    def test_dry_run_makes_no_git_calls_but_plans_them(self):
        self.fab.add(["exists"], stdout="false")
        report, _ctx = self.run_plan({"directory": "solution/store"}, dry_run=True)
        self.assertFalse([c for c in self.fab.commands if "git/connect" in c])
        git_action = next(a for a in report.manifest.actions if a["kind"] == "git")
        self.assertEqual(git_action["status"], "created")


class _Buffer:
    def __init__(self):
        self.chunks: list[str] = []

    def write(self, text: str) -> int:
        self.chunks.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    @property
    def text(self) -> str:
        return "".join(self.chunks)


if __name__ == "__main__":
    unittest.main()


class GitStatusTests(unittest.TestCase):
    """Divergence is two-directional, and the directions are not symmetric."""

    def status(self, **kwargs):
        from fabricops.engine.git import GitStatus

        return GitStatus(**kwargs)

    def test_matching_hashes_and_no_changes_is_up_to_date(self):
        state = self.status(remote="a", head="a")
        self.assertFalse(state.behind)
        self.assertFalse(state.ahead)
        self.assertEqual(state.summary(), "up to date with git")

    def test_a_moved_remote_means_behind(self):
        state = self.status(remote="b", head="a")
        self.assertTrue(state.behind)
        self.assertIn("behind git", state.summary())

    def test_workspace_changes_mean_ahead(self):
        """Someone edited in the portal; pulling would discard it."""
        state = self.status(remote="a", head="a", workspace_changes=["Curated"])
        self.assertTrue(state.ahead)
        self.assertFalse(state.behind)
        self.assertIn("1 uncommitted workspace change", state.summary())

    def test_both_directions_are_reported(self):
        state = self.status(remote="b", head="a", workspace_changes=["Curated", "Base"])
        self.assertTrue(state.behind and state.ahead)
        self.assertIn("behind git", state.summary())
        self.assertIn("2 uncommitted", state.summary())


class SyncBehaviourTests(ConnectGitTests):
    def changes(self, *names, workspace=True, conflict="None"):
        return [
            {
                "workspaceChange": "Modified" if workspace else None,
                "remoteChange": None if workspace else "Modified",
                "conflictType": conflict,
                "itemMetadata": {"displayName": name, "itemType": "Lakehouse"},
            }
            for name in names
        ]

    def test_status_is_read_even_when_sync_is_off(self):
        """A run that does not look can only say 'connected', which reads as 'fine'."""
        self.base_responses()
        self.api("git/connection", {"gitConnectionState": "ConnectedAndInitialized"})
        self.api("git/status", {"remoteCommitHash": "remote-1", "workspaceHead": "local-0"})

        report, _ctx = self.run_plan({"directory": "solution/store"})

        self.assertTrue([c for c in self.fab.commands if "git/status" in c], "status must be read")
        self.assertFalse([c for c in self.fab.commands if "updateFromGit" in c], "but nothing pulled")
        action = next(a for a in report.manifest.actions if a["kind"] == "git")
        self.assertIn("behind git", action["message"])
        self.assertIn("sync_on_commit is off", action["message"])

    def test_uncommitted_workspace_changes_are_named_when_discarded(self):
        self.base_responses()
        self.api("git/connection", {"gitConnectionState": "ConnectedAndInitialized"})
        self.api("git/status", {
            "remoteCommitHash": "remote-1", "workspaceHead": "local-0",
            "changes": self.changes("Curated", "Base"),
        })
        self.api("git/updateFromGit", {})

        report, _ctx = self.run_plan({"directory": "solution/store", "sync_on_commit": True})

        action = next(a for a in report.manifest.actions if a["kind"] == "git")
        self.assertEqual(action["status"], "updated")
        self.assertIn("overwriting 2 workspace change(s)", action["message"])

    def test_a_conflict_defaults_to_git_winning_and_says_so(self):
        """Fabric normalises items on import, so a conflict is the routine state in dev."""
        self.base_responses()
        self.api("git/connection", {"gitConnectionState": "ConnectedAndInitialized"})
        self.api("git/status", {
            "remoteCommitHash": "remote-1", "workspaceHead": "local-0",
            "changes": self.changes("Curated", conflict="Conflict"),
        })
        self.api("git/updateFromGit", {})

        report, _ctx = self.run_plan({"directory": "solution/store", "sync_on_commit": True})

        update = next(c for c in self.fab.commands if "updateFromGit" in c)
        self.assertIn("PreferRemote", update)
        action = next(a for a in report.manifest.actions if a["kind"] == "git")
        self.assertIn("overwriting 1 workspace change(s)", action["message"])

    def test_conflict_resolution_stop_pulls_nothing(self):
        self.base_responses()
        self.api("git/connection", {"gitConnectionState": "ConnectedAndInitialized"})
        self.api("git/status", {
            "remoteCommitHash": "remote-1", "workspaceHead": "local-0",
            "changes": self.changes("Curated", conflict="Conflict"),
        })

        report, _ctx = self.run_plan(
            {"directory": "solution/store", "sync_on_commit": True, "conflict_resolution": "stop"}
        )

        self.assertFalse([c for c in self.fab.commands if "updateFromGit" in c])
        action = next(a for a in report.manifest.actions if a["kind"] == "git")
        self.assertIn("conflicted", action["message"])

    def test_prefer_workspace_is_passed_through(self):
        self.base_responses()
        self.api("git/connection", {"gitConnectionState": "ConnectedAndInitialized"})
        self.api("git/status", {"remoteCommitHash": "remote-1", "workspaceHead": "local-0"})
        self.api("git/updateFromGit", {})

        self.run_plan(
            {"directory": "solution/store", "sync_on_commit": True,
             "conflict_resolution": "prefer_workspace"}
        )
        update = next(c for c in self.fab.commands if "updateFromGit" in c)
        self.assertIn("PreferWorkspace", update)


class ReferenceGuardTests(ConnectGitTests):
    """A layer whose committed references cannot resolve is deferred, not failed.

    Fabric answers `DiscoverDependenciesFailed` with nothing useful in it. Item ids are
    per-tenant, so a repository cannot ship ones that resolve anywhere else - which makes
    this a bootstrap state rather than a broken deployment.
    """

    def reference(self, committed, *, blocks=True):
        import json as _json
        import pathlib as _pathlib
        import tempfile as _tempfile

        from fabricops.engine.references import Reference, Target

        root = _pathlib.Path(_tempfile.mkdtemp(prefix="fabricops-guard-"))
        file = root / "definition.pbir"
        file.write_text(_json.dumps({"model": committed}))
        return Reference(
            file=file, target=Target("Model", "Rebrickable", "SemanticModel"),
            json_path="model", label="report: semantic model", blocks_sync=blocks,
        )

    def test_a_target_that_does_not_exist_defers_the_pull(self):
        self.fab.add([".SemanticModel"], stdout="false", command="exists")
        self.base_responses()
        self.api("git/connection", {"gitConnectionState": "ConnectedAndInitialized"})

        report, _ctx = self.run_plan(
            {"directory": "solution/present", "sync_on_commit": True},
            references=(self.reference("11111111-1111-1111-1111-111111111111"),),
        )

        action = next(a for a in report.manifest.actions if a["kind"] == "git")
        self.assertEqual(action["status"], "skipped")
        self.assertIn("waiting for", action["message"])
        self.assertFalse([c for c in self.fab.commands if "updateFromGit" in c])
        self.assertEqual(report.exit_code, 0, "a bootstrap state is not a failure")

    def test_a_stale_committed_id_names_the_command_that_fixes_it(self):
        self.fab.add([".SemanticModel"], stdout="true", command="exists")
        self.fab.add([".SemanticModel"], stdout="99999999-9999-9999-9999-999999999999", command="get")
        self.base_responses()
        self.api("git/connection", {"gitConnectionState": "ConnectedAndInitialized"})

        report, _ctx = self.run_plan(
            {"directory": "solution/present", "sync_on_commit": True},
            references=(self.reference("11111111-1111-1111-1111-111111111111"),),
        )

        action = next(a for a in report.manifest.actions if a["kind"] == "git")
        self.assertEqual(action["status"], "skipped")
        self.assertIn("references sync", action["message"])
        self.assertIn("11111111", action["message"])
        self.assertIn("99999999", action["message"])

    def test_a_matching_id_does_not_defer(self):
        self.fab.add([".SemanticModel"], stdout="true", command="exists")
        self.fab.add([".SemanticModel"], stdout="11111111-1111-1111-1111-111111111111", command="get")
        self.base_responses()
        self.api("git/connection", {"gitConnectionState": "ConnectedAndInitialized"})
        self.api("git/status", {"remoteCommitHash": "same", "workspaceHead": "same"})

        report, _ctx = self.run_plan(
            {"directory": "solution/present", "sync_on_commit": True},
            references=(self.reference("11111111-1111-1111-1111-111111111111"),),
        )
        action = next(a for a in report.manifest.actions if a["kind"] == "git")
        self.assertEqual(action["status"], "existed")

    def test_a_reference_that_does_not_block_sync_is_ignored(self):
        """A semantic model's M expression is opaque text; Fabric syncs it regardless."""
        self.base_responses()
        self.api("git/connection", {"gitConnectionState": "ConnectedAndInitialized"})
        self.api("git/status", {"remoteCommitHash": "same", "workspaceHead": "same"})

        report, _ctx = self.run_plan(
            {"directory": "solution/model", "sync_on_commit": True},
            references=(self.reference("11111111-1111-1111-1111-111111111111", blocks=False),),
        )
        action = next(a for a in report.manifest.actions if a["kind"] == "git")
        self.assertEqual(action["status"], "existed")


class RelationReasonTests(unittest.TestCase):
    """A relation that could not be created must say why.

    There is no API that lists workspace relations, so the response body is the only source
    of an explanation - and a generic `BadRequest`, which the documented code list does not
    include, is explained by nothing else.
    """

    def reason(self, body, code):
        from fabricops.engine.git import _relation_reason

        return _relation_reason(body, code)

    def test_the_api_message_is_used(self):
        reason = self.reason(
            {"errorCode": "BadRequest", "message": "The workspace is not connected to Git."},
            "BadRequest",
        )
        self.assertEqual(reason, "The workspace is not connected to Git.")

    def test_more_details_are_included(self):
        reason = self.reason(
            {
                "message": "Relation could not be created.",
                "moreDetails": [{"message": "Root directories differ."}],
            },
            "BadRequest",
        )
        self.assertIn("Relation could not be created.", reason)
        self.assertIn("Root directories differ.", reason)

    def test_a_known_code_adds_our_hint_to_the_message(self):
        reason = self.reason(
            {"message": "Cannot create relation."}, "WorkspaceRelationRootDirectoryMismatch"
        )
        self.assertIn("Cannot create relation.", reason)
        self.assertIn("same git root directory", reason)

    def test_a_known_code_with_no_message_still_explains_itself(self):
        self.assertEqual(
            self.reason({}, "WorkspaceRelationTargetHasBranches"),
            "the base workspace already has branch workspaces",
        )

    def test_an_unknown_code_with_no_message_falls_back(self):
        self.assertIn("Create Workspace Relation", self.reason({}, "Whatever"))

    def test_the_generic_hint_is_not_appended_to_a_real_message(self):
        # "see the documentation" after an actual explanation is noise.
        self.assertEqual(self.reason({"message": "Nope."}, "Whatever"), "Nope.")


class InitializationStrategyTests(ConnectGitTests):
    """`initializeConnection` needs a strategy once both sides have content.

    A freshly created workspace has none, so a layer's first run initialises without one and
    the second fails with MissingInitializationStrategy - by then the workspace holds what
    the first run pulled. This is the decision `conflict_resolution` already makes.
    """

    def initialize_calls(self):
        return [c for c in self.fab.commands if "git/initializeConnection" in c]

    def connect_fresh(self, git, **kw):
        self.base_responses()
        self.api("git/connection", {"gitConnectionState": "NotConnected"})
        self.api("git/connect", {})
        self.api("git/initializeConnection", {"requiredAction": "None"})
        return self.run_plan({"directory": "solution/store", **git}, **kw)

    def test_prefer_remote_is_sent_by_default(self):
        self.connect_fresh({})
        self.assertIn("PreferRemote", self.initialize_calls()[0])

    def test_prefer_workspace_is_honoured(self):
        self.connect_fresh({"conflict_resolution": "prefer_workspace"})
        self.assertIn("PreferWorkspace", self.initialize_calls()[0])

    def test_stop_sends_no_strategy(self):
        self.connect_fresh({"conflict_resolution": "stop"})
        call = self.initialize_calls()[0]
        self.assertNotIn("PreferRemote", call)
        self.assertNotIn("PreferWorkspace", call)

    def test_the_refusal_explains_itself(self):
        self.base_responses()
        self.api("git/connection", {"gitConnectionState": "NotConnected"})
        self.api("git/connect", {})
        self.api(
            "git/initializeConnection",
            {"errorCode": "MissingInitializationStrategy",
             "message": "The requested operation requires an initialization strategy."},
            status=400,
        )
        report, _ctx = self.run_plan(
            {"directory": "solution/store", "conflict_resolution": "stop"}
        )
        self.assertTrue(report.failures)
        message = str(report.failures[0][1])
        self.assertIn("will not choose which one wins", message)
        self.assertIn("conflict_resolution", message)


class SyncFailureExplanationTests(unittest.TestCase):
    """A missing artifact means the *deployed* item is stale, not the repository."""

    def explain(self, text):
        from fabricops.engine.git import _explain_sync_failure
        from fabricops.errors import FabricOpsError

        return _explain_sync_failure(FabricOpsError(text))

    def test_the_id_is_named_and_the_cause_explained(self):
        error = self.explain(
            "GET operations/x returned 200: [GitSyncFailed] Dataset Workload failed to import "
            "the dataset with dataset id df25eaef-4915-4f52-bbdb-aa306a80157b. Error returned: "
            "'The Fabric artifact '00000000-0000-0000-0000-00000000a002' is not found'"
        )
        self.assertIn("00000000-0000-0000-0000-00000000a002", str(error))
        self.assertIn("deployed item", str(error))
        self.assertIn("Delete the item", str(error))

    def test_it_points_at_references_sync_for_a_placeholder(self):
        error = self.explain("The Fabric artifact '00000000-0000-0000-0000-00000000a002' is not found")
        self.assertIn("references sync", str(error))

    def test_an_unrelated_failure_is_passed_through_unchanged(self):
        original = "GET operations/x returned 200: [GitSyncFailed] something else entirely"
        self.assertEqual(str(self.explain(original)), original)


class DisconnectConvergenceTests(ConnectGitTests):
    """A layer that declares `disconnect_after_initialize` must not stay connected.

    The disconnect only ran on the fresh-connect path, so a run that failed after connecting
    left the layer connected for good - and every run after it took the sync path and
    reported "behind git, nothing pulled" as though that were the intent.
    """

    GIT = {"directory": "solution/store", "disconnect_after_initialize": True, "sync_on_commit": False}

    def disconnects(self):
        return [c for c in self.fab.commands if "git/disconnect" in c]

    def test_a_fresh_connect_still_disconnects(self):
        self.base_responses()
        self.api("git/connection", {"gitConnectionState": "NotConnected"})
        self.api("git/connect", {})
        self.api("git/initializeConnection", {"requiredAction": "None"})
        self.api("git/disconnect", {})
        report, _ctx = self.run_plan(dict(self.GIT))
        self.assertEqual(len(self.disconnects()), 1)
        self.assertEqual(report.exit_code, 0)

    def test_a_layer_left_connected_and_initialised_is_disconnected(self):
        self.base_responses()
        self.api("git/connection", {"gitConnectionState": "ConnectedAndInitialized"})
        self.api("git/disconnect", {})
        report, _ctx = self.run_plan(dict(self.GIT))
        self.assertEqual(len(self.disconnects()), 1)
        self.assertEqual(report.exit_code, 0)

    def test_a_layer_left_connected_but_uninitialised_initialises_then_disconnects(self):
        self.base_responses()
        self.api("git/connection", {"gitConnectionState": "Connected"})
        self.api("git/initializeConnection", {"requiredAction": "None"})
        self.api("git/disconnect", {})
        self.run_plan(dict(self.GIT))
        self.assertTrue([c for c in self.fab.commands if "initializeConnection" in c])
        self.assertEqual(len(self.disconnects()), 1)

    def test_a_dry_run_disconnects_nothing(self):
        self.base_responses()
        self.api("git/connection", {"gitConnectionState": "ConnectedAndInitialized"})
        self.run_plan(dict(self.GIT), dry_run=True)
        self.assertEqual(self.disconnects(), [])

    def test_a_layer_without_the_flag_is_left_connected(self):
        self.base_responses()
        self.api("git/connection", {"gitConnectionState": "ConnectedAndInitialized"})
        self.api("git/status", {"workspaceHead": "a", "remoteCommitHash": "a", "changes": []})
        self.run_plan({"directory": "solution/store", "sync_on_commit": False})
        self.assertEqual(self.disconnects(), [])


class MigrationInProgressTests(unittest.TestCase):
    """A workspace being migrated is a platform state, not a deployment problem."""

    def explain(self, text):
        from fabricops.engine.git import _explain_sync_failure
        from fabricops.errors import FabricOpsError

        return _explain_sync_failure(FabricOpsError(text))

    def test_it_says_the_deployment_is_not_at_fault(self):
        error = self.explain(
            "POST workspaces/63a7cc96/git/updateFromGit returned 400: "
            "[WorkspaceMigrationOperationInProgress] A workspace migration operation is in "
            "progress.  (requestId a504df1a-369c-44c9-888c-99957a154d5f)"
        )
        self.assertIn("migrating this workspace", str(error))
        self.assertIn("not caused by your deployment", str(error))

    def test_it_says_to_run_again(self):
        error = self.explain("[WorkspaceMigrationOperationInProgress] A workspace migration is in progress.")
        self.assertIn("run again", str(error))

    def test_a_migration_is_distinguished_from_a_stale_artifact(self):
        migration = self.explain("[WorkspaceMigrationOperationInProgress] x")
        stale = self.explain("The Fabric artifact '00000000-0000-0000-0000-00000000a002' is not found")
        self.assertNotIn("Delete the item", str(migration))
        self.assertNotIn("migrating", str(stale))
