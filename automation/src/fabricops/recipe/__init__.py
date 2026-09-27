"""Recipe layer: load, normalise, merge, validate, render."""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any

from ..errors import RecipeError
from . import loader, resolver
from .merge import merge
from .normalize import normalize
from .schema import validate
from .tokens import substitute

__all__ = [
    "Recipe",
    "load",
    "load_platform",
    "load_feature",
    "loader",
    "merge",
    "normalize",
    "resolver",
    "substitute",
    "validate",
]


@dataclass
class Recipe:
    """A fully resolved, merged, validated and token-substituted recipe."""

    data: dict[str, Any]
    sources: tuple[pathlib.Path, ...]
    kind: str = "Platform"
    solution: str | None = None
    environment: str | None = None
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # --------------------------------------------------------------- accessors
    @property
    def api_version(self) -> str:
        return str(self.data.get("apiVersion") or "fabricops/v1")

    @property
    def display_name_pattern(self) -> str:
        pattern = self.data.get("display_name_pattern")
        if not pattern:
            raise RecipeError(
                "recipe has no display_name_pattern",
                hint="Add `display_name_pattern: \"Sales - {layer} [{environment}]\"` (legacy key: `name`).",
            )
        return str(pattern)

    @property
    def defaults(self) -> dict[str, Any]:
        return dict(self.data.get("defaults") or {})

    @property
    def layers(self) -> dict[str, dict[str, Any]]:
        return dict(self.data.get("layers") or {})

    def layer(self, name: str) -> dict[str, Any]:
        try:
            return self.layers[name]
        except KeyError as exc:
            raise RecipeError(f"layer '{name}' is not defined (have: {', '.join(self.layers) or 'none'})") from exc

    def items(self, layer: str) -> list[dict[str, Any]]:
        return list(self.layer(layer).get("items") or [])

    def workspace_name(self, layer: str, **extra: Any) -> str:
        """The workspace display name for `layer`, with `{layer}` (and friends) resolved.

        `extra` carries the tokens a feature run needs - `feature`, `developer`, `branch`.
        """
        context = {
            "layer": layer,
            "environment": self.environment,
            "solution": self.solution,
            **{key: value for key, value in extra.items() if value is not None},
        }
        return str(substitute(self.display_name_pattern, context, path="display_name_pattern"))

    def value(self, layer: str, key: str, default: Any = None) -> Any:
        """A layer value, falling back to `defaults` (capacity, tags, permissions, ...)."""
        layer_definition = self.layer(layer)
        if key in layer_definition:
            return layer_definition[key]
        return self.defaults.get(key, default)

    def tags_for(self, layer: str | None = None, item: dict[str, Any] | None = None) -> list[str]:
        """Union of default, layer and item tags, de-duplicated, order-stable."""
        collected: list[str] = list(self.defaults.get("tags") or [])
        if layer:
            collected += list(self.layer(layer).get("tags") or [])
        if item:
            collected += list(item.get("tags") or [])
        seen: set[str] = set()
        return [tag for tag in collected if not (tag in seen or seen.add(tag))]

    # ------------------------------------------------------------------ storage
    @property
    def storage(self) -> dict[str, Any]:
        """The storage block, with today's behaviour as the default (E07)."""
        configured = dict(self.data.get("storage") or {})
        feature_schema = dict(configured.get("feature_schema") or {})
        return {
            "default_schema": configured.get("default_schema", "dbo"),
            "feature_schema": {
                "enabled": bool(feature_schema.get("enabled", False)),
                "pattern": feature_schema.get("pattern", "dev_{feature}"),
                "drop_on_teardown": bool(feature_schema.get("drop_on_teardown", False)),
                "lakehouses": list(feature_schema.get("lakehouses") or []),
            },
        }

    def feature_schema_name(self, *, feature: str | None = None, developer: str | None = None) -> str:
        """The schema a feature writes to, sanitised to Fabric's naming rules."""
        import re as _re

        pattern = str(self.storage["feature_schema"]["pattern"])
        rendered = pattern.format(feature=feature or "", developer=developer or "")
        return _re.sub(r"[^A-Za-z0-9_]+", "_", rendered).strip("_").lower()

    def render(self, fmt: str = "yaml") -> str:
        return loader.dump(self.data, fmt)


