"""Release policy: what the recipe says about how a layer should be deployed (E09 §2).

Every knob `fabric-cicd`'s config file exposes is expressible here instead, per layer and
per environment, so a layered solution needs one recipe rather than one config file per
layer. See documentation/specs/E09 §1 for why the config file was rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import RecipeError
from ..recipe import Recipe

#: Arguments that do nothing unless the matching feature flag is also set. Passing one
#: without its flag is silently ignored by the library, which reads as "my exclude did not
#: work" - so FabricOps turns the flag on for you when you use the argument.
IMPLIED_FLAGS: dict[str, str] = {
    "items_to_include": "enable_items_to_include",
    "folder_exclude_regex": "enable_exclude_folder",
    "folder_path_to_include": "enable_include_folder",
    "shortcut_exclude_regex": "enable_shortcut_publish",
}

#: Unpublishing these destroys data, so the flag is never implied - it has to be asked for
#: by name in `deploy.features`.
DESTRUCTIVE_FLAGS: frozenset[str] = frozenset({
    "enable_lakehouse_unpublish",
    "enable_warehouse_unpublish",
    "enable_sqldatabase_unpublish",
    "enable_eventhouse_unpublish",
    "enable_kqldatabase_unpublish",
})

#: The key that means "every environment", matching fabric-cicd's parameter file.
ALL_ENVIRONMENTS = "_ALL_"

_SCALAR_FIELDS = (
    "item_types_in_scope",
    "exclude_regex",
    "items_to_include",
    "folder_exclude_regex",
    "folder_path_to_include",
    "shortcut_exclude_regex",
)
_UNPUBLISH_FIELDS = ("skip", "exclude_regex", "items_to_include")


@dataclass(frozen=True)
class DeployPolicy:
    """The resolved release policy for one layer in one environment."""

    layer: str
    environment: str | None = None
    item_types_in_scope: tuple[str, ...] | None = None
    exclude_regex: str | None = None
    items_to_include: tuple[str, ...] | None = None
    folder_exclude_regex: str | None = None
    folder_path_to_include: tuple[str, ...] | None = None
    shortcut_exclude_regex: str | None = None
    unpublish_skip: bool = False
    unpublish_exclude_regex: str | None = None
    unpublish_items_to_include: tuple[str, ...] | None = None
    features: tuple[str, ...] = ()
    constants: dict[str, Any] = field(default_factory=dict)

    @property
    def effective_features(self) -> tuple[str, ...]:
        """Declared flags, plus the ones the arguments in use cannot work without."""
        flags = list(self.features)
        for attribute, flag in IMPLIED_FLAGS.items():
            if getattr(self, attribute) and flag not in flags:
                flags.append(flag)
        return tuple(flags)

    @property
    def destructive_features(self) -> tuple[str, ...]:
        """Declared flags that let unpublish delete data-bearing items."""
        return tuple(flag for flag in self.features if flag in DESTRUCTIVE_FLAGS)

    def publish_arguments(self) -> dict[str, Any]:
        """Keyword arguments for `publish_all_items`, omitting anything unset."""
        arguments = {
            "item_name_exclude_regex": self.exclude_regex,
            "folder_path_exclude_regex": self.folder_exclude_regex,
            "folder_path_to_include": list(self.folder_path_to_include) if self.folder_path_to_include else None,
            "items_to_include": list(self.items_to_include) if self.items_to_include else None,
            "shortcut_exclude_regex": self.shortcut_exclude_regex,
        }
        return {key: value for key, value in arguments.items() if value is not None}

    def unpublish_arguments(self) -> dict[str, Any]:
        """Keyword arguments for `unpublish_all_orphan_items`.

        The library's own default for `item_name_exclude_regex` is `^$` (exclude nothing),
        not None, so it is spelled out rather than omitted.
        """
        arguments: dict[str, Any] = {"item_name_exclude_regex": self.unpublish_exclude_regex or "^$"}
        if self.unpublish_items_to_include:
            arguments["items_to_include"] = list(self.unpublish_items_to_include)
        return arguments


def for_environment(value: Any, environment: str | None) -> Any:
    """Resolve an environment-mapped value.

    `skip: true` applies everywhere; `skip: {prd: true}` applies to prd only. A mapping
    with an `_ALL_` key falls back to it. A mapping that names no environment at all
    resolves to None rather than being passed through as a dict, because every field this
    is applied to is a scalar or a list - a dict there can only be an environment map.
    """
    if not isinstance(value, dict):
        return value
    if environment is not None and environment in value:
        return value[environment]
    if ALL_ENVIRONMENTS in value:
        return value[ALL_ENVIRONMENTS]
    return None


def _tuple(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return (value,)
    return tuple(str(entry) for entry in value)


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """One level of nesting is all the deploy block has, so this is deliberately shallow."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict) and key in ("unpublish", "constants"):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def resolve(recipe: Recipe, layer: str, *, environment: str | None = None) -> DeployPolicy:
    """The deploy policy for `layer`: solution defaults overlaid with the layer's own."""
    environment = environment or recipe.environment
    defaults = dict(recipe.defaults.get("deploy") or {})
    declared = dict(recipe.layer(layer).get("deploy") or {})
    block = _merge(defaults, declared)

    values = {name: for_environment(block.get(name), environment) for name in _SCALAR_FIELDS}
    unpublish_block = block.get("unpublish") or {}
    if not isinstance(unpublish_block, dict):
        raise RecipeError(
            f"layers.{layer}.deploy.unpublish must be a mapping, got {type(unpublish_block).__name__}",
            hint="Use `unpublish: { skip: true }` or `unpublish: { skip: { prd: true } }`.",
        )
    unpublish = {name: for_environment(unpublish_block.get(name), environment) for name in _UNPUBLISH_FIELDS}

    features = _tuple(for_environment(block.get("features"), environment)) or ()
    constants = for_environment(block.get("constants"), environment)
    if constants is not None and not isinstance(constants, dict):
        constants = None

    return DeployPolicy(
        layer=layer,
        environment=environment,
        item_types_in_scope=_tuple(values["item_types_in_scope"]),
        exclude_regex=values["exclude_regex"],
        items_to_include=_tuple(values["items_to_include"]),
        folder_exclude_regex=values["folder_exclude_regex"],
        folder_path_to_include=_tuple(values["folder_path_to_include"]),
        shortcut_exclude_regex=values["shortcut_exclude_regex"],
        unpublish_skip=bool(unpublish["skip"]),
        unpublish_exclude_regex=unpublish["exclude_regex"],
        unpublish_items_to_include=_tuple(unpublish["items_to_include"]),
        features=features,
        constants=dict(constants or {}),
    )


