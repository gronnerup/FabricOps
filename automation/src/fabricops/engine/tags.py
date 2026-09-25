"""Tags as extended properties (documentation/specs/E05).

Constraints this design works around, all verified:

* a tag is a flat display name of at most 40 characters - there is no value field, so
  FabricOps encodes properties as `Key:Value` with a fixed managed-key list;
* an object may carry at most 10 tags, and a tenant at most 10,000;
* creating tags is an admin API (`Tenant.ReadWrite.All`), while applying needs only
  contributor - hence a committed registry, so the deployment identity never needs
  admin rights;
* apply takes tag **ids**, so names must be resolved;
* the tag APIs allow 25 requests per minute per principal;
* tags are not part of an item definition, so they never travel with git or deployment
  pipelines and must be reconciled by automation on every run.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable

from ..errors import FabricOpsError, RecipeError
from .actions import Action, ActionResult

if TYPE_CHECKING:  # pragma: no cover
    from ..fabric.cli import FabricCli
    from .context import RunContext

MAX_TAGS_PER_OBJECT = 10
MAX_TAG_NAME_LENGTH = 40

# Decision 6: a fixed managed-key list, so tags can be parsed back into properties.
MANAGED_KEYS = ("ManagedBy", "Solution", "Env", "Layer", "Lifecycle", "Owner", "Branch", "Retain")


def is_managed(tag: str) -> bool:
    """True when FabricOps owns this tag, i.e. it may add or remove it."""
    return tag.split(":", 1)[0] in MANAGED_KEYS if ":" in tag else False


def validate_tags(tags: Iterable[str], *, where: str) -> list[str]:
    """De-duplicate and check tags against the platform's limits."""
    seen: list[str] = []
    for tag in tags:
        if tag not in seen:
            seen.append(tag)
    problems: list[str] = []
    for tag in seen:
        if len(tag) > MAX_TAG_NAME_LENGTH:
            problems.append(f"'{tag}' is {len(tag)} characters (the limit is {MAX_TAG_NAME_LENGTH})")
    if len(seen) > MAX_TAGS_PER_OBJECT:
        problems.append(f"{len(seen)} tags requested but an object may carry at most {MAX_TAGS_PER_OBJECT}: {seen}")
    if problems:
        raise RecipeError(f"{where}: invalid tags:\n  - " + "\n  - ".join(problems))
    return seen


@dataclass
class TagRegistry:
    """The committed name -> id map, so applying tags needs no admin API call."""

    tags: dict[str, str] = field(default_factory=dict)
    scope: dict[str, Any] = field(default_factory=lambda: {"type": "Tenant"})
    path: pathlib.Path | None = None

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: str | pathlib.Path) -> "TagRegistry":
        from ..recipe import loader

        path = pathlib.Path(path)
        if not path.exists():
            return cls(path=path)
        data = loader.load_file(path)
        entries = data.get("tags") or []
        tags = {str(entry["name"]): str(entry.get("id") or "") for entry in entries if entry.get("name")}
        return cls(tags=tags, scope=data.get("scope") or {"type": "Tenant"}, path=path)

    @classmethod
    def discover(cls, resources: str | pathlib.Path, solution: str | None = None) -> "TagRegistry":
        """Prefer the solution's own registry, then the shared one."""
        resources = pathlib.Path(resources)
        candidates: list[pathlib.Path] = []
        if solution:
            candidates += [resources / "solutions" / solution / f"tags{suffix}" for suffix in (".yml", ".yaml", ".json")]
        candidates += [resources / "tags" / f"registry{suffix}" for suffix in (".yml", ".yaml", ".json")]
        for candidate in candidates:
            if candidate.exists():
                return cls.load(candidate)
        return cls(path=candidates[0] if candidates else None)

    def save(self, path: str | pathlib.Path | None = None) -> pathlib.Path:
        from ..recipe import loader

        target = pathlib.Path(path or self.path or "automation/resources/tags/registry.yml")
        target.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "apiVersion": "fabricops/v1",
            "kind": "TagRegistry",
            "scope": self.scope,
            "tags": [{"name": name, "id": self.tags[name]} for name in sorted(self.tags)],
        }
        target.write_text(loader.dump(document, "yaml"), encoding="utf-8")
        self.path = target
        return target

    # ------------------------------------------------------------------ lookups
    def resolve(self, names: Iterable[str]) -> list[str]:
        missing = [name for name in names if not self.tags.get(name)]
        if missing:
            raise FabricOpsError(
                f"these tags are not in the registry: {', '.join(missing)}",
                hint="Run `fabricops tags sync` with an admin identity to create them and record their ids.",
            )
        return [self.tags[name] for name in names]

    def known(self, names: Iterable[str]) -> list[str]:
        return [name for name in names if self.tags.get(name)]

    @property
    def ids_to_names(self) -> dict[str, str]:
        return {value: key for key, value in self.tags.items() if value}


