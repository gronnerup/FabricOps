"""Generate variable library item definitions from a recipe declaration (E06).

A variable library is a *workspace item*, and consumers must live in the same workspace.
So one declaration in the recipe becomes one item per consuming workspace, with a value
set per environment. The recipe stays the single source of truth; the library is a
deployed projection of it.

Definition parts, per the Fabric item definition reference:

    variables.json          required - the variables and their default values
    settings.json           required - value set ordering
    valueSets/<name>.json   optional - per-environment overrides
    .platform               optional here, required by git integration and fabric-cicd
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..errors import RecipeError

ITEM_TYPE = "VariableLibrary"

# The item-definition reference is inconsistent here: the parts table says
# `valueSets\valueSetName.json` while the payload example shows `valueSet/…`. Git
# integration uses the plural form, so that is what we write - and it is one constant to
# change if that turns out to be wrong.
VALUE_SET_DIR = "valueSets"

SCHEMA_VARIABLES = "https://developer.microsoft.com/json-schemas/fabric/item/variableLibrary/definition/variables/1.0.0/schema.json"
SCHEMA_VALUE_SET = "https://developer.microsoft.com/json-schemas/fabric/item/variableLibrary/definition/valueSet/1.0.0/schema.json"
SCHEMA_SETTINGS = "https://developer.microsoft.com/json-schemas/fabric/item/variableLibrary/definition/settings/1.0.0/schema.json"
SCHEMA_PLATFORM = "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json"

VARIABLE_TYPES = ("String", "Integer", "Number", "Boolean", "DateTime", "Guid", "ItemReference", "ConnectionReference")

# A stable namespace, so regenerating produces the same logicalId rather than a new item.
LOGICAL_ID_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


@dataclass
class VariableLibraryPlan:
    """One generated library: which workspace it belongs to, and its definition parts."""

    name: str
    layer: str
    parts: dict[str, str] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)

    @property
    def folder(self) -> str:
        return f"{self.name}.{ITEM_TYPE}"


def logical_id(solution: str | None, layer: str, name: str) -> str:
    return str(uuid.uuid5(LOGICAL_ID_NAMESPACE, f"fabricops/{solution or 'default'}/{layer}/{name}"))


def render_variable_library(
    declaration: dict[str, Any],
    *,
    layer: str,
    solution: str | None,
    environments: Iterable[str],
    resolve_reference,
) -> VariableLibraryPlan:
    """Render one library for one workspace.

    `resolve_reference(environment, spec)` returns `{"workspaceId": …, "itemId": …}` for an
    ItemReference variable, or None when that environment has not been provisioned yet -
    in which case the override is omitted and reported in `unresolved`, rather than
    silently written with the wrong ids.
    """
    name = str(declaration.get("name") or "").strip()
    if not name:
        raise RecipeError(f"layers.{layer}.variable_libraries: every library needs a name")

    value_sets = [str(value) for value in (declaration.get("value_sets") or environments)]
    default_set = value_sets[0] if value_sets else None
    variables = list(declaration.get("variables") or [])
    plan = VariableLibraryPlan(name=name, layer=layer)

    rendered_variables: list[dict[str, Any]] = []
    overrides: dict[str, list[dict[str, Any]]] = {value_set: [] for value_set in value_sets}

    for variable in variables:
        variable_name = str(variable.get("name") or "").strip()
        variable_type = str(variable.get("type") or "String")
        if not variable_name:
            raise RecipeError(f"layers.{layer}.variable_libraries.{name}: every variable needs a name")
        if variable_type not in VARIABLE_TYPES:
            raise RecipeError(
                f"layers.{layer}.variable_libraries.{name}.{variable_name}: "
                f"unknown type '{variable_type}' (expected one of: {', '.join(VARIABLE_TYPES)})"
            )

        per_set = dict(variable.get("values") or {})
        entry: dict[str, Any] = {"name": variable_name, "type": variable_type}
        if variable.get("note"):
            entry["note"] = str(variable["note"])

        for index, value_set in enumerate(value_sets):
            raw = per_set.get(value_set, variable.get("value"))
            if variable_type == "ItemReference":
                resolved = resolve_reference(value_set, raw)
                if resolved is None:
                    plan.unresolved.append(f"{variable_name}@{value_set}")
                    continue
                value: Any = resolved
            else:
                value = raw

            if index == 0:
                entry["value"] = value          # the first value set supplies the default
            else:
                overrides[value_set].append({"name": variable_name, "value": value})

        entry.setdefault("value", None)
        rendered_variables.append(entry)

    plan.parts["variables.json"] = _json({"$schema": SCHEMA_VARIABLES, "variables": rendered_variables})
    plan.parts["settings.json"] = _json({"$schema": SCHEMA_SETTINGS, "valueSetsOrder": value_sets})
    for value_set in value_sets[1:]:                 # the first set is the default, not a file
        plan.parts[f"{VALUE_SET_DIR}/{value_set}.json"] = _json(
            {"$schema": SCHEMA_VALUE_SET, "name": value_set, "variableOverrides": overrides[value_set]}
        )
    plan.parts[".platform"] = _json(
        {
            "$schema": SCHEMA_PLATFORM,
            "metadata": {
                "type": ITEM_TYPE,
                "displayName": name,
                "description": str(declaration.get("description") or "Generated by FabricOps"),
            },
            "config": {"version": "2.0", "logicalId": logical_id(solution, layer, name)},
        }
    )
    _ = default_set
    return plan


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
