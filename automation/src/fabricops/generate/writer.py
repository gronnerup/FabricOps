"""Turn recipe declarations into generated item definitions on disk (E06).

Generated artefacts live under `automation/generated/<solution>/<layer>/…` so that what a
machine wrote is never mixed with what a person wrote.
"""

from __future__ import annotations

import pathlib
from typing import Any, Iterable

from ..engine.manifest import DEFAULT_ROOT, Manifest
from ..errors import RecipeError
from ..recipe import Recipe
from .variable_library import VariableLibraryPlan, render_variable_library

GENERATED_ROOT = pathlib.Path("automation/generated")


def declarations(recipe: Recipe) -> list[tuple[str, dict[str, Any]]]:
    """Every variable library declared in the recipe, with the layer that declared it."""
    found: list[tuple[str, dict[str, Any]]] = []
    for layer in recipe.layers:
        for declaration in recipe.layer(layer).get("variable_libraries") or []:
            found.append((layer, declaration))
    return found


def target_layers(recipe: Recipe, layer: str, declaration: dict[str, Any]) -> list[str]:
    """Which workspaces get a copy - the declaring layer unless told otherwise."""
    requested = [str(name) for name in (declaration.get("workspaces") or [layer])]
    unknown = [name for name in requested if name not in recipe.layers]
    if unknown:
        raise RecipeError(
            f"layers.{layer}.variable_libraries.{declaration.get('name')}: "
            f"unknown workspace(s) {', '.join(unknown)}",
            hint=f"Known layers: {', '.join(recipe.layers)}",
        )
    return requested


def reference_resolver(environments: Iterable[str], manifest_root: str | pathlib.Path = DEFAULT_ROOT):
    """Resolve ItemReference values from the manifests of previous runs, per environment."""
    manifests = {
        environment: Manifest.latest_for(environment, manifest_root) for environment in environments
    }

    def resolve(environment: str, spec: Any) -> dict[str, str] | None:
        if not isinstance(spec, dict):
            return None
        manifest = manifests.get(environment)
        if not manifest:
            return None
        layer, item, item_type = spec.get("layer"), spec.get("item"), spec.get("type", "Lakehouse")
        outputs = manifest.get("outputs") or {}
        workspace = outputs.get(f"workspace:{layer}", {}).get("id")
        target = outputs.get(f"item:{layer}:{item_type}:{item}", {}).get("id")
        if not workspace or not target:
            return None
        return {"workspaceId": str(workspace), "itemId": str(target)}

    return resolve


def render_all(
    recipe: Recipe,
    *,
    environments: list[str],
    manifest_root: str | pathlib.Path = DEFAULT_ROOT,
) -> list[VariableLibraryPlan]:
    """Every generated library, one per (declaration × target workspace)."""
    resolve = reference_resolver(environments, manifest_root)
    plans: list[VariableLibraryPlan] = []
    for layer, declaration in declarations(recipe):
        for target in target_layers(recipe, layer, declaration):
            plans.append(
                render_variable_library(
                    declaration,
                    layer=target,
                    solution=recipe.solution,
                    environments=environments,
                    resolve_reference=resolve,
                )
            )
    return plans


def write(plans: Iterable[VariableLibraryPlan], root: str | pathlib.Path, solution: str | None) -> list[pathlib.Path]:
    """Write the generated definitions, returning the folders written."""
    base = pathlib.Path(root) / (solution or "default")
    written: list[pathlib.Path] = []
    for plan in plans:
        folder = base / plan.layer.lower() / plan.folder
        folder.mkdir(parents=True, exist_ok=True)
        for relative, content in plan.parts.items():
            path = folder / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        written.append(folder)
    return written