# ------------------------------------------------------------------------ actions
@dataclass
class ApplyTags(Action):
    """Apply the recipe's tags to a workspace or an item."""

    target: str = "workspace"          # workspace | item
    workspace_action: str | None = None
    item_action: str | None = None
    tags: list[str] = field(default_factory=list)
    reconcile: bool = False

    def describe(self) -> str:
        return f"Tags ({len(self.tags)})"

    def detail(self) -> str:
        return ", ".join(self.tags)

    def apply(self, ctx: "RunContext") -> ActionResult:
        registry = ctx.tag_registry
        if registry is None:
            return ActionResult("skipped", message="no tag registry configured")

        desired = validate_tags(self.tags, where=self.id)
        workspace_id = ctx.output(self.workspace_action or "", "id")
        item_id = ctx.output(self.item_action or "", "id") if self.item_action else None

        if ctx.dry_run:
            return ActionResult("updated", {"tags": desired}, "would apply")
        if not workspace_id or (self.target == "item" and not item_id):
            return ActionResult("skipped", message="target id unknown")

        endpoint = (
            f"workspaces/{workspace_id}/applyTags"
            if self.target == "workspace"
            else f"workspaces/{workspace_id}/items/{item_id}/applyTags"
        )
        current = self._current(ctx, workspace_id, item_id)
        to_apply = [tag for tag in desired if tag not in current]
        to_remove = [tag for tag in current if self.reconcile and is_managed(tag) and tag not in desired]

        if not to_apply and not to_remove:
            return ActionResult("existed", {"tags": desired}, "already applied")

        if to_apply:
            ctx.cli.api(endpoint, method="post", body={"tags": registry.resolve(to_apply)}, expect=(200, 201))
        if to_remove:
            unapply = endpoint.replace("applyTags", "unapplyTags")
            ctx.cli.api(unapply, method="post", body={"tags": registry.resolve(to_remove)}, expect=(200, 201), check=False)

        message = ", ".join(filter(None, [
            f"+{len(to_apply)}" if to_apply else "",
            f"-{len(to_remove)}" if to_remove else "",
        ]))
        return ActionResult("updated", {"tags": desired}, message)

    def _current(self, ctx: "RunContext", workspace_id: Any, item_id: Any) -> list[str]:
        """Tags already applied, resolved from ids back to names."""
        endpoint = f"workspaces/{workspace_id}" if self.target == "workspace" else f"workspaces/{workspace_id}/items/{item_id}"
        response = ctx.cli.api(endpoint, check=False)
        body = response.body if isinstance(response.body, dict) else {}
        names: list[str] = []
        registry = ctx.tag_registry
        for entry in body.get("tags") or []:
            if isinstance(entry, dict):
                name = entry.get("displayName") or (registry.ids_to_names.get(str(entry.get("id"))) if registry else None)
            else:
                name = registry.ids_to_names.get(str(entry)) if registry else None
            if name:
                names.append(str(name))
        return names


# --------------------------------------------------------------------- admin sync
def sync_registry(cli: "FabricCli", registry: TagRegistry, required: Iterable[str], *, batch_size: int = 20) -> dict[str, Any]:
    """Create missing tenant/domain tags and record their ids. Needs an admin identity.

    Batched to stay inside the 25 requests-per-minute limit on the tag APIs.
    """
    required = validate_names(required)
    existing = _list_tags(cli)
    registry.tags.update({name: tag_id for name, tag_id in existing.items() if name in required})

    missing = [name for name in required if not registry.tags.get(name)]
    created: dict[str, str] = {}
    for start in range(0, len(missing), batch_size):
        batch = missing[start : start + batch_size]
        body: dict[str, Any] = {"createTagsRequest": [{"displayName": name} for name in batch]}
        if registry.scope.get("type") == "Domain":
            body["scope"] = registry.scope
        response = cli.api("admin/tags/bulkCreateTags", method="post", body=body, expect=(200, 201))
        for entry in (response.body or {}).get("tags", []) if isinstance(response.body, dict) else []:
            created[str(entry.get("displayName"))] = str(entry.get("id"))
    registry.tags.update(created)
    return {"required": list(required), "created": created, "existing": len(existing)}


def validate_names(names: Iterable[str]) -> list[str]:
    unique: list[str] = []
    for name in names:
        if name not in unique:
            unique.append(name)
    too_long = [name for name in unique if len(name) > MAX_TAG_NAME_LENGTH]
    if too_long:
        raise RecipeError(f"tag names longer than {MAX_TAG_NAME_LENGTH} characters: {', '.join(too_long)}")
    return unique


def _list_tags(cli: "FabricCli") -> dict[str, str]:
    response = cli.api("admin/tags", expect=(200, 401, 403), check=False)
    body = response.body if isinstance(response.body, dict) else {}
    if response.status_code in (401, 403):
        raise FabricOpsError(
            "listing tenant tags needs a Fabric administrator identity",
            hint="Run `tags sync` with an admin, then commit the registry; deployments only need the committed file.",
        )
    return {str(entry.get("displayName")): str(entry.get("id")) for entry in body.get("value") or [] if entry.get("displayName")}
