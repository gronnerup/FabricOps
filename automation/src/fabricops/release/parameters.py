"""The parameter overlay: what a run generates, kept out of what you wrote (E09 §3).

`parameter.yml` is hand-authored and committed, and FabricOps never writes to it. Anything
that can only be known at deploy time is rendered into a separate overlay file that the
committed file pulls in with `extend:`. The old script upserted generated entries straight
into the file you edit, which made `git diff` on it meaningless.

Order of preference for expressing an environment-specific value, per E09 §3:

1. fabric-cicd's own dynamic notation (`$workspace…`, `$items…`, `$sqlendpoint`) - it is
   resolved at deploy time and survives a workspace being re-created, so it belongs in the
   committed file and needs nothing from this module;
2. `$ENV:` values, for what the pipeline already knows;
3. an overlay entry - only for what neither of the above can express, which today means
   connection ids that have to be looked up by name.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..errors import RecipeError
from ..recipe import Recipe

DEFAULT_PARAMETER_FILE = pathlib.Path("automation/resources/parameters/parameter.yml")
OVERLAY_RELATIVE = "./generated/dynamic.parameter.yml"
LEGACY_BINDING_FILE = "sqlendpoint_model_binding.yml"

HEADER = """\
# GENERATED - do not edit, and do not commit a hand edit here.
#
# Rendered by `fabricops release` for a single run, and pulled into the committed
# parameter.yml through its `extend:` list. Everything in here is something that could not
# be expressed with fabric-cicd's dynamic notation, which is why it needs generating at
# all. See documentation/specs/E09 section 3.
"""

#: Resolves a connection display name to its id. Returns None when the connection does not
#: exist, which is a warning rather than a failure - the model simply stays unbound.
ConnectionResolver = Callable[[str], str | None]


@dataclass
class Overlay:
    """The generated half of the parameter file."""

    environment: str
    semantic_model_binding: dict[str, Any] = field(default_factory=dict)
    find_replace: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if self.find_replace:
            body["find_replace"] = self.find_replace
        if self.semantic_model_binding:
            body["semantic_model_binding"] = self.semantic_model_binding
        return body

    @property
    def is_empty(self) -> bool:
        return not self.to_dict()


def bindings_declared(recipe: Recipe, layers: Iterable[str] | None = None) -> list[tuple[str, str, str]]:
    """Every `binding.connection` a SemanticModel item declares, as (layer, model, connection)."""
    found: list[tuple[str, str, str]] = []
    for layer in list(layers or recipe.layers):
        for item in recipe.items(layer):
            if item.get("type") != "SemanticModel":
                continue
            binding = item.get("binding") or {}
            connection = binding.get("connection") or binding.get("connection_name")
            if connection:
                found.append((layer, str(item["name"]), str(connection)))
    return found


def render(
    recipe: Recipe,
    *,
    environment: str,
    resolve_connection: ConnectionResolver,
    layers: Iterable[str] | None = None,
) -> Overlay:
    """Build the overlay for one release run."""
    overlay = Overlay(environment=environment)

    models: list[dict[str, Any]] = []
    for layer, model, connection in bindings_declared(recipe, layers):
        connection_id = resolve_connection(connection)
        if not connection_id:
            overlay.warnings.append(
                f"layers.{layer}.items.{model}: connection '{connection}' not found, "
                "so the semantic model will not be bound"
            )
            continue
        models.append({
            "semantic_model_name": [model],
            "connection_id": {environment: connection_id},
        })

    if models:
        overlay.semantic_model_binding = {"models": models}
    return overlay


def merge_legacy_bindings(
    overlay: Overlay,
    recipe: Recipe,
    *,
    legacy_path: pathlib.Path,
    resolve_connection: ConnectionResolver,
) -> Overlay:
    """Translate `sqlendpoint_model_binding.yml` into overlay entries, once, with a warning.

    The old file names a lakehouse and a list of models; the connection it means is the one
    the recipe already declares on that lakehouse, so the translation is a lookup rather
    than a guess.
    """
    if not legacy_path.exists():
        return overlay

    from ..recipe import loader

    document = loader.load_file(legacy_path) or {}
    entries = document.get("semantic_model_sqlendpoint_binding") or []
    if not entries:
        return overlay

    overlay.warnings.append(
        f"{legacy_path.name} is deprecated: declare `binding: {{ connection: ... }}` on the "
        "SemanticModel item in the recipe instead. Translated for this run."
    )

    already_bound = {
        name
        for entry in overlay.semantic_model_binding.get("models", [])
        for name in entry["semantic_model_name"]
    }
    models = list(overlay.semantic_model_binding.get("models", []))

    for entry in entries:
        connection = _lakehouse_connection(recipe, entry.get("lakehouse_layer"), entry.get("lakehouse_name"))
        if not connection:
            overlay.warnings.append(
                f"{legacy_path.name}: no connection declared for lakehouse "
                f"'{entry.get('lakehouse_name')}' in layer '{entry.get('lakehouse_layer')}'; skipped"
            )
            continue
        connection_id = resolve_connection(connection)
        if not connection_id:
            overlay.warnings.append(f"{legacy_path.name}: connection '{connection}' not found; skipped")
            continue
        names = [str(name) for name in (entry.get("semantic_models") or []) if str(name) not in already_bound]
        if names:
            models.append({"semantic_model_name": names, "connection_id": {overlay.environment: connection_id}})

    if models:
        overlay.semantic_model_binding = {"models": models}
    return overlay


def _lakehouse_connection(recipe: Recipe, layer: Any, name: Any) -> str | None:
    if not layer or not name or layer not in recipe.layers:
        return None
    for item in recipe.items(str(layer)):
        if item.get("name") == name and item.get("type") == "Lakehouse":
            return (item.get("connection") or {}).get("name")
    return None


def write(overlay: Overlay, parameter_file: pathlib.Path) -> pathlib.Path:
    """Write the overlay next to the committed parameter file, and return its path.

    An empty overlay is still written. A committed `extend:` pointing at a file that does
    not exist is a deployment failure, and "there was nothing to generate this run" is a
    perfectly normal state.
    """
    from ..recipe import loader

    target = (parameter_file.parent / OVERLAY_RELATIVE).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    body = overlay.to_dict()
    if not body:
        # A template of comments alone parses to nothing, and fabric-cicd then warns "None of
        # the template parameter files were valid or found" on every single release. An empty
        # list is a valid document and says the same thing without the alarm.
        body = {"find_replace": []}
    rendered = HEADER + "\n" + loader.dump(body, "yaml")
    target.write_text(rendered, encoding="utf-8")
    return target


def check_extends(parameter_file: pathlib.Path) -> str | None:
    """Warn if the committed file will not pick the overlay up.

    FabricOps deliberately does not add the `extend:` entry itself - that file belongs to
    whoever wrote it, and silently editing it is the behaviour this design is replacing.
    """
    if not parameter_file.exists():
        return f"{parameter_file} does not exist; nothing will be parameterised"

    from ..recipe import loader

    try:
        document = loader.load_file(parameter_file) or {}
    except Exception as error:  # noqa: BLE001 - a broken parameter file is the user's to fix
        raise RecipeError(f"{parameter_file}: {error}") from error

    extends = [str(entry) for entry in (document.get("extend") or [])]
    if OVERLAY_RELATIVE in extends or OVERLAY_RELATIVE.removeprefix("./") in extends:
        return None
    return (
        f"{parameter_file} has no `extend: [\"{OVERLAY_RELATIVE}\"]` entry, so generated "
        "parameters will be ignored. Add it, or run with --extend-parameters false."
    )
