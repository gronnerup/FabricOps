"""Feature workspaces: one branch, one workspace per participating layer (E08).

What this adds over the first-generation script: the branched-workspace relation, tags
for ownership so cleanup stops parsing names, the initiating developer as an admin of
their own workspace, and the same git module the platform flow uses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..errors import RecipeError
from ..recipe import Recipe
from . import inventory
from .actions import AssignRole, CreateWorkspace, SetProperties
from .git import ConnectGit, RegisterWorkspaceRelation
from .plan import Plan, _references_in, order
from .storage import DropFeatureSchema, configured_lakehouses
from .tags import ApplyTags

FEATURE_PREFIXES = ("refs/heads/", "feature/", "features/")


@dataclass(frozen=True)
class BranchInfo:
    """What a branch name tells us."""

    branch: str
    topic: str
    layer: str | None = None
    developer: str | None = None
    group: str | None = None

    @property
    def slug(self) -> str:
        return re.sub(r"[^A-Za-z0-9-]+", "-", self.branch).strip("-")


def _is_object_id(value: str) -> bool:
    """An Entra object id is a GUID. A CI actor id is not."""
    return bool(re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", str(value).strip()))


def branch_segment(branch: str) -> str | None:
    """The `<segment>` in `feature/<segment>/<topic>`, before any recipe is loaded.

    Needed early: the segment decides *which* feature recipe to load when it names a
    group, so it cannot be derived from the recipe's layer list.
    """
    trimmed = strip_prefixes(branch)
    parts = [part for part in trimmed.split("/") if part]
    return parts[0] if len(parts) > 1 else None


def strip_prefixes(branch: str) -> str:
    trimmed = branch.strip()
    for prefix in FEATURE_PREFIXES:
        if trimmed.startswith(prefix):
            trimmed = trimmed[len(prefix) :]
    return trimmed.removeprefix("feature/")


def parse_branch(
    branch: str, layers: list[str], *, developer: str | None = None, group: str | None = None
) -> BranchInfo:
    """`feature/prepare/add-orders` -> topic `add-orders`, layer `Prepare` when it matches.

    The convention is the first-generation one, kept: strip the feature prefix, and if the
    leading segment names a layer, only that layer (plus any marked `always`) participates.
    """
    trimmed = strip_prefixes(branch)
    parts = [part for part in trimmed.split("/") if part]
    if not parts:
        raise RecipeError(f"could not derive a feature name from branch '{branch}'")

    # A segment consumed as a group is not also a layer filter: the group recipe
    # declares the layers its slice needs.
    layer: str | None = None
    if len(parts) > 1 and not group:
        candidate = parts[0].lower()
        layer = next((name for name in layers if name.lower() == candidate), None)

    return BranchInfo(branch=branch, topic=parts[-1], layer=layer, developer=developer, group=group)


def participating_layers(recipe: Recipe, info: BranchInfo, explicit: list[str] | None = None) -> list[str]:
    if explicit:
        wanted = {name.strip().lower() for name in explicit}
        return [name for name in recipe.layers if name.lower() in wanted]
    if not info.layer:
        return list(recipe.layers)
    return [
        name
        for name in recipe.layers
        if name == info.layer or bool(recipe.layer(name).get("always"))
    ]


def build_feature_plan(
    recipe: Recipe,
    *,
    branch: str,
    group: str | None = None,
    developer: str | None = None,
    developer_object_id: str | None = None,
    layers: list[str] | None = None,
    base_pattern: str | None = None,
    base_environment: str = "dev",
    register_relation: bool = True,
    storage_recipe: "Recipe | None" = None,
    base_recipe: "Recipe | None" = None,
    repository_root: str = ".",
    created: str | None = None,
) -> Plan:
    """Build the plan for one feature branch."""
    info = parse_branch(branch, list(recipe.layers), developer=developer, group=group)
    selected = participating_layers(recipe, info, layers)
    if not selected:
        raise RecipeError(
            f"no layers participate for branch '{branch}'",
            hint=f"Layers in the feature recipe: {', '.join(recipe.layers) or 'none'}",
        )

    defaults = recipe.defaults
    actions: list[Any] = []
    warnings: list[str] = []
    created_at = created or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for layer in selected:
        definition = recipe.layer(layer)
        workspace = recipe.workspace_name(layer, feature=info.topic, developer=developer, branch=info.branch)
        workspace_action = CreateWorkspace(
            id=f"workspace:{layer}",
            kind="workspace",
            layer=layer,
            workspace=workspace,
            capacity=recipe.value(layer, "capacity", defaults.get("capacity")),
        )
        actions.append(workspace_action)

        tags = _feature_tags(recipe, layer, info)
        if tags:
            actions.append(
                ApplyTags(
                    id=f"tags:{layer}",
                    kind="tags",
                    layer=layer,
                    target="workspace",
                    workspace_action=workspace_action.id,
                    tags=tags,
                    depends_on=(workspace_action.id,),
                )
            )

        # The self-describing stamp cleanup reads (E08-S3). Tags would be the nicer query
        # surface, but they need a tenant admin to create and a recipe may declare none,
        # so the description is the record that is always there.
        properties = {
            "description": inventory.stamp(
                solution=recipe.solution,
                layer=layer,
                branch=info.branch,
                developer=developer,
                created=created_at,
                base=_base_template(base_pattern or recipe.data.get("base_display_name_pattern"),
                                    base_environment, recipe.solution),
            ),
            **(defaults.get("properties") or {}),
            **(definition.get("properties") or {}),
        }
        if properties:
            actions.append(
                SetProperties(
                    id=f"properties:{layer}",
                    kind="properties",
                    layer=layer,
                    target_workspace=workspace,
                    properties=properties,
                    depends_on=(workspace_action.id,),
                )
            )

        index = 0
        for role, principals in (defaults.get("permissions") or {}).items():
            for principal in principals or []:
                if not principal.get("id"):
                    continue
                actions.append(
                    AssignRole(
                        id=f"role:{layer}:{role}:{index}",
                        kind="role",
                        layer=layer,
                        workspace=workspace,
                        role=str(role).lower(),
                        principal_id=principal["id"],
                        principal_type=str(principal.get("type") or "Group"),
                        depends_on=(workspace_action.id,),
                    )
                )
                index += 1

        if developer_object_id and not _is_object_id(developer_object_id):
            # E08-S5 says fall back to the recipe's group permissions with a warning when the
            # identity cannot be resolved. Failing the whole run over an optional convenience
            # is not that - and the value that gets here wrongly is usually a CI actor id.
            warnings.append(
                f"'{developer_object_id}' is not an Entra object id, so {info.developer or 'the developer'} "
                f"was not made an admin of their own workspace. Pass --developer-object-id with a GUID."
            )
        elif developer_object_id:
            # The developer who pushed the branch owns their own workspace, so they can
            # switch branches and manage it without a platform admin.
            actions.append(
                AssignRole(
                    id=f"role:{layer}:developer",
                    kind="role",
                    layer=layer,
                    label=f"Role admin for {developer or 'the initiating developer'}",
                    workspace=workspace,
                    role="admin",
                    principal_id=developer_object_id,
                    principal_type="User",
                    depends_on=(workspace_action.id,),
                    # Advisory: the id can be GUID-shaped and still not be an Entra object -
                    # Azure DevOps' BUILD_REQUESTEDFORID is an Azure DevOps identity id, which
                    # usually is not. The recipe's admin group already owns the workspace, so a
                    # rejected id costs the developer a portal click, not seven workspaces.
                    advisory=True,
                )
            )

        git_node = {**(defaults.get("git") or {}), **(definition.get("git") or {})}
        if git_node.get("directory") and git_node.get("provider"):
            git_action = ConnectGit(
                id=f"git:{layer}",
                kind="git",
                layer=layer,
                workspace=workspace,
                git=git_node,
                connection_id=(git_node.get("credentials") or {}).get("connection_id"),
                connection_action=_git_connection_action(git_node),
                connection_name=(git_node.get("credentials") or {}).get("connection"),
                branch=info.branch,
                directory=str(git_node["directory"]),
                disconnect_after_initialize=bool(git_node.get("disconnect_after_initialize")),
                depends_on=(workspace_action.id,),
                # A feature branch carries the same committed item definitions as the branch
                # it came from, so it hits the same unresolvable ids - and a feature run that
                # is not deferred fails on `PowerBIEntityNotFound` instead of saying which
                # reference is stale. The declarations live in the platform recipe because
                # they describe the repository, not an environment, and they are resolved
                # against the base environment because that is the model a feature shares.
                references=_references_in(base_recipe, repository_root, str(git_node["directory"])),
                reference_recipe=base_recipe,
            )
            actions.append(git_action)

            if register_relation and not git_node.get("disconnect_after_initialize"):
                pattern = base_pattern or recipe.data.get("base_display_name_pattern")
                if pattern:
                    base_workspace = _render_base(pattern, layer, base_environment, recipe.solution)
                    actions.append(
                        RegisterWorkspaceRelation(
                            id=f"relation:{layer}",
                            kind="relation",
                            layer=layer,
                            base_workspace=base_workspace,
                            depends_on=(workspace_action.id, git_action.id),
                        )
                    )

    actions.extend(
        _feature_schema_actions(recipe, info, base_pattern, base_environment, storage_recipe)
    )
    return Plan(actions=order(actions), recipe=recipe, warnings=warnings)


def _feature_schema_actions(
    recipe: Recipe,
    info: BranchInfo,
    base_pattern: str | None,
    base_environment: str,
    storage_recipe: "Recipe | None",
) -> list[Any]:
    """Teardown for the schema a feature wrote into the *shared* lakehouse (E07).

    The schema lives in dev Store, not in the feature workspace, so deleting the workspace
    does not remove it. These actions do nothing on apply - the solution's notebook creates
    the schema on first write - and drop it on teardown when the recipe says so.
    """
    settings = (storage_recipe or recipe).storage
    feature_schema = settings["feature_schema"]
    if not feature_schema["enabled"] or not feature_schema["drop_on_teardown"]:
        return []

    pattern = base_pattern or recipe.data.get("base_display_name_pattern")
    if not pattern:
        return []

    schema = (storage_recipe or recipe).feature_schema_name(feature=info.topic, developer=info.developer)
    actions: list[Any] = []
    for reference in configured_lakehouses(storage_recipe or recipe):
        actions.append(
            DropFeatureSchema(
                id=f"storage:{reference.layer}:{reference.item}:{schema}",
                kind="storage",
                layer=reference.layer,
                workspace=_render_base(pattern, reference.layer, base_environment, recipe.solution),
                lakehouse=reference.item,
                schema=schema,
            )
        )
    return actions


def _feature_tags(recipe: Recipe, layer: str, info: BranchInfo) -> list[str]:
    """Ownership tags, so cleanup queries metadata instead of parsing workspace names."""
    tags = list(recipe.tags_for(layer))
    if not tags:
        return []
    additions = [f"Lifecycle:Feature", f"Layer:{layer}"]
    if info.developer:
        additions.append(f"Owner:{info.developer}")
    branch_tag = f"Branch:{info.slug}"
    if len(branch_tag) <= 40:
        additions.append(branch_tag)
    for tag in additions:
        if tag not in tags:
            tags.append(tag)
    return tags


def _git_connection_action(git_node: dict[str, Any]) -> str | None:
    connection = (git_node.get("credentials") or {}).get("connection")
    return f"connection:{connection}" if connection else None


def _base_template(pattern: str | None, environment: str, solution: str | None) -> str | None:
    """The base workspace name with `{layer}` left unresolved, for the description stamp.

    A feature workspace is named `*Brickyard add-orders (Prepare)`, which carries neither
    the environment nor, reliably, the solution - so a notebook in it cannot work out
    `Brickyard - Store [dev]` from its own name. The planner can, because it renders that
    very name for the branch relation. Stamping the template lets the notebook read it
    back and substitute the layer it needs.
    """
    if not pattern:
        return None
    # Plain replacement, not `substitute()`: that function only defers tokens at the root
    # of a recipe, and a stamp is not a recipe. Two replaces cannot fail, so nothing here
    # needs catching - and a swallowed exception is how this bug hid in the first place.
    return str(pattern).replace("{environment}", environment).replace("{solution}", solution or "default")


def _render_base(pattern: str, layer: str, environment: str, solution: str | None) -> str:
    from ..recipe.tokens import substitute

    return str(
        substitute(pattern, {"layer": layer, "environment": environment, "solution": solution}, path="base_pattern")
    )
