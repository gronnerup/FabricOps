"""Token substitution.

One documented token set, applied to every string value in a recipe - not just display
names, which is what today's `.format(layer=..., environment=...)` calls are limited to.
An unknown token is an error, never a literal, so a typo cannot silently reach Fabric.
"""

from __future__ import annotations

import os
import re
from typing import Any

from ..errors import RecipeError

_TOKEN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)(?::([^}]*))?\}")

# Legacy token names from the first generation of FabricOps recipes (feature.json used
# `{feature_name}` and `{layer_name}`). Permanent, per decision 4.
TOKEN_ALIASES = {
    "feature_name": "feature",
    "layer_name": "layer",
    "environment_name": "environment",
    "solution_name": "solution",
    "identity_username": "developer",
    "identity_id": "developer_id",
}

KNOWN_TOKENS = (
    "environment",
    "developer_id",
    "layer",
    "solution",
    "feature",
    "developer",
    "capacity",
    "branch",
)


def substitute(value: Any, context: dict[str, Any], *, path: str = "", deferred: tuple[str, ...] = ()) -> Any:
    """Recursively substitute `{token}` occurrences in every string in `value`.

    Tokens listed in `deferred` are left untouched - `display_name_pattern` keeps its
    `{layer}` placeholder at the root, and the engine resolves it per layer.
    """
    if isinstance(value, str):
        return _substitute_string(value, context, path, deferred)
    if isinstance(value, dict):
        return {
            key: substitute(item, context, path=f"{path}.{key}" if path else str(key), deferred=deferred)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [substitute(item, context, path=f"{path}[{index}]", deferred=deferred) for index, item in enumerate(value)]
    return value


def _substitute_string(text: str, context: dict[str, Any], path: str, deferred: tuple[str, ...] = ()) -> str:
    def replace(match: re.Match[str]) -> str:
        name, argument = match.group(1), match.group(2)
        name = TOKEN_ALIASES.get(name, name)
        if name in deferred and context.get(name) is None:
            return match.group(0)
        if name == "env":
            if not argument:
                raise RecipeError(f"{path or 'recipe'}: '{{env:}}' needs a variable name, e.g. {{env:FABRIC_CAPACITY}}")
            if argument not in os.environ:
                raise RecipeError(
                    f"{path or 'recipe'}: environment variable '{argument}' is not set",
                    hint=f"Set {argument}, or replace the token with a literal value.",
                )
            return os.environ[argument]
        if name in context and context[name] is not None:
            return str(context[name])
        if name in KNOWN_TOKENS:
            raise RecipeError(
                f"{path or 'recipe'}: token '{{{name}}}' is not available here",
                hint=f"Available in this context: {', '.join(sorted(k for k, v in context.items() if v is not None)) or 'none'}",
            )
        raise RecipeError(
            f"{path or 'recipe'}: unknown token '{{{name}}}'",
            hint=f"Known tokens: {', '.join(KNOWN_TOKENS)}, plus {{env:VAR}}.",
        )

    return _TOKEN.sub(replace, text)


def find_tokens(value: Any) -> set[str]:
    """Every token name used anywhere in `value` (for validation and docs)."""
    found: set[str] = set()
    if isinstance(value, str):
        found.update(match.group(1) for match in _TOKEN.finditer(value))
    elif isinstance(value, dict):
        for item in value.values():
            found |= find_tokens(item)
    elif isinstance(value, list):
        for item in value:
            found |= find_tokens(item)
    return found


def substitute_content(text: str, context: dict[str, Any], *, path: str = "") -> str:
    """Substitute tokens in a *file body*, leaving anything unrecognised alone.

    Deliberately more forgiving than `substitute`. In a recipe an unknown token is a typo
    and raising is the point. In a notebook or a JSON definition, braces are ordinary
    syntax - `f"{name}"`, `{"a": 1}`, a format string - and treating every one of them as
    a token would make inline definition parts unusable for real code.

    So only tokens that are actually available get replaced. `{env:VAR}` still raises when
    the variable is missing, because that one is unambiguously a request for a value.
    """

    def replace(match: "re.Match[str]") -> str:
        name, argument = match.group(1), match.group(2)
        name = TOKEN_ALIASES.get(name, name)
        if name == "env":
            if not argument:
                raise RecipeError(f"{path or 'definition'}: '{{env:}}' needs a variable name")
            if argument not in os.environ:
                raise RecipeError(
                    f"{path or 'definition'}: environment variable '{argument}' is not set",
                    hint=f"Set {argument}, or replace the token with a literal value.",
                )
            return os.environ[argument]
        value = context.get(name)
        return match.group(0) if value is None else str(value)

    return _TOKEN.sub(replace, text)
