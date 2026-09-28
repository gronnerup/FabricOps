"""The `permissions` node: role assignments, plus one policy key.

    permissions:
      mode: additive | strict     # optional, default additive
      Admin: [ {type: Group, id: ...} ]
      Member: [ ... ]

`additive` (the default) sets every assignment the recipe declares and leaves everything
else alone, so a role somebody added in the portal survives the next run. `strict` makes
the recipe the whole truth: undeclared assignments are removed on the next run. A layer's
`mode` overrides the defaults', so prod can be strict while dev stays additive.
"""

from __future__ import annotations

from typing import Any

ADDITIVE = "additive"
STRICT = "strict"
MODES = (ADDITIVE, STRICT)
MODE_KEY = "mode"


def split_permissions(node: Any) -> tuple[str | None, dict[str, Any]]:
    """(mode or None, the role -> principals mapping without the policy key)."""
    if not isinstance(node, dict):
        return None, {}
    mode = node.get(MODE_KEY)
    roles = {key: value for key, value in node.items() if key != MODE_KEY}
    return (str(mode).lower() if mode is not None else None), roles


def resolve_mode(*nodes: Any) -> str:
    """The effective mode: the last node that sets one wins, else additive."""
    mode = ADDITIVE
    for node in nodes:
        declared, _ = split_permissions(node)
        if declared:
            mode = declared
    return mode
