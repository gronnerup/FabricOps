"""Load a recipe file - JSON or YAML - into a plain dict.

Both formats parse to the same structure, so there is no on-disk translation step: the
canonical thing is the *model*, not a file format (documentation/specs/E01).
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from ..errors import RecipeError

JSON_SUFFIXES = (".json",)
YAML_SUFFIXES = (".yml", ".yaml")
SUPPORTED_SUFFIXES = JSON_SUFFIXES + YAML_SUFFIXES


def load_file(path: str | pathlib.Path) -> dict[str, Any]:
    """Parse a recipe file. Duplicate keys are an error in both formats."""
    path = pathlib.Path(path)
    if not path.exists():
        raise RecipeError(f"recipe file not found: {path}")

    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8-sig")

    if suffix in JSON_SUFFIXES:
        data = _load_json(text, path)
    elif suffix in YAML_SUFFIXES:
        data = _load_yaml(text, path)
    else:
        raise RecipeError(
            f"{path}: unsupported recipe format '{suffix or path.name}'",
            hint=f"Use one of: {', '.join(SUPPORTED_SUFFIXES)}",
        )

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise RecipeError(f"{path}: a recipe must be a mapping, got {type(data).__name__}")
    return data


def _load_json(text: str, path: pathlib.Path) -> Any:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: dict[str, Any] = {}
        for key, value in pairs:
            if key in seen:
                raise RecipeError(f"{path}: duplicate key '{key}'")
            seen[key] = value
        return seen

    try:
        return json.loads(text, object_pairs_hook=no_duplicates)
    except json.JSONDecodeError as exc:
        raise RecipeError(f"{path}:{exc.lineno}:{exc.colno}: invalid JSON - {exc.msg}") from exc


def _load_yaml(text: str, path: pathlib.Path) -> Any:
    try:
        from ruamel.yaml import YAML
        from ruamel.yaml.error import YAMLError
    except ImportError as exc:  # pragma: no cover - dependency is in requirements.txt
        raise RecipeError(
            f"{path}: YAML support needs the 'ruamel.yaml' package",
            hint="pip install -r automation/resources/requirements.txt",
        ) from exc

    yaml = YAML(typ="safe")
    yaml.allow_duplicate_keys = False
    try:
        return yaml.load(text)
    except YAMLError as exc:
        raise RecipeError(f"{path}: invalid YAML - {exc}") from exc


def dump(data: dict[str, Any], fmt: str = "yaml") -> str:
    """Render a recipe back out, for `recipe render` and golden-file tests."""
    if fmt == "json":
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    from io import StringIO

    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=4, offset=2)
    buffer = StringIO()
    yaml.dump(data, buffer)
    return buffer.getvalue()
