"""Execution context shared by every action."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..fabric.cli import FabricCli
from ..obs.logging import RunLog
from ..recipe import Recipe
from .connections import Credentials
from .tags import TagRegistry


@dataclass
class RunContext:
    """Everything an action may touch: the CLI, the log, the recipe and prior outputs."""

    cli: FabricCli
    log: RunLog
    recipe: Recipe
    dry_run: bool = False
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    credentials: Credentials = field(default_factory=Credentials)
    tag_registry: TagRegistry | None = None
    sleep: Callable[[float], None] = time.sleep

    def record(self, action_id: str, outputs: dict[str, Any]) -> None:
        if outputs:
            self.outputs.setdefault(action_id, {}).update(outputs)

    def output(self, action_id: str, key: str, default: Any = None) -> Any:
        return self.outputs.get(action_id, {}).get(key, default)

    def workspace_name(self, layer: str) -> str:
        return self.recipe.workspace_name(layer)

    def workspace_id(self, layer: str) -> Any:
        return self.output(f"workspace:{layer}", "id")

    def workspace_was_created(self, layer: str) -> bool:
        """Whether this run created the workspace, rather than finding it already there."""
        return bool(self.output(f"workspace:{layer}", "created"))

    def item_output(self, layer: str, name: str, item_type: str, key: str, default: Any = None) -> Any:
        return self.output(f"item:{layer}:{item_type}:{name}", key, default)