def load(
    files: list[pathlib.Path] | tuple[pathlib.Path, ...],
    *,
    kind: str = "Platform",
    solution: str | None = None,
    environment: str | None = None,
    developer: str | None = None,
    context: dict[str, Any] | None = None,
    validate_recipe: bool = True,
) -> Recipe:
    """Load, normalise, merge, validate and substitute tokens for `files` in order."""
    merged: dict[str, Any] = {}
    notes: list[str] = []
    for path in files:
        raw = loader.load_file(path)
        canonical, file_notes = normalize(raw)
        notes.extend(f"{pathlib.Path(path).name}: {note}" for note in file_notes)
        merged = merge(merged, canonical) if merged else canonical

    warnings = validate(merged, source=" + ".join(str(p) for p in files)) if validate_recipe else []

    token_context: dict[str, Any] = {
        "environment": environment,
        "solution": solution or (merged.get("metadata") or {}).get("solution"),
        "developer": developer,
        "capacity": (merged.get("defaults") or {}).get("capacity"),
    }
    token_context.update(context or {})
    substituted = substitute(merged, token_context, deferred=("layer", "feature", "branch"))

    return Recipe(
        data=substituted,
        sources=tuple(pathlib.Path(p) for p in files),
        kind=str(substituted.get("kind") or kind),
        solution=token_context["solution"],
        environment=environment,
        notes=notes,
        warnings=warnings,
    )


def load_platform(
    resources: str | pathlib.Path = resolver.DEFAULT_RESOURCES,
    *,
    solution: str | None = None,
    environment: str | None = None,
    validate_recipe: bool = True,
) -> Recipe:
    resolved = resolver.resolve_platform(resources, solution=solution, environment=environment)
    return load(
        list(resolved.files),
        kind="Platform",
        solution=solution,
        environment=environment,
        validate_recipe=validate_recipe,
    )


def load_feature(
    resources: str | pathlib.Path = resolver.DEFAULT_RESOURCES,
    *,
    solution: str | None = None,
    developer: str | None = None,
    branch: str | None = None,
    group: str | None = None,
    validate_recipe: bool = True,
) -> Recipe:
    developer = developer or resolver.resolve_developer()
    resolved = resolver.resolve_feature(resources, solution=solution, developer=developer, group=group)
    loaded = load(
        list(resolved.files),
        kind="Feature",
        solution=solution,
        developer=developer,
        context={"branch": branch},
        validate_recipe=validate_recipe,
    )
    if group:
        _select_group_layers(loaded, resolved, group)
    return loaded


def _select_group_layers(loaded: "Recipe", resolved, group: str) -> None:
    """A group recipe's `layers` is a selection, not an addition.

    Overlays merge, and a mapping merge is a union - so a group file that listed Ingest and
    Prepare still produced every layer in feature.json, which is the opposite of what a
    group is for. The group *names* its layers and *inherits* their definitions from the
    base; whatever it says about a layer merges on top as before; whatever it does not name
    is dropped. Developer overlays keep merging as they always did - they tune settings,
    they do not pick layers. `always: true` is a rule for the layer-name path
    (`feature/prepare/x`); a group is explicit, and explicit wins.
    """
    group_files = [path for path in resolved.overlays if pathlib.Path(path).stem.startswith(f"feature.{group}")]
    if not group_files:
        return
    canonical, _notes = normalize(loader.load_file(group_files[0]))
    named = [key for key in (canonical.get("layers") or {}) if key not in ("$merge", "merge_type")]
    if not named:
        return                      # a group that only tunes settings selects nothing
    # Judged against feature.json alone. The merged recipe already contains whatever the
    # group named - that is what a union does - so checking it there found nothing unknown,
    # and a typo became a layer with no definition instead of an error.
    base_canonical, _notes = normalize(loader.load_file(resolved.base))
    defined = {key for key in (base_canonical.get("layers") or {}) if key not in ("$merge", "merge_type")}
    unknown = [name for name in named if name not in defined]
    if unknown:
        raise RecipeError(
            f"group '{group}' names layer(s) {', '.join(unknown)} that the feature recipe does not define",
            hint="A group selects layers from feature.json; it cannot introduce one. "
                 f"Defined: {', '.join(sorted(defined)) or 'none'}.",
        )
    loaded.data = {**loaded.data, "layers": {name: loaded.layers[name] for name in loaded.layers if name in named}}
