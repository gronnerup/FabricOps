"""The run manifest: what was executed, what it produced.

Outputs (ids, SQL endpoints, connection ids) are recorded here rather than being written
back into the recipe object as today's scripts do. The manifest is what parameter-file
generation, variable-library generation and teardown consume.
"""

from __future__ import annotations

import json
import pathlib
import time
from dataclasses import dataclass, field
from typing import Any

DEFAULT_ROOT = pathlib.Path(".fabricops/runs")


@dataclass
class Manifest:
    run_id: str
    command: str = ""
    solution: str | None = None
    environment: str | None = None
    kind: str = "Platform"
    dry_run: bool = False
    started: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    sources: list[str] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add(self, action_id: str, kind: str, layer: str | None, description: str, status: str,
            duration: float, message: str = "", outputs: dict[str, Any] | None = None) -> None:
        self.actions.append(
            {
                "id": action_id,
                "kind": kind,
                "layer": layer,
                "description": description,
                "status": status,
                "duration_s": round(duration, 3),
                "message": message,
            }
        )
        if outputs:
            self.outputs.setdefault(action_id, {}).update(outputs)

    # ------------------------------------------------------------------ summary
    @property
    def counts(self) -> dict[str, int]:
        summary: dict[str, int] = {}
        for action in self.actions:
            summary[action["status"]] = summary.get(action["status"], 0) + 1
        return summary

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [action for action in self.actions if action["status"] == "failed"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "command": self.command,
            "solution": self.solution,
            "environment": self.environment,
            "kind": self.kind,
            "dry_run": self.dry_run,
            "started": self.started,
            "finished": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "sources": self.sources,
            "counts": self.counts,
            "actions": self.actions,
            "outputs": self.outputs,
        }

    def write(self, root: str | pathlib.Path = DEFAULT_ROOT) -> pathlib.Path:
        directory = pathlib.Path(root) / self.run_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "manifest.json"
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
        return path

    @classmethod
    def read(cls, path: str | pathlib.Path) -> dict[str, Any]:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))

    @classmethod
    def latest_for(cls, environment: str, root: str | pathlib.Path = DEFAULT_ROOT) -> dict[str, Any] | None:
        """The most recent successful manifest for `environment`, for value resolution.

        Dry runs are skipped: their outputs are placeholders, and writing a placeholder id
        into a generated variable library would be worse than leaving it out.
        """
        root = pathlib.Path(root)
        if not root.is_dir():
            return None
        for path in sorted(root.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = cls.read(path)
            except (OSError, ValueError):
                continue
            if data.get("environment") == environment and not data.get("dry_run"):
                return data
        return None

    @classmethod
    def latest(cls, root: str | pathlib.Path = DEFAULT_ROOT) -> pathlib.Path | None:
        root = pathlib.Path(root)
        if not root.is_dir():
            return None
        manifests = sorted(root.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return manifests[0] if manifests else None
