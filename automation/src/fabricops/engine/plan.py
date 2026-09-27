"""Planning: a recipe becomes an ordered, dependency-aware list of actions.

Order comes from `depends_on`, not from the order statements appear in a script - which is
why a `WorkspaceIdentity` role assignment no longer needs the hand-rolled second pass in
today's `fabric_setup.py`.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..errors import RecipeError
from ..recipe import Recipe
from . import definitions
from .actions import (
    Action,
    AssignRole,
    CreateFolder,
    CreateItem,
    CreateWorkspace,
    CreateWorkspaceIdentity,
    ImportDefinition,
    SetProperties,
)
from .connections import CreateConnection, connection_roles
from .git import ConnectGit
from .tags import ApplyTags


def _pad(line: str, column: int = 62) -> str:
    """Right-pad so the detail column lines up, without truncating a long label."""
    return " " * max(0, column - len(line))


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    recipe: Recipe | None = None
    #: Things the caller should know that are not failures - an optional action skipped
    #: because its input was unusable, for instance.
    warnings: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.actions)

    def __iter__(self):
        return iter(self.actions)

    def by_id(self, action_id: str) -> Action | None:
        return next((action for action in self.actions if action.id == action_id), None)

    def of_kind(self, kind: str) -> list[Action]:
        return [action for action in self.actions if action.kind == kind]

    def for_destroy(self) -> list[Action]:
        """Teardown is the same plan in reverse, keeping only actions that own something."""
        return [action for action in reversed(self.actions) if type(action).destroy is not Action.destroy]

    # ------------------------------------------------------------------ rendering
    def describe(self, *, sequence: bool = False) -> list[str]:
        """Render the plan. Grouped by layer by default; `sequence=True` for the
        literal execution order.

        Grouping answers "what happens to this workspace", which is the question a
        reader actually has. The sequence view answers "in what order", which matters
        exactly when something ordered itself surprisingly.
        """
        return self._describe_sequence() if sequence else self._describe_grouped()

    def _describe_grouped(self) -> list[str]:
        groups: dict[str | None, list[Action]] = {}
        for action in self.actions:
            groups.setdefault(action.layer, []).append(action)

        width = max((len(action.kind) for action in self.actions), default=10)
        lines: list[str] = []

        for layer in [None, *[key for key in groups if key is not None]]:
            actions = groups.get(layer)
            if not actions:
                continue
            lines.append("")
            lines.append(self._group_header(layer, actions))
            for action in actions:
                detail = action.detail()
                line = f"  {action.kind.ljust(width)}  {action.describe()}"
                lines.append(f"{line}{'' if not detail else '  ' + _pad(line) + detail}")
        return lines

    def _group_header(self, layer: str | None, actions: list[Action]) -> str:
        if layer is None:
            return "solution"
        workspace = next(
            (getattr(action, "workspace", "") for action in actions if getattr(action, "workspace", "")),
            "",
        )
        return f"{layer} \u2192 {workspace}" if workspace else str(layer)

    def _describe_sequence(self) -> list[str]:
        width = max((len(action.kind) for action in self.actions), default=10)
        lines: list[str] = []
        for index, action in enumerate(self.actions, start=1):
            scope = action.layer or "solution"
            lines.append(f"{index:>3}. {action.kind.ljust(width)}  [{scope}] {action.describe()}")
            if action.depends_on:
                lines.append(f"     {' ' * width}  \u2190 {', '.join(action.depends_on)}")
        return lines


def build_plan(recipe: Recipe, *, repository_root: pathlib.Path | str = ".") -> Plan:
    """Build the plan for a platform recipe.

    `repository_root` is what `definition.from` and `definition.parts[].from` are relative
    to; it is the repository root rather than the recipe's own directory, because those
    paths name solution content, not recipe content.
    """
    actions: list[Action] = []
    repository_root = pathlib.Path(repository_root)
    defaults = recipe.defaults
    default_capacity = defaults.get("capacity")
    default_permissions = defaults.get("permissions") or {}
    default_properties = defaults.get("properties") or {}

    identity_actions: dict[str, str] = {}  # workspace display name -> identity action id
    connection_roles_for_solution = connection_roles(default_permissions)

    # Solution-scoped connections and the git credentials connection come first: layers
    # and items depend on them, and they exist once per solution rather than per layer.
    is_primary = bool(defaults.get("is_primary", True))
    deferred_connections: list[CreateConnection] = []
    git_actions: dict[str, str] = {}   # layer -> git action id, for connections to wait on
    for connection in _solution_connections(recipe):
        scope = str(connection.get("scope") or "solution").lower()
        if scope == "solution" and not is_primary:
            continue
        source = connection.get("from_item")
        if source:
            # Built from an item the repository owns and `fabricops release` deploys, so it
            # cannot be created until the workspace exists and the item is published. It is
            # planned after the workspaces rather than alongside the solution connections.
            deferred_connections.append(
                CreateConnection(
                    id=f"connection:{connection['name']}",
                    kind="connection",
                    layer=str(source.get("layer")),
                    label=f"Connection '{connection['name']}'",
                    connection_name=str(connection["name"]),
                    payload_kind="sql",
                    auth=str(connection.get("auth") or "ServicePrincipal"),
                    source_item=_source_item(recipe, connection["name"], source),
                    roles=connection_roles_for_solution,
                    depends_on=(f"workspace:{source.get('layer')}",),
                )
            )
            continue
        actions.append(
            CreateConnection(
                id=f"connection:{connection['name']}",
                kind="connection",
                label=f"Connection '{connection['name']}'",
                connection_name=str(connection["name"]),
                payload_kind="fabric",
                connection_type=connection.get("type"),
                auth=str(connection.get("auth") or "ServicePrincipal"),
                scope=scope,
                roles=connection_roles_for_solution,
            )
        )

    git_defaults = defaults.get("git") or {}
    git_connection_action: str | None = None
    git_connection_id: str | None = None
    if git_defaults:
        credentials_node = git_defaults.get("credentials") or {}
        connection_name = credentials_node.get("connection")
        git_connection_id = credentials_node.get("connection_id")
        if connection_name:
            git_connection_action = f"connection:{connection_name}"
            if not any(action.id == git_connection_action for action in actions):
                actions.append(
                    CreateConnection(
                        id=git_connection_action,
                        kind="connection",
                        label=f"Source control connection '{connection_name}'",
                        connection_name=str(connection_name),
                        payload_kind="git",
                        git=git_defaults,
                        roles=connection_roles_for_solution,
                    )
                )

    # Pass 1: workspaces and their identities, so cross-layer references can resolve.
    for layer in recipe.layers:
        workspace = recipe.workspace_name(layer)
        workspace_action = CreateWorkspace(
            id=f"workspace:{layer}",
            kind="workspace",
            layer=layer,
            workspace=workspace,
            capacity=recipe.value(layer, "capacity", default_capacity),
        )
        actions.append(workspace_action)

        if recipe.layer(layer).get("workspace_identity"):
            identity = CreateWorkspaceIdentity(
                id=f"identity:{layer}",
                kind="identity",
                layer=layer,
                workspace=workspace,
                depends_on=(workspace_action.id,),
            )
            actions.append(identity)
            identity_actions[workspace] = identity.id

    # Pass 2: everything that lives inside a workspace.
    for layer in recipe.layers:
        definition = recipe.layer(layer)
        workspace = recipe.workspace_name(layer)
        workspace_action_id = f"workspace:{layer}"

        properties = {**default_properties, **(definition.get("properties") or {})}
        if properties:
            actions.append(
                SetProperties(
                    id=f"properties:{layer}",
                    kind="properties",
                    layer=layer,
                    target_workspace=workspace,
                    properties=properties,
                    depends_on=(workspace_action_id,),
                )
            )

        workspace_tags = recipe.tags_for(layer)
        if workspace_tags:
            actions.append(
                ApplyTags(
                    id=f"tags:{layer}",
                    kind="tags",
                    layer=layer,
                    target="workspace",
                    workspace_action=workspace_action_id,
                    tags=workspace_tags,
                    depends_on=(workspace_action_id,),
                )
            )

        for role, principals in _merged_permissions(default_permissions, definition.get("permissions")).items():
            for index, principal in enumerate(principals):
                actions.append(_role_action(layer, workspace, role, principal, index, workspace_action_id, identity_actions))

        folder_actions: dict[str, str] = {}
        for folder in _folders(definition.get("items") or []):
            action = CreateFolder(
                id=f"folder:{layer}:{folder}",
                kind="folder",
                layer=layer,
                workspace=workspace,
                folder=folder,
                depends_on=(workspace_action_id, *([folder_actions[_parent(folder)]] if _parent(folder) in folder_actions else [])),
            )
            folder_actions[folder] = action.id
            actions.append(action)

        for item in definition.get("items") or []:
            _validate_item(layer, item)
            depends = [workspace_action_id]
            folder = item.get("folder")
            if folder and folder in folder_actions:
                depends.append(folder_actions[folder])
            depends += _item_dependencies(recipe, item)

            item_action = CreateItem(
                id=_item_id(layer, item),
                kind="item",
                layer=layer,
                workspace=workspace,
                item=item,
                depends_on=tuple(dict.fromkeys(depends)),
            )
            actions.append(item_action)

            item_tags = recipe.tags_for(layer, item)
            if item.get("tags"):
                actions.append(
                    ApplyTags(
                        id=f"{item_action.id}:tags",
                        kind="tags",
                        layer=layer,
                        target="item",
                        workspace_action=workspace_action_id,
                        item_action=item_action.id,
                        tags=item_tags,
                        depends_on=(item_action.id,),
                    )
                )

            if item.get("connection"):
                connection = item["connection"]
                actions.append(
                    CreateConnection(
                        id=f"connection:{layer}:{item['name']}",
                        kind="connection",
                        layer=layer,
                        label=f"Item connection '{connection.get('name')}'",
                        connection_name=str(connection.get("name")),
                        payload_kind="sql",
                        source_action=item_action.id,
                        roles=connection_roles(_merged_permissions(default_permissions, definition.get("permissions"))),
                        depends_on=(item_action.id,),
                    )
                )

            if item.get("definition"):
                actions.append(
                    ImportDefinition(
                        id=f"{item_action.id}:definition",
                        kind="definition",
                        layer=layer,
                        target_workspace=workspace,
                        item=item,
                        source=definitions.resolve(
                            dict(item["definition"]),
                            root=repository_root,
                            context=_token_context(recipe, layer, item),
                            where=f"layers.{layer}.items.{item.get('name')}",
                        ),
                        repository_root=repository_root,
                        depends_on=(item_action.id,),
                    )
                )

            if item.get("properties"):
                actions.append(
                    SetProperties(
                        id=f"{item_action.id}:properties",
                        kind="properties",
                        layer=layer,
                        target_workspace=workspace,
                        item=item,
                        properties=dict(item["properties"]),
                        depends_on=(f"{item_action.id}:definition",)
                        if item.get("definition")
                        else (item_action.id,),
                    )
                )

    # Git integration per layer: the workspace and its items must exist first, and
    # an update-from-git may create further items.
    for layer in recipe.layers:
        definition = recipe.layer(layer)
        layer_git = {**git_defaults, **(definition.get("git") or {})}
        directory = layer_git.get("directory")
        if not directory or not layer_git.get("provider"):
            continue
        depends = [f"workspace:{layer}"]
        if git_connection_action:
            depends.append(git_connection_action)
        depends += [action.id for action in actions if action.kind == "item" and action.layer == layer]
        actions.append(
            ConnectGit(
                id=f"git:{layer}",
                kind="git",
                layer=layer,
                workspace=recipe.workspace_name(layer),
                git=layer_git,
                connection_action=git_connection_action,
                connection_id=git_connection_id,
                directory=str(directory),
                references=_references_in(recipe, repository_root, str(directory)),
                disconnect_after_initialize=bool(layer_git.get("disconnect_after_initialize")),
                depends_on=tuple(dict.fromkeys(depends)),
            )
        )
        git_actions[layer] = f"git:{layer}"

    # Connections built from an item come last of all. On a git-connected workspace the
    # item arrives with the git sync above, so the connection has to wait for it - which is
    # the whole of dev, where `fabricops release` never runs. Where the layer is not
    # git-connected the item comes from release instead, and this reports what it is
    # waiting for rather than failing.
    for connection in deferred_connections:
        git_action = git_actions.get(str(connection.layer))
        if git_action:
            connection.depends_on = tuple(dict.fromkeys((*connection.depends_on, git_action)))
    actions.extend(deferred_connections)

    return Plan(actions=order(actions), recipe=recipe)


def _solution_connections(recipe: Recipe) -> list[dict[str, Any]]:
    """Connections declared at the root or under defaults, de-duplicated by name."""
    collected: list[dict[str, Any]] = []
    for source in (recipe.data.get("connections"), recipe.defaults.get("connections")):
        for connection in source or []:
            if connection.get("name") and connection["name"] not in [c["name"] for c in collected]:
                collected.append(connection)
    return collected


def order(actions: Iterable[Action]) -> list[Action]:
    """Stable topological sort: declaration order, adjusted to satisfy dependencies."""
    remaining = list(actions)
    known = {action.id for action in remaining}
    resolved: list[Action] = []
    satisfied: set[str] = set()

    while remaining:
        progressed = False
        for action in list(remaining):
            missing = [dep for dep in action.depends_on if dep in known and dep not in satisfied]
            if missing:
                continue
            resolved.append(action)
            satisfied.add(action.id)
            remaining.remove(action)
            progressed = True
        if not progressed:
            cycle = ", ".join(action.id for action in remaining)
            raise RecipeError(f"circular dependency between actions: {cycle}")
    return resolved


# ------------------------------------------------------------------------ helpers
def _item_id(layer: str, item: dict[str, Any]) -> str:
    return f"item:{layer}:{item.get('type')}:{item.get('name')}"


def _validate_item(layer: str, item: dict[str, Any]) -> None:
    if not item.get("name") or not item.get("type"):
        raise RecipeError(f"layers.{layer}.items: every item needs both 'name' and 'type' (got {item!r})")


def _item_dependencies(recipe: Recipe, item: dict[str, Any]) -> list[str]:
    """Cross-item dependencies declared in the recipe (e.g. a Report's semantic model)."""
    payload = item.get("creation_payload") or {}
    model = payload.get("semanticModel") or payload.get("semantic_model")
    if not model:
        return []
    layer = payload.get("semanticModelLayer")
    layers = [layer] if layer else list(recipe.layers)
    for candidate in layers:
        for other in recipe.items(candidate):
            if other.get("type") == "SemanticModel" and other.get("name") == model:
                return [_item_id(candidate, other)]
    return []


def _folders(items: list[dict[str, Any]]) -> list[str]:
    """Every folder path an item needs, parents first, de-duplicated."""
    paths: list[str] = []
    for item in items:
        folder = str(item.get("folder") or "").strip("/")
        if not folder:
            continue
        parts = folder.split("/")
        for depth in range(1, len(parts) + 1):
            candidate = "/".join(parts[:depth])
            if candidate not in paths:
                paths.append(candidate)
    return paths


def _parent(folder: str) -> str:
    return folder.rsplit("/", 1)[0] if "/" in folder else ""


def _merged_permissions(defaults: dict[str, Any], layer: Any) -> dict[str, list[dict[str, Any]]]:
    """Union default and layer permissions, keeping one entry per principal per role."""
    merged: dict[str, list[dict[str, Any]]] = {}
    for source in (defaults or {}, layer or {}):
        for role, principals in (source or {}).items():
            # Keyed lowercase because that is what the action uses. Recipes in the wild
            # write both "Admin" and "admin"; bucketing by the raw key made those two
            # roles, and the same principal was then assigned the same role twice.
            bucket = merged.setdefault(str(role).lower(), [])
            for principal in principals or []:
                identity = (principal.get("id"), principal.get("name"), principal.get("type"))
                if identity not in [(p.get("id"), p.get("name"), p.get("type")) for p in bucket]:
                    bucket.append(principal)
    return merged


def _role_action(
    layer: str,
    workspace: str,
    role: str,
    principal: dict[str, Any],
    index: int,
    workspace_action_id: str,
    identity_actions: dict[str, str],
) -> AssignRole:
    principal_type = str(principal.get("type") or "Group")
    depends = [workspace_action_id]
    source_workspace: str | None = None
    identity_from: str | None = None

    if principal_type.lower() == "workspaceidentity":
        source_workspace = str(principal.get("workspace") or principal.get("name") or "")
        identity_from = identity_actions.get(source_workspace)
        if identity_from:
            depends.append(identity_from)

    return AssignRole(
        id=f"role:{layer}:{role}:{index}",
        kind="role",
        layer=layer,
        workspace=workspace,
        role=str(role).lower(),
        principal_id=principal.get("id"),
        principal_type=principal_type,
        principal_workspace=source_workspace,
        identity_from=identity_from,
        depends_on=tuple(depends),
    )


def _token_context(recipe: Recipe, layer: str, item: dict[str, Any]) -> dict[str, Any]:
    """Tokens available inside an inline definition part."""
    return {
        "layer": layer,
        "environment": recipe.environment,
        "solution": recipe.solution,
        "workspace": recipe.workspace_name(layer),
        "item": item.get("name"),
        "item_type": item.get("type"),
    }


def _source_item(recipe: Recipe, connection_name: str, source: dict[str, Any]) -> dict[str, Any]:
    """Resolve a connection's `from_item` reference to a concrete workspace and item."""
    layer = str(source.get("layer") or "")
    if layer not in recipe.layers:
        raise RecipeError(
            f"connection '{connection_name}': from_item.layer '{layer}' is not a layer",
            hint=f"Known layers: {', '.join(recipe.layers) or 'none'}",
        )
    for key in ("name", "type"):
        if not source.get(key):
            raise RecipeError(f"connection '{connection_name}': from_item.{key} is required")
    return {
        "workspace": recipe.workspace_name(layer),
        "layer": layer,
        "name": str(source["name"]),
        "type": str(source["type"]),
    }


def _references_in(recipe: Recipe | None, root: pathlib.Path | str, directory: str) -> tuple:
    """The declared references whose files live in one layer's git directory.

    A layer can only be blocked by references it actually carries: the report blocks Present,
    the pipeline blocks Orchestrate, and neither should hold up Store.
    """
    from . import references as references_module

    if recipe is None:
        return ()      # a feature run with no platform recipe to read declarations from
    root = pathlib.Path(root)
    try:
        declared = references_module.declared(recipe, root)
    except RecipeError:
        return ()      # `recipe validate` is the place that complains about a bad declaration
    target = (root / directory).resolve()
    return tuple(
        reference for reference in declared
        if reference.blocks_sync
        and (target == reference.file.parent or target in reference.file.parents)
    )