def order(recipe: Recipe, layers: list[str] | None = None) -> list[str]:
    """Layer release order: declaration order, adjusted so dependencies publish first.

    A Report in Present that binds a SemanticModel in Model has to see the model's real
    item id, which only exists once Model is published. Declaration order usually gets
    this right already; this makes it true rather than lucky.
    """
    selected = list(layers or recipe.layers)
    unknown = [layer for layer in selected if layer not in recipe.layers]
    if unknown:
        raise RecipeError(
            f"unknown layer(s): {', '.join(unknown)}",
            hint=f"Known layers: {', '.join(recipe.layers)}",
        )

    # Release order is the recipe's, not the order someone happened to type --layers in.
    selected = [layer for layer in recipe.layers if layer in set(selected)]
    dependencies = {layer: _layer_dependencies(recipe, layer) & set(selected) - {layer} for layer in selected}

    ordered: list[str] = []
    remaining = list(selected)
    while remaining:
        ready = [layer for layer in remaining if dependencies[layer] <= set(ordered)]
        if not ready:
            # A cycle is a recipe bug, but refusing to release is worse than releasing in
            # declaration order and saying so.
            ordered.extend(remaining)
            break
        ordered.append(ready[0])
        remaining.remove(ready[0])
    return ordered


def _layer_dependencies(recipe: Recipe, layer: str) -> set[str]:
    """Layers holding items that this layer's items reference."""
    found: set[str] = set()
    for item in recipe.items(layer):
        payload = item.get("creation_payload") or {}
        model = payload.get("semanticModel") or payload.get("semantic_model")
        if not model:
            continue
        declared_layer = payload.get("semanticModelLayer")
        candidates = [declared_layer] if declared_layer else list(recipe.layers)
        for candidate in candidates:
            if candidate == layer or candidate not in recipe.layers:
                continue
            if any(o.get("type") == "SemanticModel" and o.get("name") == model for o in recipe.items(candidate)):
                found.add(candidate)
                break
    return found
