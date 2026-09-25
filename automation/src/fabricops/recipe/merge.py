"""Overlay merging with explicit, per-key strategies.

Replaces the `merge_type: 0|1|2` flag that leaked into the data model. Defaults:

    mapping                     deep merge, child wins
    scalar                      child wins
    list of objects with `name` merge by name
    list of scalars             union, order-stable

An explicit `$merge: keep|replace|append|merge` on a node overrides the default for that
node. `merge_type` is still accepted (permanently) and mapped to the same strategies.
"""

from __future__ import annotations

from typing import Any

from .aliases import MERGE_TYPE_TO_STRATEGY

STRATEGY_KEYS = ("$merge", "merge_type")
VALID_STRATEGIES = ("merge", "replace", "keep", "append")

# Keys used to identify "the same" object across overlays, in priority order.
IDENTITY_KEYS = ("name", "item_name", "id", "find_value")


def _strategy_of(node: Any, inherited: str) -> str:
    if not isinstance(node, dict):
        return inherited
    if "$merge" in node:
        value = str(node["$merge"]).lower()
        return value if value in VALID_STRATEGIES else inherited
    if "merge_type" in node:
        return MERGE_TYPE_TO_STRATEGY.get(node["merge_type"], inherited)
    return inherited


def _strip_strategy(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: v for k, v in node.items() if k not in STRATEGY_KEYS}
    return node


def _identity(item: Any) -> Any:
    if isinstance(item, dict):
        for key in IDENTITY_KEYS:
            if key in item:
                return (key, item[key])
    return None


def merge(base: Any, overlay: Any, *, inherited: str = "merge") -> Any:
    """Merge `overlay` onto `base`, returning a new structure."""
    strategy = _strategy_of(overlay, inherited)

    if strategy == "keep":
        return base if base is not None else _strip_strategy(overlay)
    if strategy == "replace":
        return _strip_strategy(overlay)

    if isinstance(base, dict) and isinstance(overlay, dict):
        out = dict(base)
        for key, value in overlay.items():
            if key in STRATEGY_KEYS:
                continue
            out[key] = merge(base.get(key), value, inherited=strategy) if key in base else _strip_strategy(value)
        return out

    if isinstance(base, list) and isinstance(overlay, list):
        if strategy == "append":
            return list(base) + [item for item in overlay if item not in base]
        return _merge_lists(base, overlay, strategy)

    return _strip_strategy(overlay) if overlay is not None else base


def _merge_lists(base: list[Any], overlay: list[Any], strategy: str) -> list[Any]:
    identified = [item for item in base if _identity(item) is not None]
    if identified:
        # Merge objects by identity, preserving base order then appending new ones.
        by_identity = {_identity(item): dict(item) for item in identified}
        order = [_identity(item) for item in identified]
        plain = [item for item in base if _identity(item) is None]
        for item in overlay:
            key = _identity(item)
            if key is None:
                if item not in plain:
                    plain.append(item)
                continue
            if key in by_identity:
                by_identity[key] = merge(by_identity[key], item, inherited=strategy)
            else:
                by_identity[key] = _strip_strategy(item)
                order.append(key)
        return plain + [by_identity[key] for key in order]

    # Lists of scalars: union, order-stable (tags behave this way).
    out = list(base)
    for item in overlay:
        if item not in out:
            out.append(item)
    return out
