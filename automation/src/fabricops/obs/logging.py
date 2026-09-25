"""Run logging: console output, an optional JSONL trace, one run id, redaction.

Off by default in the sense that matters: the console keeps the same step-oriented
output FabricOps has today, and nothing is written to disk unless a trace file is
requested. Turning on `debug` adds one line per `fab` invocation; `trace` also asks the
Fabric CLI itself for HTTP-level logging (see fabric.cli).
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any

from .redaction import redact

# ANSI colours, matching the output FabricOps already produces.
_RESET = "\033[0m"
_BOLD = "\033[1m"
_RED = "\033[91m"
_YELLOW = "\033[33m"
_GREEN = "\033[32m"
_BLUE_BOLD = "\033[1;34m"
_GREY = "\033[90m"


class Level(IntEnum):
    OFF = 0
    ERROR = 1
    WARN = 2
    INFO = 3
    DEBUG = 4
    TRACE = 5

    @classmethod
    def parse(cls, value: "str | Level | None", default: "Level | None" = None) -> "Level":
        if isinstance(value, cls):
            return value
        if value is None:
            return default or cls.INFO
        try:
            return cls[str(value).strip().upper()]
        except KeyError as exc:
            valid = ", ".join(level.name.lower() for level in cls)
            raise ValueError(f"unknown log level '{value}' (expected one of: {valid})") from exc


def _supports_colour(stream: Any) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


@dataclass
class RunLog:
    """The single logging surface for a FabricOps run."""

    level: Level = Level.INFO
    trace_file: Path | None = None
    redaction_enabled: bool = True
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    stream: Any = None
    _colour: bool | None = field(default=None, repr=False)
    _handle: Any = field(default=None, repr=False)
    _counts: dict[str, int] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.stream is None:
            self.stream = sys.stdout
        if self._colour is None:
            self._colour = _supports_colour(self.stream)
        if self.trace_file is not None:
            self.trace_file = Path(self.trace_file)
            self.trace_file.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.trace_file.open("a", encoding="utf-8")

    # ---------------------------------------------------------------- factories
    @classmethod
    def from_env(cls, **overrides: Any) -> "RunLog":
        """Build a RunLog from FABOPS_* environment variables, then apply overrides."""
        level = Level.parse(os.environ.get("FABOPS_LOG_LEVEL"), Level.INFO)
        trace = os.environ.get("FABOPS_TRACE_FILE")
        allow_unredacted = os.environ.get("FABOPS_ALLOW_UNREDACTED") == "1"
        kwargs: dict[str, Any] = {
            "level": level,
            "trace_file": Path(trace) if trace else None,
            "redaction_enabled": not allow_unredacted,
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    # ------------------------------------------------------------------ console
    def _emit(self, text: str, *, colour: str = "", end: str = "\n") -> None:
        if self.level == Level.OFF:
            return
        body = redact(text, enabled=self.redaction_enabled)
        if colour and self._colour:
            body = f"{colour}{body}{_RESET}"
        self.stream.write(body + end)
        flush = getattr(self.stream, "flush", None)
        if flush:
            flush()

    def error(self, text: str) -> None:
        self._count("error")
        if self.level >= Level.ERROR:
            self._emit(text, colour=_RED)

    def warning(self, text: str) -> None:
        self._count("warning")
        if self.level >= Level.WARN:
            self._emit(text, colour=_YELLOW)

    def success(self, text: str) -> None:
        if self.level >= Level.INFO:
            self._emit(text, colour=_GREEN)

    def info(self, text: str = "", *, bold: bool = False, end: str = "\n") -> None:
        if self.level >= Level.INFO:
            self._emit(text, colour=_BOLD if bold else "", end=end)

    def debug(self, text: str) -> None:
        if self.level >= Level.DEBUG:
            self._emit(text, colour=_GREY)

    def trace(self, text: str) -> None:
        if self.level >= Level.TRACE:
            self._emit(text, colour=_GREY)

    def header(self, text: str) -> None:
        if self.level < Level.INFO:
            return
        bar = "#" * 129
        self._emit("")
        self._emit(bar, colour=_BLUE_BOLD)
        self._emit(f"# {text.center(125)} #", colour=_BLUE_BOLD)
        self._emit(bar, colour=_BLUE_BOLD)

    def step(self, text: str) -> None:
        """A step line that a later `ok`/`skip`/`fail` completes on the same row."""
        self.info(f"{text}...", bold=True, end="")

    def ok(self, text: str = " ✔") -> None:
        self.success(text)

    def skip(self, text: str = " ⚠ Already exists") -> None:
        self.warning(text)

    def fail(self, text: str = " ✖ Failed!") -> None:
        self.error(text)

    # -------------------------------------------------------------------- trace
    def record(self, kind: str, **fields: Any) -> None:
        """Append one structured record to the trace file (if enabled)."""
        self._count(kind)
        if self._handle is None:
            return
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "run_id": self.run_id,
            "kind": kind,
        }
        for key, value in fields.items():
            if isinstance(value, str):
                value = redact(value, enabled=self.redaction_enabled)
            payload[key] = value
        self._handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        self._handle.flush()

    def _count(self, kind: str) -> None:
        self._counts[kind] = self._counts.get(kind, 0) + 1

    @property
    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "RunLog":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
