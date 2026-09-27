"""Test helpers: the fake `fab` harness."""

from __future__ import annotations

import json
import pathlib
import shutil
import sys
import tempfile
from typing import Any, Sequence

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

FAKE_FAB = pathlib.Path(__file__).resolve().parent / "fakefab" / "fab"


class FakeFab:
    """Scripts the fake `fab` binary and records what was called.

    Rules are matched on substrings of the joined argv, so a test states only what it
    cares about:

        fab.add(["exists", "Store"], stdout="* true")
        fab.add_sequence(["mkdir"], [{"returncode": 1, "stderr": "429 TooManyRequests"},
                                     {"returncode": 0}])
    """

    def __init__(self) -> None:
        self._dir = pathlib.Path(tempfile.mkdtemp(prefix="fabricops-fakefab-"))
        self.script_path = self._dir / "script.json"
        self.log_path = self._dir / "calls.jsonl"
        self._rules: list[dict[str, Any]] = []
        self._flush()

    # ------------------------------------------------------------- scripting
    def add(
        self,
        match: Sequence[str],
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        command: str | None = None,
    ) -> "FakeFab":
        """Add a rule. `match` items are substrings of the joined argv.

        Pass `command` to also require argv[0] - needed whenever a needle could appear
        elsewhere in the argv (`get` also occurs inside `api ... -X get`).
        """
        rule: dict[str, Any] = {
            "match": list(match),
            "stdout": stdout,
            "stderr": stderr,
            "returncode": returncode,
        }
        if command:
            rule["command"] = command
        self._rules.append(rule)
        self._flush()
        return self

    def add_json(self, match: Sequence[str], payload: Any, *, command: str | None = None) -> "FakeFab":
        return self.add(match, stdout=json.dumps(payload), command=command)

    def add_sequence(self, match: Sequence[str], responses: Sequence[dict[str, Any]]) -> "FakeFab":
        self._rules.append({"match": list(match), "responses": [dict(r) for r in responses]})
        self._flush()
        return self

    def _flush(self) -> None:
        self.script_path.write_text(json.dumps(self._rules), encoding="utf-8")
        state = pathlib.Path(str(self.script_path) + ".state")
        if state.exists():
            state.unlink()

    # ----------------------------------------------------------------- usage
    @property
    def env(self) -> dict[str, str]:
        """Environment for a `FabricCli(env=...)` that should hit the fake binary."""
        import os

        env = dict(os.environ)
        env.update({"FAKE_FAB_SCRIPT": str(self.script_path), "FAKE_FAB_LOG": str(self.log_path)})
        return env

    @property
    def executable(self) -> str:
        return str(FAKE_FAB)

    @property
    def calls(self) -> list[list[str]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    @property
    def commands(self) -> list[str]:
        return [" ".join(call) for call in self.calls]

    def cleanup(self) -> None:
        shutil.rmtree(self._dir, ignore_errors=True)
