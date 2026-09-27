"""Git integration actions.

One implementation, shared by the platform and feature flows - today's scripts carry two
copies of connect/initialize/update, with slightly different retry behaviour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..errors import FabricApiError, FabricOpsError
from .actions import Action, ActionResult

if TYPE_CHECKING:  # pragma: no cover
    from .context import RunContext


#: Fabric reports `NotConnected`, `Connected` (connected but not yet initialised) or
#: `ConnectedAndInitialized`. Matching `Connected` exactly missed the initialised case, so
#: every re-run tried to connect again and got 409 WorkspaceAlreadyConnectedToGit.
def _is_connected(state: str | None) -> bool:
    return bool(state) and str(state) != "NotConnected"


def _is_initialized(state: str | None) -> bool:
    return "initialized" in str(state or "").lower()


def provider_details(git: dict[str, Any], *, branch: str | None = None, directory: str | None = None) -> dict[str, Any]:
    """The `gitProviderDetails` payload the REST API expects, from a canonical git node."""
    provider = str(git.get("provider") or "")
    details: dict[str, Any] = {"gitProviderType": provider}
    if provider.lower() == "github":
        details["ownerName"] = git.get("owner")
        details["repositoryName"] = git.get("repository")
    else:
        details["organizationName"] = git.get("organization")
        details["projectName"] = git.get("project")
        details["repositoryName"] = git.get("repository")
    details["branchName"] = branch or git.get("branch")
    details["directoryName"] = directory or git.get("directory")

    missing = [key for key, value in details.items() if not value]
    if missing:
        raise FabricOpsError(
            f"incomplete git settings: {', '.join(missing)}",
            hint="Set them under `defaults.git` (repository/branch) and `layers.<layer>.git.directory`.",
        )
    return details


@dataclass
class GitStatus:
    """What `git/status` says about one workspace, in both directions."""

    remote: str | None = None
    head: str | None = None
    workspace_changes: list[str] = field(default_factory=list)
    remote_changes: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    @property
    def behind(self) -> bool:
        """Git has moved on from what the workspace last pulled."""
        return bool(self.remote and self.remote != self.head)

    @property
    def ahead(self) -> bool:
        """Someone changed the workspace and has not committed it."""
        return bool(self.workspace_changes)

    def summary(self) -> str:
        parts = []
        if self.behind:
            parts.append("behind git")
        if self.ahead:
            parts.append(f"{len(self.workspace_changes)} uncommitted workspace change(s)")
        if self.conflicts:
            parts.append(f"{len(self.conflicts)} conflict(s)")
        return ", ".join(parts) or "up to date with git"


@dataclass
class ConnectGit(Action):
    workspace: str = ""
    git: dict[str, Any] = field(default_factory=dict)
    connection_action: str | None = None
    connection_id: str | None = None
    connection_name: str | None = None
    branch: str | None = None
    directory: str | None = None
    #: References declared in the recipe whose files live in this layer's directory. Pulling
    #: before they resolve is what produces DiscoverDependenciesFailed, so they are checked
    #: first and the pull is deferred rather than attempted.
    references: tuple = ()
    #: Recipe to resolve those references against, when it is not the recipe being run. A
    #: feature branch shares dev's semantic model and lakehouses, so a committed id has to be
    #: judged against dev - resolving it against the feature workspaces would ask whether the
    #: branch points at its own copy, which is exactly what it must not do.
    reference_recipe: Any = None
    initialize: bool = True
    disconnect_after_initialize: bool = False

    def describe(self) -> str:
        return "Git integration"

    def detail(self) -> str:
        branch = self.branch or self.git.get("branch")
        facts = [f"{self.git.get('provider')}", f"{self.directory or self.git.get('directory')} @ {branch}"]
        if self.git.get("sync_on_commit"):
            facts.append("sync on commit")
        if self.disconnect_after_initialize:
            facts.append("disconnect after init")
        return " \u00b7 ".join(facts)

    def apply(self, ctx: "RunContext") -> ActionResult:
        workspace_id = ctx.workspace_id(self.layer) if self.layer else None
        if not workspace_id and not ctx.dry_run:
            raise FabricOpsError(f"workspace id for layer '{self.layer}' is not known yet")

        connection_id = self.connection_id or (
            ctx.output(self.connection_action, "id") if self.connection_action else None
        )
        if not connection_id and self.connection_name and not ctx.dry_run:
            # The feature flow reuses the connection the platform setup created, so it is
            # not part of this plan: resolve it by name.
            from ..fabric.paths import FabPath

            path = FabPath.connection(self.connection_name)
            if ctx.cli.exists(path):
                connection_id = ctx.cli.get_value(path, "id")
        if not connection_id and not ctx.dry_run:
            raise FabricOpsError(
                "no git credentials connection is available",
                hint="Set `defaults.git.credentials.connection` (or connection_id) so the workspace can authenticate.",
            )

        blocked = self._unresolved_references(ctx)
        current = self._connection_state(ctx, workspace_id)

        if _is_connected(current) and blocked:
            return ActionResult("skipped", {"git_state": current}, blocked)

        if _is_connected(current):
            if not _is_initialized(current):
                # Connected but never initialised. This is what a run that connected and
                # then failed during updateFromGit leaves behind, so it has to be a
                # recoverable state rather than a reason to try connecting again.
                if ctx.dry_run:
                    return ActionResult("updated", {"git_state": current}, "would initialise")
                self._initialize(ctx, workspace_id)
                if self.disconnect_after_initialize:
                    self._disconnect(ctx, workspace_id)
                    return ActionResult("updated", {"git_state": "Disconnected"}, "initialised, disconnected")
                return ActionResult("updated", {"git_state": "ConnectedAndInitialized"}, "initialised")
            if self.disconnect_after_initialize:
                # The recipe says this layer must not stay connected, and it is connected -
                # which is where a run that failed after connecting leaves it. Converging on
                # the declared state matters more than what an earlier run managed: without
                # this, one failure left the layer connected for good, and every later run
                # reported "behind git, nothing pulled" as though that were the intent.
                if ctx.dry_run:
                    return ActionResult("updated", {"git_state": current}, "would disconnect")
                self._disconnect(ctx, workspace_id)
                return ActionResult("updated", {"git_state": "Disconnected"}, "disconnected")
            # Only report a change when one actually happened: an already-connected
            # workspace that needed no sync is `existed`, not `updated`.
            changed, message = self._sync(ctx, workspace_id)
            return ActionResult("updated" if changed else "existed", {"git_state": current}, message)

        body = {
            "gitProviderDetails": provider_details(self.git, branch=self.branch, directory=self.directory),
            "myGitCredentials": {"source": "ConfiguredConnection", "connectionId": connection_id},
        }
        try:
            ctx.cli.api(f"workspaces/{workspace_id}/git/connect", method="post", body=body, expect=(200, 201))
        except FabricOpsError as error:
            if "GitProviderAuthenticationFailed" in str(error) or "BadCredentials" in str(error):
                # Fabric is rejecting the token *stored in the connection*, not our identity.
                # Saying so matters: the natural reading of "authentication failed" is that
                # the service principal is wrong, and it is not.
                name = self.connection_name or connection_id
                raise FabricOpsError(
                    f"the git provider rejected the credentials stored in connection '{name}'",
                    hint="The personal access token has expired or lost access to the repository. "
                         "Issue a new one, put it in your credentials file or GITHUB_PAT, then run "
                         "`fabricops connection refresh`.",
                ) from error
            raise
        if ctx.dry_run:
            return ActionResult("created", {"git_state": "would connect"})

        if blocked:
            # Connected, but pulling now would fail on the unresolved reference. The connect
            # did happen, so this is `created` with a deferral - not a failure and not a
            # no-op. Nothing is wrong with the deployment; an id this tenant has not
            # generated yet is simply not in the repository.
            return ActionResult("created", {"git_state": "Connected"}, f"connected; {blocked}")

        if self.initialize:
            self._initialize(ctx, workspace_id)
        if self.disconnect_after_initialize:
            self._disconnect(ctx, workspace_id)
            return ActionResult("created", {"git_state": "Disconnected"}, "connected, initialised, disconnected")
        return ActionResult("created", {"git_state": "Connected"})

    # ------------------------------------------------------------------ internals
    def _unresolved_references(self, ctx: "RunContext") -> str | None:
        """Why this layer cannot be pulled yet, or None if it can.

        A report's semantic model and a pipeline's notebooks are resolved by Fabric at sync
        time, and item ids are per-tenant - so a repository cannot ship ones that resolve
        anywhere else. Checking first turns `DiscoverDependenciesFailed`, which says nothing
        useful, into a named reference and the command that fixes it.
        """
        # Runs in a dry run as well: every step of it is a read, and a plan that cannot say
        # "this layer will be deferred" is not a plan you can act on. I wrote this guard with
        # `or ctx.dry_run` first, which is the same mistake this codebase has now made ten
        # times - see the note in documentation/reference/development.md.
        if not self.references:
            return None
        from . import references as references_module

        recipe = self.reference_recipe or ctx.recipe
        resolve = references_module.tenant_resolver(ctx.cli, recipe)
        # Honoured here as well as in the planner, so the flag means the same thing wherever
        # the list came from.
        for reference in (r for r in self.references if r.blocks_sync):
            deployed = resolve(reference.target)
            if not deployed:
                return f"waiting for {reference.target.describe()}, which does not exist yet"
            committed = references_module.read(reference)
            if committed and committed.casefold() != deployed.casefold():
                return (
                    f"{reference.describe()} points at {committed}, but this environment has "
                    f"{deployed} - run `fabricops references sync --environment "
                    f"{recipe.environment or 'dev'} --apply` and commit"
                )
        return None

    def _connection_state(self, ctx: "RunContext", workspace_id: Any) -> str | None:
        # Read in a dry run as well. Returning None here made every connected workspace look
        # unconnected, so a plan claimed it would connect seven workspaces that were already
        # connected - and `plan --check` called that drift.
        if not workspace_id or str(workspace_id).startswith("<"):
            return None      # the workspace itself would only be created by this run
        try:
            response = ctx.cli.api(f"workspaces/{workspace_id}/git/connection", check=False)
        except FabricApiError:
            return None
        body = response.body if isinstance(response.body, dict) else {}
        return body.get("gitConnectionState")

    def _disconnect(self, ctx: "RunContext", workspace_id: Any) -> None:
        ctx.cli.api(f"workspaces/{workspace_id}/git/disconnect", method="post", expect=(200, 204))

    def _initialize(self, ctx: "RunContext", workspace_id: Any) -> None:
        # `initializationStrategy` is required when content exists on **both** sides. A
        # freshly created workspace has none, which is why the first run of a layer works
        # without it and the second fails with MissingInitializationStrategy: by then the
        # workspace holds what the first run pulled. It is the same decision
        # `conflict_resolution` already makes for a pull, so it uses the same policy rather
        # than inventing a second one. `stop` sends nothing: with no ambiguity Fabric
        # initialises anyway, and with ambiguity it refuses, which is what `stop` asks for.
        policy = str(self.git.get("conflict_resolution") or "prefer_remote").lower()
        strategy = self.CONFLICT_POLICIES.get(policy)
        request = {"initializationStrategy": strategy} if strategy else None
        try:
            response = ctx.cli.api(
                f"workspaces/{workspace_id}/git/initializeConnection",
                method="post",
                body=request,
                expect=(200, 202),
            )
        except FabricOpsError as error:
            if "MissingInitializationStrategy" in str(error) or "MissingInitializationPolicy" in str(error):
                raise FabricOpsError(
                    "both the workspace and the branch have content, so Fabric will not "
                    "choose which one wins",
                    hint="conflict_resolution is `stop` for this layer. Set it to "
                         "`prefer_remote` or `prefer_workspace`, or empty the workspace.",
                ) from error
            raise
        body = response.body if isinstance(response.body, dict) else {}
        if body.get("requiredAction") in (None, "None"):
            return
        remote = body.get("remoteCommitHash")
        if body.get("requiredAction") == "UpdateFromGit" and remote:
            self._update_from_git(ctx, workspace_id, remote)

    def _sync(self, ctx: "RunContext", workspace_id: Any) -> tuple[bool, str]:
        """Compare the workspace with git, then act only as far as the recipe allows.

        `sync_on_commit` gates the **write**, never the read. Reading is cheap and safe,
        and a run that does not look cannot tell you that dev is twenty commits behind or
        that someone has been editing in the portal - it can only say "connected", which
        reads as "fine".

        Divergence is two-directional and the two directions are not symmetric. Behind git
        is routine and fixable. Ahead of git means someone changed the workspace and has
        not committed, and pulling would discard that - so it is named, every time, rather
        than absorbed by `PreferRemote`.
        """
        status = self._status(ctx, workspace_id)
        policy = str(self.git.get("conflict_resolution") or "prefer_remote").lower()

        if status.conflicts and policy == "stop":
            return False, f"conflicted: {status.summary()} - resolve in the portal, or set conflict_resolution"

        if not self.git.get("sync_on_commit"):
            return False, f"{status.summary()} (sync_on_commit is off, so nothing was pulled)"

        if not status.behind:
            return False, status.summary()

        losing = status.workspace_changes + status.conflicts
        if losing and policy == "prefer_remote":
            ctx.log.warning(f"  git wins over workspace changes to: {', '.join(losing)}")
        if ctx.dry_run:
            overwritten = f", overwriting {len(losing)} workspace change(s)" if losing else ""
            return True, f"would pull from git{overwritten}"
        self._update_from_git(ctx, workspace_id, status.remote, status.head, policy=policy)
        overwritten = f", overwriting {len(losing)} workspace change(s)" if losing else ""
        return True, f"pulled from git{overwritten}"

    def _status(self, ctx: "RunContext", workspace_id: Any) -> "GitStatus":
        response = ctx.cli.api(f"workspaces/{workspace_id}/git/status", check=False)
        body = response.body if isinstance(response.body, dict) else {}
        workspace_changes, remote_changes, conflicts = [], [], []
        for change in body.get("changes") or []:
            metadata = change.get("itemMetadata") or {}
            name = str(metadata.get("displayName") or metadata.get("itemType") or "?")
            if str(change.get("conflictType") or "None") != "None":
                conflicts.append(name)
            elif change.get("workspaceChange"):
                workspace_changes.append(name)
            elif change.get("remoteChange"):
                remote_changes.append(name)
        return GitStatus(
            remote=body.get("remoteCommitHash"),
            head=body.get("workspaceHead"),
            workspace_changes=workspace_changes,
            remote_changes=remote_changes,
            conflicts=conflicts,
        )

    #: `prefer_remote` is the default because a git-connected workspace exists to mirror a
    #: branch - and in practice Fabric normalises items on import, so "conflict" is the
    #: routine state after any edit, not an exceptional one. `stop` is for environments
    #: where losing a workspace change would matter more than staying in step.
    CONFLICT_POLICIES = {"prefer_remote": "PreferRemote", "prefer_workspace": "PreferWorkspace"}

    def _update_from_git(
        self,
        ctx: "RunContext",
        workspace_id: Any,
        remote: str,
        head: str | None = None,
        policy: str = "prefer_remote",
    ) -> None:
        body: dict[str, Any] = {
            "remoteCommitHash": remote,
            "conflictResolution": {
                "conflictResolutionType": "Workspace",
                "conflictResolutionPolicy": self.CONFLICT_POLICIES.get(policy, "PreferRemote"),
            },
            "options": {"allowOverrideItems": True},
        }
        if head:
            body["workspaceHead"] = head
        try:
            ctx.cli.api(
                f"workspaces/{workspace_id}/git/updateFromGit", method="post", body=body, expect=(200, 202)
            )
        except FabricOpsError as error:
            raise _explain_sync_failure(error) from error


@dataclass
class RegisterWorkspaceRelation(Action):
    """Make an automation-created feature workspace a *branched workspace*.

    Uses the preview Create Workspace Relation API, which is the documented way to get
    the same relationship the portal's "Branch out to another workspace" creates - the
    workspace tree, breadcrumbs and related-branches navigation all follow from it.

    Known constraints, mapped to actionable messages: the caller needs admin on the
    branch workspace and contributor on the base, both workspaces must be connected to
    the same git root directory, the base cannot itself be a branch, and the target must
    not already have branch workspaces.
    """

    base_workspace: str = ""
    relation_type: str = "Base"

    def describe(self) -> str:
        return f"Branch relation to '{self.base_workspace}'"

    def detail(self) -> str:
        return f"{self.relation_type} \u2192 {self.base_workspace}"

    def apply(self, ctx: "RunContext") -> ActionResult:
        from ..fabric.paths import FabPath

        workspace_id = ctx.workspace_id(self.layer) if self.layer else None
        if ctx.dry_run:
            return ActionResult("created", {"relation": self.relation_type}, "would register")
        if not workspace_id:
            return ActionResult("skipped", message="feature workspace id unknown")

        # Only when this run created the workspace. Fabric establishes this relation as part
        # of branching out, which is a create-time operation, and there is no API to read the
        # current relations - so on a workspace that already existed we cannot tell an
        # existing relation from a missing one, and the POST is a coin toss that usually
        # loses. `WorkspaceRelationAlreadyExists` is documented for exactly this, but the
        # service returns a generic `BadRequest: An error occurred in the Entity Framework`,
        # which no amount of error handling can distinguish from a real fault. Skipped, and
        # said plainly - not reported as `existed`, because nothing verified that it does.
        if not ctx.dry_run and not ctx.workspace_was_created(self.layer or ""):
            return ActionResult(
                "skipped",
                message="the workspace already existed, so its relation was left as it is",
            )

        base_path = FabPath.workspace(self.base_workspace)
        if not ctx.cli.exists(base_path):
            return ActionResult(
                "skipped", message=f"base workspace '{self.base_workspace}' does not exist"
            )
        base_id = ctx.cli.get_value(base_path, "id")

        response = ctx.cli.api(
            f"workspaces/{workspace_id}/git/workspaceRelations",
            method="post",
            body={"relatedWorkspaceId": base_id, "relationType": self.relation_type},
            expect=(200, 201, 400, 409),
            check=False,
        )
        body = response.body if isinstance(response.body, dict) else {}
        error_code = str(body.get("errorCode") or "")

        if response.ok:
            return ActionResult("created", {"relation_id": body.get("id"), "base_workspace_id": base_id})
        if error_code in ("WorkspaceRelationAlreadyExists", "WorkspaceRelationBidirectionalExists"):
            return ActionResult("existed", {"base_workspace_id": base_id})
        if error_code:
            # The API's own message, in preference to our hint. There is no endpoint that
            # lists relations, so a generic `BadRequest` - which is not in the documented
            # code list - can only be explained by what the response says, and keeping the
            # code alone threw that away.
            return ActionResult("skipped", message=f"{error_code}: {_relation_reason(body, error_code)}")
        return ActionResult("skipped", message=f"status {response.status_code}")


#: An id in a *deployed* item that no longer resolves. Fabric reports it as the artifact
#: being missing, which reads as though the repository is wrong - and the repository is
#: usually right by the time anyone sees this.
_MISSING_ARTIFACT = re.compile(r"Fabric artifact '([0-9a-fA-F-]{36})' is not found")


def _explain_sync_failure(error: "FabricOpsError") -> "FabricOpsError":
    """Name what a git sync failure actually means, where we can recognise it."""
    text = str(error)
    if "WorkspaceMigrationOperationInProgress" in text:
        # Fabric is moving the workspace - capacity or backend - and refuses writes while it
        # does. Nothing to do with the deployment, and it clears on its own. Not added to the
        # transient markers: the service says `isRetriable: false`, and a migration outlasts
        # a backoff that tops out at 20 seconds, so retrying here would burn time in CI and
        # still fail. Re-running the pipeline once it settles is the honest advice.
        return FabricOpsError(
            "Fabric is migrating this workspace and will not accept writes until it finishes",
            hint="Temporary, and not caused by your deployment or your recipe. Wait for the "
                 "migration to finish and run again - the workspace is left connected, so the "
                 "next run picks up where this one stopped.",
        )
    match = _MISSING_ARTIFACT.search(text)
    if not match:
        return error
    return FabricOpsError(
        f"the deployed item still points at '{match.group(1)}', which does not exist in this "
        f"tenant - and Fabric will not update an item whose current state is broken",
        hint="A correct commit is not enough: the workspace copy has to go. Delete the item "
             "from the workspace and run again to recreate it from git. If the id looks like "
             "a placeholder, run `fabricops references sync --environment <env> --apply` and "
             "commit first, then delete.",
    )


def _relation_reason(body: dict, error_code: str) -> str:
    """What the API said, falling back to what we know about the code."""
    parts = [str(body.get("message") or "").strip()]
    parts += [
        str(detail.get("message") or "").strip()
        for detail in (body.get("moreDetails") or [])
        if isinstance(detail, dict)
    ]
    said = "; ".join(part for part in parts if part)
    hint = _relation_hint(error_code)
    if said and hint and "see the Create Workspace Relation" not in hint:
        return f"{said} - {hint}"
    return said or hint


def _relation_hint(error_code: str) -> str:
    return {
        "WorkspaceRelationRootDirectoryMismatch": "both workspaces must use the same git root directory",
        "WorkspaceRelationBaseIsBranch": "the base workspace is itself a branch workspace",
        "WorkspaceRelationTargetHasBranches": "the base workspace already has branch workspaces",
        "WorkspaceRelationDifferentBase": "this workspace already relates to a different base",
        "InsufficientPrivileges": "needs admin on the feature workspace and contributor on the base",
        "WorkspaceRelationSelfReferencing": "a workspace cannot be related to itself",
        "WorkspaceRelationInvalidArgument": "invalid arguments for a workspace relation",
        "WorkspaceRelationInvalidType": "invalid workspace relation type",
        "WorkspaceNotFound": "the related workspace was not found",
    }.get(error_code, "see the Create Workspace Relation API documentation")
