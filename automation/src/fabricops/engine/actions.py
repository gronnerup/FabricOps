"""Actions: the unit the planner emits and the executor runs.

An action is idempotent by contract - it reads first and acts only on a difference - and
reports which of `created / existed / updated / skipped / failed` happened. Item types do
not appear in engine control flow; type-specific behaviour lives in handlers.py.
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from ..errors import FabricOpsError
from ..fabric.paths import FabPath, params as render_params

if TYPE_CHECKING:  # pragma: no cover
    from .context import RunContext

# `deleted` is its own status rather than borrowing `created`. A teardown that reports
# "1 created" is the kind of line that makes someone check whether it really deleted
# anything.
Status = Literal["created", "existed", "updated", "deleted", "skipped", "failed"]

STATUS_MARKS: dict[str, str] = {
    "created": " ✔",
    "existed": " ⚠ Already exists",
    "updated": " ✔ Updated",
    "deleted": " ✔ Deleted",
    "skipped": " ⚠ Skipped",
    "failed": " ✖ Failed!",
}


@dataclass
class ActionResult:
    status: Status
    outputs: dict[str, Any] = field(default_factory=dict)
    message: str = ""

    @property
    def changed(self) -> bool:
        return self.status in ("created", "updated")


@dataclass
class Action:
    """Base action. Subclasses implement `apply` and, where meaningful, `destroy`."""

    id: str
    kind: str = "action"
    label: str = ""
    depends_on: tuple[str, ...] = ()
    layer: str | None = None
    # A convenience the run can do without. When one of these fails the executor warns and
    # carries on, and nothing that depends on it is skipped. Granting the pushing developer
    # admin on their own feature workspace is the case this exists for: the recipe's admin
    # group already has the workspace, so losing it costs a portal click, not the run.
    advisory: bool = False
    #: Kept only because something else depends on it. It still runs - a git action cannot
    #: resolve a workspace id without it - but it is plumbing for the job that was asked
    #: for, so it reports at debug level instead of cluttering the run.
    incidental: bool = False

    def describe(self) -> str:
        return self.label or self.id

    def detail(self) -> str:
        """The material facts of this action - what a reader needs to judge it.

        `describe()` names the thing; `detail()` says what will actually be done to it
        (capacity, role, git directory, creation payload). Empty when there is nothing
        worth adding.
        """
        return ""

    def apply(self, ctx: "RunContext") -> ActionResult:  # pragma: no cover - abstract
        raise NotImplementedError

    def destroy(self, ctx: "RunContext") -> ActionResult | None:
        """Return None when this action owns nothing to delete (the default)."""
        return None


# --------------------------------------------------------------------- workspace
@dataclass
class CreateWorkspace(Action):
    workspace: str = ""
    capacity: str | None = None

    def describe(self) -> str:
        return f"Workspace '{self.workspace}'"

    def detail(self) -> str:
        return f"capacity={self.capacity}" if self.capacity else "no capacity specified"

    @property
    def path(self) -> FabPath:
        return FabPath.workspace(self.workspace)

    def apply(self, ctx: "RunContext") -> ActionResult:
        if ctx.cli.exists(self.path):
            workspace_id = ctx.cli.get_value(self.path, "id")
            return ActionResult("existed", {"id": workspace_id, "name": self.workspace, "created": False})

        if self.incidental:
            # Kept in the plan only because something else reads this workspace's id. On a
            # sync that must stay a read: creating it here yields a bare workspace with the
            # service principal as its only admin, no identity, no settings - which is what
            # the dev sync on every merge silently did after a teardown, seven times over.
            raise FabricOpsError(
                f"workspace '{self.workspace}' does not exist, and a sync does not create workspaces",
                hint="Run `fabricops setup --environment <env>` first. A sync only converges git on "
                     "workspaces that setup has provisioned with roles, identity and settings.",
            )

        parameters = render_params({"capacityname": self.capacity}) if self.capacity else None
        try:
            ctx.cli.mkdir(self.path, params=parameters)
        except FabricOpsError as error:
            # Enriched rather than pre-checked: a pre-check would cost an extra read before
            # every workspace, on every run, to improve one rare error. The CLI names the
            # capacity it could not find but not the alternatives, and "which capacities can
            # this identity actually see" is the question you are left holding.
            if self.capacity and _is_missing_capacity(error, str(self.capacity)):
                raise FabricOpsError(
                    f"capacity '{self.capacity}' does not exist, or this identity cannot see it",
                    hint=_capacity_hint(ctx),
                ) from error
            raise
        if ctx.dry_run:
            return ActionResult("created", {"id": f"<{self.workspace} id>", "name": self.workspace, "created": True})
        return ActionResult(
            "created", {"id": ctx.cli.get_value(self.path, "id"), "name": self.workspace, "created": True}
        )

    def destroy(self, ctx: "RunContext") -> ActionResult | None:
        if not ctx.cli.exists(self.path):
            return ActionResult("skipped", message="does not exist")
        ctx.cli.rm(self.path)
        return ActionResult("deleted")


@dataclass
class CreateWorkspaceIdentity(Action):
    workspace: str = ""

    def describe(self) -> str:
        return "Workspace identity"

    def detail(self) -> str:
        return "managed identity"

    def apply(self, ctx: "RunContext") -> ActionResult:
        path = FabPath.managed_identity(self.workspace)
        if ctx.cli.exists(path):
            return ActionResult("existed")
        ctx.cli.mkdir(path)
        if ctx.dry_run:
            return ActionResult("created")
        identity = ctx.cli.get_value(
            FabPath.workspace(self.workspace), "workspaceIdentity.servicePrincipalId"
        )
        return ActionResult("created", {"service_principal_id": identity})


@dataclass
class CreateFolder(Action):
    workspace: str = ""
    folder: str = ""

    def describe(self) -> str:
        return f"Folder '{self.folder}'"

    def detail(self) -> str:
        return self.folder

    def apply(self, ctx: "RunContext") -> ActionResult:
        path = FabPath.folder(self.workspace, self.folder)
        if ctx.cli.exists(path):
            return ActionResult("existed", {"path": self.folder})
        ctx.cli.mkdir(path)
        return ActionResult("created", {"path": self.folder})


@dataclass
class AssignRole(Action):
    workspace: str = ""
    role: str = "member"
    principal_id: str | None = None
    principal_type: str = "Group"
    principal_workspace: str | None = None  # for type=WorkspaceIdentity
    identity_from: str | None = None        # action id providing service_principal_id

    def describe(self) -> str:
        # Naming the principal, because a layer can have two admins - Model grants the
        # generic group *and* Orchestrate's workspace identity - and two lines reading
        # "Role admin" look like the same action applied twice.
        if self.principal_workspace:
            return f"Role {self.role} for the identity of '{self.principal_workspace}'"
        if self.identity_from:
            return f"Role {self.role} for a workspace identity"
        if self.principal_id:
            return f"Role {self.role} for {self.principal_type} {self.principal_id[:8]}"
        return f"Role {self.role}"

    def detail(self) -> str:
        who = self.principal_id or f"identity of {self.principal_workspace}"
        return f"{self.role} \u2192 {self.principal_type} {who}"

    def _resolve_identity(self, ctx: "RunContext") -> str | None:
        """The managed identity of another workspace, if that workspace exists yet."""
        path = FabPath.workspace(str(self.principal_workspace))
        if not ctx.cli.exists(path):
            return None
        try:
            return ctx.cli.get_value(path, "workspaceIdentity.servicePrincipalId") or None
        except FabricOpsError:
            return None

    def apply(self, ctx: "RunContext") -> ActionResult:
        principal = self.principal_id
        if principal is None and self.identity_from:
            principal = ctx.output(self.identity_from, "service_principal_id")
        if principal is None and self.principal_workspace:
            # Resolved in a dry run too, when the source workspace is already there. It is a
            # read, and without it the identity stays a placeholder that can never match an
            # existing assignment - so `plan --check` reported drift on this one action for
            # ever, which is the failure mode a nightly check cannot survive.
            principal = self._resolve_identity(ctx)
        if principal is None and self.principal_workspace and not ctx.dry_run:
            principal = ctx.cli.get_value(
                FabPath.workspace(self.principal_workspace), "workspaceIdentity.servicePrincipalId"
            )
        if not principal:
            if ctx.dry_run:
                # Planning a fresh tenant: the identity this role refers to would only be
                # created by this same run, so a placeholder keeps the plan inspectable.
                principal = f"<identity of {self.principal_workspace or self.workspace}>"
            else:
                raise FabricOpsError(
                    f"could not resolve the principal for {self.describe()}",
                    hint="For type=WorkspaceIdentity, the source workspace must have an identity.",
                )
        path = FabPath.workspace(self.workspace)
        if _role_already_assigned(ctx, path, principal, self.role):
            return ActionResult("existed", {"principal_id": principal, "role": self.role})
        ctx.cli.acl_set(path, principal, self.role)
        return ActionResult("updated", {"principal_id": principal, "role": self.role})


# -------------------------------------------------------------------------- item
@dataclass
class CreateItem(Action):
    workspace: str = ""
    item: dict[str, Any] = field(default_factory=dict)

    @property
    def item_name(self) -> str:
        return str(self.item.get("name"))

    @property
    def item_type(self) -> str:
        return str(self.item.get("type"))

    def describe(self) -> str:
        return f"{self.item_type}: {self.item_name}"

    def detail(self) -> str:
        facts: list[str] = []
        payload = self.item.get("creation_payload") or {}
        if payload:
            facts.append(str(render_params(payload)))   # MaskedValue renders masked
        if self.item.get("folder"):
            facts.append(f"folder={self.item['folder']}")
        if self.item.get("definition"):
            facts.append("with definition")
        if self.item.get("skip_creation"):
            facts.append("skip_creation")
        return " \u00b7 ".join(str(fact) for fact in facts)

    @property
    def path(self) -> FabPath:
        return FabPath.item(self.workspace, self.item_name, self.item_type)

    def apply(self, ctx: "RunContext") -> ActionResult:
        from .handlers import handler_for

        handler = handler_for(self.item_type)
        existed = ctx.cli.exists(self.path)

        if not existed and self.item.get("skip_creation"):
            return ActionResult("skipped", message="skip_creation is set and the item does not exist")

        if existed:
            status: Status = "existed"
        else:
            payload = dict(self.item.get("creation_payload") or {})
            handler.pre_create(ctx, self, payload)
            parameters = render_params(payload) if payload else None
            if self.item.get("description"):
                parameters = ",".join(filter(None, [parameters, f"description={self.item['description']}"]))
            ctx.cli.mkdir(self.path, params=parameters)
            status = "created"

        if ctx.dry_run and status == "created":
            return ActionResult(status, {"name": self.item_name, "type": self.item_type})

        metadata = handler.wait_ready(ctx, self)
        outputs = {"name": self.item_name, "type": self.item_type, **handler.outputs(metadata)}
        return ActionResult(status, outputs)


@dataclass
class ImportDefinition(Action):
    """Import an item definition, and only when it differs from what is already there."""

    target_workspace: str = ""
    item: dict[str, Any] = field(default_factory=dict)
    source: Any = None                      # definitions.DefinitionSource
    repository_root: Any = None             # pathlib.Path

    @property
    def item_name(self) -> str:
        return str(self.item.get("name"))

    @property
    def item_type(self) -> str:
        return str(self.item.get("type"))

    def describe(self) -> str:
        return f"Definition of {self.item_type}: {self.item_name}"

    def detail(self) -> str:
        if self.source is None:
            return ""
        if self.source.directory:
            return f"from {self.source.directory}"
        return f"{len(self.source.parts)} inline part(s)"

    @property
    def path(self) -> FabPath:
        return FabPath.item(self.target_workspace, self.item_name, self.item_type)

    def apply(self, ctx: "RunContext") -> ActionResult:
        import tempfile
        from . import definitions

        desired = definitions.source_hash(self.source)
        current = self._current_hash(ctx)
        if current == desired:
            return ActionResult("existed", {"content_hash": desired}, message="definition unchanged")

        with tempfile.TemporaryDirectory(prefix="fabricops-import-") as tmp:
            directory = definitions.materialise(self.source, pathlib.Path(tmp))
            ctx.cli.import_definition(
                self.path, directory, definition_format=self.source.definition_format
            )
        status: Status = "updated" if current else "created"
        return ActionResult(status, {"content_hash": desired})

    def _current_hash(self, ctx: "RunContext") -> str | None:
        """Hash of the definition the tenant holds now, or None if it holds none."""
        import tempfile
        from . import definitions

        if not ctx.cli.exists(self.path):
            return None
        with tempfile.TemporaryDirectory(prefix="fabricops-export-") as tmp:
            root = pathlib.Path(tmp)
            result = ctx.cli.export_definition(
                self.path, root, definition_format=self.source.definition_format
            )
            if not result.ok:
                # An item with no definition yet, or a type that cannot be exported. Either
                # way there is nothing to compare against, so import and let Fabric decide.
                return None
            return definitions.exported_hash(root, self.item_name, self.item_type)


@dataclass
class SetProperties(Action):
    target_workspace: str = ""
    item: dict[str, Any] | None = None
    properties: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        scope = f"{self.item['type']}: {self.item['name']}" if self.item else "workspace"
        return f"Properties on {scope} ({len(self.properties)})"

    def detail(self) -> str:
        keys = list(self.properties)
        shown = ", ".join(keys[:3])
        return shown + (f", +{len(keys) - 3} more" if len(keys) > 3 else "")

    @property
    def path(self) -> FabPath:
        if self.item:
            return FabPath.item(self.target_workspace, str(self.item["name"]), str(self.item["type"]))
        return FabPath.workspace(self.target_workspace)

    def apply(self, ctx: "RunContext") -> ActionResult:
        changed: list[str] = []
        for query, desired in self.properties.items():
            # Compare in a dry run too. Reads are allowed there, and drift detection
            # (E03-S8) depends on this reporting "already up to date" honestly.
            if _current_matches(ctx, self.path, query, desired):
                continue
            ctx.cli.set_property(self.path, query, desired)
            changed.append(query)
        if not changed:
            # "(4)... Already exists (4 of 4 as declared)": the count in the label is what
            # was checked, the message says how many of those matched. Without both, a
            # reader counts three names against a four and thinks one was forgotten.
            return ActionResult("existed", message=f"{len(self.properties)} of {len(self.properties)} as declared")
        return ActionResult(
            "updated", {"changed": changed},
            message=f"{len(changed)} of {len(self.properties)}: " + ", ".join(changed),
        )


#: The CLI's own wording, from fabric_cli/errors/common.py: "The <type> '<name>' could not
#: be found". Matched precisely, because the failure message also carries the redacted
#: command - which contains `-P capacityname=<name>` and would match anything looser.
_MISSING_CAPACITY = re.compile(r"The Capacity '([^']+)' could not be found", re.IGNORECASE)


def _is_missing_capacity(error: Exception, capacity: str) -> bool:
    match = _MISSING_CAPACITY.search(str(error))
    if not match:
        return False
    named = match.group(1).removesuffix(".Capacity")
    return named.casefold() == capacity.casefold()


def _capacity_hint(ctx: "RunContext") -> str:
    """List what this identity can see, because that is the next thing you need to know.

    Cached per run: every layer usually names the same capacity, so without this a bad
    capacity name costs one `ls .capacities` per workspace.
    """
    cached = getattr(ctx.cli, "_capacity_hint", None)
    if cached is not None:
        return cached
    try:
        result = ctx.cli.invoke(["ls", ".capacities"], check=False)
        names = [
            line.strip().removesuffix(".Capacity")
            for line in result.stdout.splitlines()
            if line.strip().endswith(".Capacity")
        ]
    except FabricOpsError:
        names = []
    hint = (
        "Capacities this identity can see: " + ", ".join(names)
        if names
        else "This identity can see no capacities at all. It needs Capacity Contributor or "
             "Admin on the capacity, and the capacity must be active."
    )
    ctx.cli._capacity_hint = hint
    return hint


def _role_already_assigned(ctx: "RunContext", path: FabPath, principal: str, role: str) -> bool:
    """Read-compare-write for role assignments.

    Without this, every run reassigns every role and reports `updated`, which makes a
    nightly drift check (E03-S8) permanently red for reasons nobody can act on.
    """
    for entry in ctx.cli.acl_get(path):
        assigned = str(entry.get("role") or "")
        if str(entry.get("id")) == str(principal) and assigned.casefold() == role.casefold():
            return True
    return False


def _current_matches(ctx: "RunContext", path: FabPath, query: str, desired: Any) -> bool:
    """Read-compare-write: never issue a write that would change nothing."""
    try:
        current = ctx.cli.get_json(path, query)
    except FabricOpsError:
        return False
    if isinstance(desired, (dict, list)) or isinstance(current, (dict, list)):
        return current == desired
    return str(current).strip() == str(desired).strip()
