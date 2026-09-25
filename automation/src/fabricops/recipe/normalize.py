"""Turn a raw recipe (any generation, any format) into the canonical shape.

Everything downstream - planner, release, feature flow, parameter generation - sees only
canonical keys. Legacy spellings and the items-keyed-by-type shape are supported
permanently (decision 4), so this module is where the first generation of FabricOps
recipes is met and translated.
"""

from __future__ import annotations

from typing import Any

from .aliases import (
    CONNECTION_ALIASES,
    DEFAULTS_ALIASES,
    GIT_ALIASES,
    ITEM_ALIASES,
    LAYER_ALIASES,
    ROOT_ALIASES,
    rename,
    used_aliases,
)


def normalize(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return `(canonical_recipe, notes)`; `notes` lists the legacy keys encountered."""
    notes: list[str] = []
    root = rename(dict(raw), ROOT_ALIASES)
    _note(notes, raw, ROOT_ALIASES, "recipe")

    out: dict[str, Any] = {}
    if "apiVersion" in root:
        out["apiVersion"] = root.pop("apiVersion")
    if "kind" in root:
        out["kind"] = root.pop("kind")

    metadata = dict(root.pop("metadata", {}) or {})
    if "display_name_pattern" in root:
        metadata.setdefault("display_name_pattern", root.pop("display_name_pattern"))
    if metadata:
        rest = {k: v for k, v in metadata.items() if k != "display_name_pattern"}
        if rest:
            out["metadata"] = rest
        if "display_name_pattern" in metadata:
            out["display_name_pattern"] = metadata["display_name_pattern"]

    defaults = _normalize_defaults(root.pop("defaults", {}) or {}, notes)

    # First-generation feature recipes keep these at the root; fold them into defaults.
    for key in ("capacity", "capacity_name", "permissions", "git", "git_settings", "connections", "tags"):
        if key in root:
            value = root.pop(key)
            canonical = DEFAULTS_ALIASES.get(key, key)
            if canonical == "git":
                value = _normalize_git(value, notes, "defaults.git")
            defaults.setdefault(canonical, value)
    if defaults:
        out["defaults"] = defaults

    if "connections" in root:
        out["connections"] = [rename(dict(c), CONNECTION_ALIASES) for c in root.pop("connections") or []]

    for key in ("storage", "branch"):
        if key in root:
            out[key] = root.pop(key)

    if "layers" in root:
        out["layers"], layer_notes = _normalize_layers(root.pop("layers"))
        notes.extend(layer_notes)

    # Anything unrecognised is preserved so validation can report it by name.
    for key, value in root.items():
        if key in ("merge_type", "$merge"):
            continue
        out[key] = value

    return out, notes


def _normalize_defaults(raw: Any, notes: list[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    _note(notes, raw, DEFAULTS_ALIASES, "defaults")
    node = rename(dict(raw), DEFAULTS_ALIASES)
    if isinstance(node.get("git"), dict):
        node["git"] = _normalize_git(node["git"], notes, "defaults.git")
    if isinstance(node.get("connections"), list):
        node["connections"] = [rename(dict(c), CONNECTION_ALIASES) for c in node["connections"]]
    return node


def _normalize_git(raw: Any, notes: list[str], path: str) -> dict[str, Any]:
    """Flatten `gitProviderDetails` / `myGitCredentials` into one canonical `git` node."""
    if not isinstance(raw, dict):
        return {}
    node = dict(raw)
    details = node.pop("gitProviderDetails", None)
    if isinstance(details, dict):
        _note(notes, details, GIT_ALIASES, f"{path}.gitProviderDetails")
        node.update(details)
    node = rename(node, GIT_ALIASES)
    _note(notes, raw, GIT_ALIASES, path)

    credentials = node.get("credentials")
    if isinstance(credentials, dict):
        node["credentials"] = rename(dict(credentials), GIT_ALIASES)
    return node


def _normalize_layers(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """Accept a mapping of layer name -> definition, or a list with `name` on each."""
    notes: list[str] = []
    layers: dict[str, Any] = {}

    if isinstance(raw, list):
        pairs = [(item.get("name"), {k: v for k, v in item.items() if k != "name"}) for item in raw if isinstance(item, dict)]
    elif isinstance(raw, dict):
        pairs = [(key, value) for key, value in raw.items() if key not in ("merge_type", "$merge")]
    else:
        return {}, notes

    for name, definition in pairs:
        if not name:
            continue
        layers[str(name)], layer_notes = _normalize_layer(definition, f"layers.{name}")
        notes.extend(layer_notes)
    return layers, notes


def _normalize_layer(raw: Any, path: str) -> tuple[dict[str, Any], list[str]]:
    notes: list[str] = []
    if not isinstance(raw, dict):
        return {}, notes

    _note(notes, raw, LAYER_ALIASES, path)
    node = rename(dict(raw), LAYER_ALIASES)

    # spark_settings -> properties["sparkSettings.<path>"]
    spark = node.pop("_spark_settings", None)
    if isinstance(spark, dict):
        properties = dict(node.get("properties") or {})
        for key, value in _flatten(spark, "sparkSettings"):
            properties.setdefault(key, value)
        node["properties"] = properties

    # git_directoryName / git_synchronize_on_commit / git_disconnect_after_initialize -> git.*
    git_node = dict(node.get("git") or {})
    for internal, canonical in (
        ("_git_directory", "directory"),
        ("_git_sync_on_commit", "sync_on_commit"),
        ("_git_disconnect_after_init", "disconnect_after_initialize"),
    ):
        if internal in node:
            git_node.setdefault(canonical, node.pop(internal))
    if git_node:
        node["git"] = _normalize_git(git_node, notes, f"{path}.git")

    if "items" in node:
        node["items"], item_notes = _normalize_items(node["items"], f"{path}.items")
        notes.extend(item_notes)

    return node, notes


def _normalize_items(raw: Any, path: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Accept `{"Lakehouse": [{item_name: X}]}` (legacy) or a list carrying `type`."""
    notes: list[str] = []
    items: list[dict[str, Any]] = []

    if isinstance(raw, dict):
        notes.append(f"{path}: items keyed by type (legacy shape) - normalised to a list")
        for item_type, entries in raw.items():
            if item_type in ("merge_type", "$merge"):
                continue
            for entry in entries or []:
                if isinstance(entry, dict):
                    items.append(_normalize_item(dict(entry), item_type, notes, path))
    elif isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                items.append(_normalize_item(dict(entry), None, notes, path))

    return items, notes


def _normalize_item(entry: dict[str, Any], item_type: str | None, notes: list[str], path: str) -> dict[str, Any]:
    _note(notes, entry, ITEM_ALIASES, path)
    item = rename(entry, ITEM_ALIASES)
    if item_type and "type" not in item:
        item["type"] = item_type

    connection_name = item.pop("_connection_name", None)
    if connection_name and "connection" not in item:
        item["connection"] = {"name": connection_name}

    ordered = {key: item[key] for key in ("name", "type") if key in item}
    ordered.update({key: value for key, value in item.items() if key not in ordered})
    return ordered


def _flatten(node: dict[str, Any], prefix: str) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for key, value in node.items():
        full = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            out.extend(_flatten(value, full))
        else:
            out.append((full, value))
    return out


def _note(notes: list[str], node: Any, table: dict[str, str], path: str) -> None:
    if not isinstance(node, dict):
        return
    for key in used_aliases(node, table):
        notes.append(f"{path}.{key} -> {table[key]} (legacy key, still supported)")
