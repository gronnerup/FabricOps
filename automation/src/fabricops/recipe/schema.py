"""Recipe validation, dependency-free.

One declarative spec drives three things: validation with path-qualified messages and
"did you mean" suggestions, the generated recipe reference documentation, and (later) the
`solution.schema.json` published for IDE completion. Keeping it in Python means the
validator needs no third-party package to run in CI.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any

from ..errors import RecipeError
from .aliases import ALL_ALIASES


@dataclass(frozen=True)
class Field:
    name: str
    types: tuple[type, ...]
    description: str
    required: bool = False
    node: str | None = None          # nested node spec name, for mappings/lists
    of_node: str | None = None       # node spec applied to each list element
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class Node:
    name: str
    fields: tuple[Field, ...]
    free_form: bool = False          # accept unknown keys (e.g. `properties`)
    description: str = ""
    known: set[str] = field(default_factory=set)

    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]


def _f(name, types, description, **kw):  # noqa: ANN001, ANN201 - terse spec builder
    return Field(name=name, types=types if isinstance(types, tuple) else (types,), description=description, **kw)


NODES: dict[str, Node] = {
    "root": Node(
        "root",
        (
            _f("apiVersion", str, "Recipe schema version. Optional; absent means fabricops/v1."),
            _f("kind", str, "Platform or Feature. Optional; inferred from the resolved file.", choices=("Platform", "Feature")),
            _f("metadata", dict, "Solution identity.", node="metadata"),
            _f("display_name_pattern", str, "Workspace display name pattern, e.g. 'Sales - {layer} [{environment}]'."),
            _f("defaults", dict, "Values inherited by every layer.", node="defaults"),
            _f("connections", list, "Solution-level Fabric connections.", of_node="connection"),
            _f("layers", (dict, list), "The workspaces that make up the solution.", node="layers"),
            _f("storage", dict, "Feature-development storage strategy (kind: Feature).", node="storage"),
            _f("branch", dict, "Feature branch conventions (kind: Feature).", node="branch"),
            _f("references", list, "Committed item ids to keep in step with the environment.", of_node="reference"),
        ),
    ),
    "metadata": Node(
        "metadata",
        (
            _f("solution", str, "Solution name; also available as the {solution} token."),
            _f("description", str, "Free text."),
        ),
    ),
    "defaults": Node(
        "defaults",
        (
            _f("capacity", str, "Capacity name used when a layer does not override it."),
            _f("environment", str, "Environment name this overlay describes, e.g. dev."),
            _f("is_primary", bool, "Marks the environment that owns solution-scoped objects."),
            _f("permissions", dict, "Workspace role assignments.", node="permissions"),
            _f("tags", list, "Tags applied to every workspace and item (Key:Value)."),
            _f("git", dict, "Git provider settings.", node="git"),
            _f("connections", list, "Solution-level Fabric connections.", of_node="connection"),
            _f("properties", dict, "Workspace properties applied to every layer.", node="properties"),
            _f("deploy", dict, "Default release policy for every layer.", node="deploy"),
        ),
    ),
    "permissions": Node("permissions", (), free_form=True, description="Role name -> list of principals."),
    "properties": Node("properties", (), free_form=True, description="JSON path -> value, applied with `fab set`."),
    "git": Node(
        "git",
        (
            _f("provider", str, "GitHub or AzureDevOps.", choices=("GitHub", "AzureDevOps")),
            _f("owner", str, "GitHub owner."),
            _f("organization", str, "Azure DevOps organization."),
            _f("project", str, "Azure DevOps project."),
            _f("repository", str, "Repository name."),
            _f("branch", str, "Branch to connect."),
            _f("directory", str, "Repository directory this workspace maps to."),
            _f("credentials", dict, "Connection holding the git credentials.", node="git_credentials"),
            _f("sync_on_commit", bool, "Update the workspace from git on every commit."),
            _f("disconnect_after_initialize", bool, "Disconnect once the initial sync completes."),
            _f(
                "conflict_resolution",
                str,
                "Who wins when the workspace and git both changed. Default: prefer_remote.",
                choices=("prefer_remote", "prefer_workspace", "stop"),
            ),
        ),
    ),
    "git_credentials": Node(
        "git_credentials",
        (
            _f("source", str, "ConfiguredConnection or Automatic."),
            _f("connection", str, "Connection display name."),
            _f("connection_id", str, "Connection id, when the name is not known."),
        ),
    ),
    "connection": Node(
        "connection",
        (
            _f("name", str, "Connection display name.", required=True),
            _f("type", str, "Fabric connection type, e.g. PowerBIDatasets."),
            _f("auth", str, "Credential type, e.g. ServicePrincipal."),
            _f("scope", str, "solution or environment.", choices=("solution", "environment")),
            _f("from_item", dict, "Build a SQL connection from a deployed item's endpoint.", node="from_item"),
        ),
    ),
    "from_item": Node(
        "from_item",
        (
            _f("layer", str, "Layer holding the item.", required=True),
            _f("name", str, "Item display name.", required=True),
            _f("type", str, "Item type, e.g. Lakehouse or SQLDatabase.", required=True),
        ),
    ),
    "layers": Node("layers", (), free_form=True, description="Layer name -> layer definition."),
    "layer": Node(
        "layer",
        (
            _f("capacity", str, "Capacity override for this layer."),
            _f("workspace_identity", bool, "Create a workspace managed identity."),
            _f("always", bool, "Always provision this layer for a feature branch."),
            _f("permissions", dict, "Role assignments for this workspace.", node="permissions"),
            _f("tags", list, "Tags applied to this workspace (Key:Value)."),
            _f("properties", dict, "Workspace properties.", node="properties"),
            _f("git", dict, "Git settings for this workspace.", node="git"),
            _f("items", (list, dict), "Items to provision.", of_node="item"),
            _f("variable_libraries", list, "Variable libraries to generate.", of_node="variable_library"),
            _f("private_endpoints", list, "Managed private endpoints.", of_node="private_endpoint"),
            _f("deploy", dict, "Release policy for this layer.", node="deploy"),
        ),
    ),
    "item": Node(
        "item",
        (
            _f("name", str, "Item display name.", required=True),
            _f("type", str, "Fabric item type, e.g. Lakehouse.", required=True),
            _f("description", str, "Item description."),
            _f("folder", str, "Workspace folder path."),
            _f("creation_payload", dict, "Type-specific creation parameters (passed to -P)."),
            _f("properties", dict, "Item properties applied after creation.", node="properties"),
            _f("definition", dict, "Item definition to import.", node="definition"),
            _f("connection", dict, "Connection to create for this item.", node="item_connection"),
            _f("tags", list, "Tags applied to this item (Key:Value)."),
            _f("skip_creation", bool, "Do not create the item; only resolve it."),
            _f("binding", dict, "Semantic model binding (SemanticModel items).", node="binding"),
        ),
    ),
    "binding": Node(
        "binding",
        (
            _f("connection", str, "Connection display name to bind this semantic model to."),
            _f("connection_id", str, "Connection id, when the name is not known."),
        ),
    ),
    "item_connection": Node(
        "item_connection",
        (
            _f("name", str, "Connection display name (tokens allowed).", required=True),
            _f("type", str, "Connection type; defaults to SQL for SQL-backed items."),
            _f("auth", str, "Credential type."),
        ),
    ),
    "definition": Node(
        "definition",
        (
            _f("from", str, "Repository-relative directory holding the item definition."),
            _f("format", str, "Definition format, e.g. ipynb, TMDL."),
            _f("parts", list, "Inline definition parts.", of_node="definition_part"),
        ),
    ),
    "definition_part": Node(
        "definition_part",
        (
            _f("path", str, "Part path inside the item definition.", required=True),
            _f("from", str, "Repository-relative source file."),
            _f("content", str, "Inline content."),
        ),
    ),
    "variable_library": Node(
        "variable_library",
        (
            _f("name", str, "Variable library item name.", required=True),
            _f("description", str, "Item description."),
            _f("workspaces", list, "Layers to generate this library into. Defaults to the declaring layer."),
            _f("value_sets", list, "Value set names; the first is the default. Should match environment names."),
            _f("variables", list, "The variables.", of_node="variable"),
        ),
    ),
    "variable": Node(
        "variable",
        (
            _f("name", str, "Variable name.", required=True),
            _f(
                "type",
                str,
                "Variable type.",
                required=True,
                choices=("String", "Integer", "Number", "Boolean", "DateTime", "Guid", "ItemReference", "ConnectionReference"),
            ),
            _f("note", str, "Free text note. Worth writing - a name like vl_wh_gold_id explains nothing."),
            _f("value", (str, int, float, bool, dict), "Value for the first value set, and the default."),
            _f("values", dict, "Per-value-set values, keyed by value set name."),
        ),
    ),
    "private_endpoint": Node(
        "private_endpoint",
        (
            _f("name", str, "Endpoint name.", required=True),
            _f("id", str, "Target private link resource id.", required=True),
            _f("auto_approve", bool, "Auto-approve the connection."),
        ),
    ),
    "deploy": Node(
        "deploy",
        (
            _f("item_types_in_scope", list, "Item types fabric-cicd should publish."),
            _f("exclude_regex", str, "Items to skip when publishing."),
            _f("items_to_include", list, "Only publish these items (name.type)."),
            _f("folder_exclude_regex", str, "Folders to skip when publishing."),
            _f("folder_path_to_include", list, "Only publish these workspace folders."),
            _f("shortcut_exclude_regex", str, "Lakehouse shortcuts to skip."),
            _f("unpublish", dict, "Orphan-unpublish policy.", node="unpublish"),
            _f("features", list, "fabric-cicd feature flags to enable."),
            _f("constants", dict, "fabric-cicd constant overrides."),
        ),
    ),
    "unpublish": Node(
        "unpublish",
        (
            _f("skip", (bool, dict), "Skip unpublishing (per environment when a mapping)."),
            _f("exclude_regex", str, "Items never to unpublish."),
            _f("items_to_include", list, "Only unpublish these items."),
        ),
    ),
    "reference": Node(
        "reference",
        (
            _f("file", str, "Repository-relative file holding the reference.", required=True),
            _f("replace", list, "The ids inside it and what each points at.", of_node="reference_target"),
        ),
    ),
    "reference_target": Node(
        "reference_target",
        (
            _f("at", str, "Dotted path to the id, for JSON documents."),
            _f("pattern", str, "Regex with one capture group around the id, for text formats."),
            _f("layer", str, "Layer the reference points at.", required=True),
            _f("item", str, "Item name; omit to reference the workspace itself."),
            _f("type", str, "Item type, e.g. SemanticModel."),
            _f("label", str, "How this reference is described in output."),
            _f("blocks_sync", bool, "Fabric resolves this at git sync time, so defer the pull until it matches."),
        ),
    ),
    "storage": Node(
        "storage",
        (
            _f("default_schema", str, "The shared schema notebooks read and write by default."),
            _f("feature_schema", dict, "Optional per-feature schema isolation.", node="feature_schema"),
        ),
    ),
    "feature_schema": Node(
        "feature_schema",
        (
            _f("enabled", bool, "Whether the solution's notebooks use a per-feature schema. Default false."),
            _f("pattern", str, "Schema name pattern; must contain {feature} or {developer}."),
            _f("drop_on_teardown", bool, "Drop the feature schema when the feature is reaped. Default false."),
            _f("lakehouses", list, "Lakehouses holding feature schemas, as '<layer>/<item>' references."),
        ),
    ),
    "branch": Node(
        "branch",
        (
            _f("pattern", str, "Branch naming pattern, e.g. feature/{developer}/{topic}."),
            _f("layer_from_branch", bool, "Derive the layer from the branch name."),
        ),
    ),
}

# Internal keys the normalizer produces from legacy aliases; accepted everywhere.
_INTERNAL_PREFIX = "_"


def validate(recipe: dict[str, Any], *, source: str = "recipe") -> list[str]:
    """Validate `recipe`, returning warnings. Errors raise `RecipeError`.

    Messages are path-qualified (`layers.Store.items[0].typ`) and suggest the intended
    key when a close match exists - including legacy aliases, so an old spelling is
    reported as accepted rather than unknown.
    """
    problems: list[str] = []
    warnings: list[str] = []
    _validate_node(recipe, NODES["root"], "", problems, warnings)

    storage = recipe.get("storage") or {}
    feature_schema = storage.get("feature_schema") or {}
    if isinstance(feature_schema, dict) and feature_schema.get("pattern"):
        problems.extend(validate_schema_pattern(str(feature_schema["pattern"]), "storage.feature_schema.pattern"))

    if problems:
        listing = "\n".join(f"  - {problem}" for problem in problems)
        raise RecipeError(f"{source} is not valid:\n{listing}")
    return warnings


def _validate_node(node: Any, spec: Node, path: str, problems: list[str], warnings: list[str]) -> None:
    if not isinstance(node, dict):
        problems.append(f"{path or 'recipe'}: expected a mapping, got {type(node).__name__}")
        return

    known = set(spec.field_names())
    for key, value in node.items():
        here = f"{path}.{key}" if path else str(key)
        if key.startswith(_INTERNAL_PREFIX) or key in ("$merge", "merge_type", "$schema"):
            continue
        if spec.free_form:
            _validate_free_form_child(key, value, spec, here, problems, warnings)
            continue
        if key not in known:
            problems.append(f"{here}: unknown property{_suggest(key, known)}")
            continue
        field_spec = next(f for f in spec.fields if f.name == key)
        _validate_field(field_spec, value, here, problems, warnings)

    for field_spec in spec.fields:
        if field_spec.required and field_spec.name not in node:
            problems.append(f"{path or 'recipe'}: missing required property '{field_spec.name}'")


def _validate_free_form_child(key: str, value: Any, spec: Node, path: str, problems: list[str], warnings: list[str]) -> None:
    if spec.name == "layers" and isinstance(value, dict):
        _validate_node(value, NODES["layer"], path, problems, warnings)


def _validate_field(spec: Field, value: Any, path: str, problems: list[str], warnings: list[str]) -> None:
    if value is None:
        return
    if not isinstance(value, spec.types):
        expected = " or ".join(t.__name__ for t in spec.types)
        problems.append(f"{path}: expected {expected}, got {type(value).__name__}")
        return
    if spec.choices and isinstance(value, str) and value not in spec.choices:
        problems.append(f"{path}: '{value}' is not valid (expected one of: {', '.join(spec.choices)})")
    if spec.node and isinstance(value, dict):
        _validate_node(value, NODES[spec.node], path, problems, warnings)
    if spec.of_node and isinstance(value, list):
        for index, item in enumerate(value):
            _validate_node(item, NODES[spec.of_node], f"{path}[{index}]", problems, warnings)
    if spec.of_node and isinstance(value, dict) and not spec.node:
        # Legacy shape: a mapping of item type -> list of items (normalised on load).
        for key, entries in value.items():
            if key in ("$merge", "merge_type"):
                continue
            if not isinstance(entries, list):
                problems.append(f"{path}.{key}: expected a list of items, got {type(entries).__name__}")
                continue
            for index, item in enumerate(entries):
                _validate_node(item, NODES[spec.of_node], f"{path}.{key}[{index}]", problems, warnings)


SCHEMA_TOKEN = re.compile(r"\{(feature|developer)\}")
SCHEMA_LITERAL = re.compile(r"^[A-Za-z0-9_{}a-z]*$")


def validate_schema_pattern(pattern: str, path: str) -> list[str]:
    """A feature schema name must survive Fabric's rules once the token is substituted."""
    problems: list[str] = []
    if not SCHEMA_TOKEN.search(pattern):
        problems.append(f"{path}: '{pattern}' must contain {{feature}} or {{developer}}, or every feature shares a schema")
    literal = SCHEMA_TOKEN.sub("", pattern)
    if literal and not re.fullmatch(r"[A-Za-z0-9_]*", literal):
        problems.append(
            f"{path}: '{pattern}' contains characters a lakehouse schema name cannot hold "
            "(letters, numbers and underscore only)"
        )
    return problems


def _suggest(key: str, known: set[str]) -> str:
    if key in ALL_ALIASES:
        return f" (legacy key; the canonical name is '{ALL_ALIASES[key]}')"
    matches = difflib.get_close_matches(key, sorted(known), n=1, cutoff=0.6)
    return f" (did you mean '{matches[0]}'?)" if matches else ""


def reference_rows() -> list[tuple[str, str, str, str]]:
    """(node, property, types, description) rows for the generated documentation."""
    rows: list[tuple[str, str, str, str]] = []
    for node in NODES.values():
        for field_spec in node.fields:
            rows.append(
                (
                    node.name,
                    field_spec.name,
                    " | ".join(t.__name__ for t in field_spec.types),
                    field_spec.description,
                )
            )
    return rows
