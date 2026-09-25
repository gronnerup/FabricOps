"""Legacy key aliases - permanent, per decision 4.

Recipes written for the first generation of FabricOps keep working forever. Aliases are
applied by the loader *before* validation, so everything downstream sees canonical names
only, and the alias table is the single source of truth for both the normalizer and the
generated recipe reference documentation.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------- key aliases
# legacy key -> canonical key, per node kind.
ROOT_ALIASES: dict[str, str] = {
    "name": "display_name_pattern",
    "generic": "defaults",
    "feature_name": "display_name_pattern",
}

DEFAULTS_ALIASES: dict[str, str] = {
    "capacity_name": "capacity",
    "git_settings": "git",
    "fabric_connections": "connections",
    "environment_name": "environment",
}

LAYER_ALIASES: dict[str, str] = {
    "capacity_name": "capacity",
    "create_workspace_identity": "workspace_identity",
    "always_provision": "always",
    "spark_settings": "_spark_settings",  # folded into `properties` by the normalizer
    "git_directoryName": "_git_directory",
    "git_directoryname": "_git_directory",
    "git_synchronize_on_commit": "_git_sync_on_commit",
    "git_disconnect_after_initialize": "_git_disconnect_after_init",
}

ITEM_ALIASES: dict[str, str] = {
    "item_name": "name",
    "item_type": "type",
    "skip_item_creation": "skip_creation",
    "connection_name": "_connection_name",
}

GIT_ALIASES: dict[str, str] = {
    "gitProviderType": "provider",
    "ownerName": "owner",
    "organizationName": "organization",
    "projectName": "project",
    "repositoryName": "repository",
    "branchName": "branch",
    "directoryName": "directory",
    "myGitCredentials": "credentials",
    "connection_name": "connection",
    "connectionId": "connection_id",
}

CONNECTION_ALIASES: dict[str, str] = {
    "auth_type": "auth",
}

# `merge_type` (0/1/2) maps onto the explicit strategies in merge.py.
MERGE_TYPE_TO_STRATEGY: dict[Any, str] = {
    0: "keep",
    1: "replace",
    2: "merge",
    "0": "keep",
    "1": "replace",
    "2": "merge",
}

# Every legacy key, for the "did you mean" suggester and the docs table.
ALL_ALIASES: dict[str, str] = {
    **ROOT_ALIASES,
    **DEFAULTS_ALIASES,
    **LAYER_ALIASES,
    **ITEM_ALIASES,
    **GIT_ALIASES,
    **CONNECTION_ALIASES,
}


def rename(node: dict[str, Any], table: dict[str, str]) -> dict[str, Any]:
    """Return `node` with legacy keys renamed, preserving insertion order.

    A canonical key already present always wins over its legacy alias, so a recipe that
    carries both is not ambiguous.
    """
    out: dict[str, Any] = {}
    for key, value in node.items():
        canonical = table.get(key, key)
        if canonical in out and canonical != key:
            continue  # canonical value already set - ignore the legacy duplicate
        out[canonical] = value
    for key, value in node.items():
        canonical = table.get(key, key)
        if canonical == key:
            out[key] = value
    return out


def used_aliases(node: dict[str, Any], table: dict[str, str]) -> list[str]:
    return [key for key in node if key in table]
