"""Reading local credentials from a file, for local runs only.

Pipelines should never use this - they have a secret store, and `FAB_SPN_*` keeps the
secret out of FabricOps entirely. But a local run needs an identity from somewhere, and
the alternative to a gitignored file is a shell history full of `--client-secret`.

The convention is the one the `locale/` scripts already established:

    automation/credentials/credentials.<environment>.json    tried first
    automation/credentials/credentials.json                  fallback

Two guards, because this exact file was committed to this repository for months before
anyone noticed:

* if git tracks the file, say so loudly on every run - a warning that repeats is the point;
* if the file is readable by anyone but the owner, say that too.
"""

from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess
from dataclasses import dataclass
from typing import Any

DEFAULT_DIRECTORY = pathlib.Path("automation/credentials")

#: Recognised keys, mapped to what FabricOps calls them.
FIELDS = {
    "tenant_id": "tenant_id",
    "client_id": "client_id",
    "client_secret": "client_secret",
    "github_pat": "github_pat",
    "ado_pat": "ado_pat",
}


@dataclass
class LocalCredentials:
    values: dict[str, str]
    path: pathlib.Path
    warnings: list[str]


def find(
    directory: str | pathlib.Path = DEFAULT_DIRECTORY,
    *,
    environment: str | None = None,
) -> pathlib.Path | None:
    """The credentials file for this run, environment-specific first."""
    base = pathlib.Path(directory)
    candidates = []
    if environment:
        candidates.append(base / f"credentials.{environment}.json")
    candidates.append(base / "credentials.json")
    return next((path for path in candidates if path.is_file()), None)


def load(
    directory: str | pathlib.Path = DEFAULT_DIRECTORY,
    *,
    environment: str | None = None,
) -> LocalCredentials | None:
    """Read the local credentials file, with its hygiene warnings."""
    path = find(directory, environment=environment)
    if not path:
        return None

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return LocalCredentials({}, path, [f"{path} could not be read: {error}"])

    values = {
        name: str(document[key])
        for key, name in FIELDS.items()
        if document.get(key) and not _is_placeholder(str(document[key]))
    }
    return LocalCredentials(values, path, _hygiene(path))


def _is_placeholder(value: str) -> bool:
    """The shipped template is not a credential."""
    lowered = value.strip().lower()
    return (
        not lowered
        or lowered.startswith("your")
        or set(lowered) <= set("x")
        or lowered == "00000000-0000-0000-0000-000000000000"
    )


def _hygiene(path: pathlib.Path) -> list[str]:
    warnings: list[str] = []
    if _tracked_by_git(path):
        warnings.append(
            f"{path} is tracked by git. Anything in it should be treated as public: "
            f"rotate it, then `git rm --cached {path}`."
        )
    try:
        mode = path.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            warnings.append(f"{path} is readable by other users; `chmod 600 {path}`.")
    except OSError:  # pragma: no cover - unreadable stat is not worth failing over
        pass
    return warnings


def _tracked_by_git(path: pathlib.Path) -> bool:
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "ls-files", "--error-unmatch", str(path)],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=path.parent if path.parent.exists() else None,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def merge(explicit: dict[str, Any], local: LocalCredentials | None) -> dict[str, Any]:
    """Explicit values win; the file only fills the gaps.

    A flag or an environment variable is a deliberate act for this run. The file is a
    convenience, so it must never override one.
    """
    merged = dict(explicit)
    for name, value in (local.values if local else {}).items():
        if not merged.get(name):
            merged[name] = value
    return merged


def from_environment() -> dict[str, Any]:
    """The environment variables the entry-point scripts have always read."""
    return {
        name: os.environ.get(variable)
        for name, variable in (
            ("tenant_id", "TENANT_ID"),
            ("client_id", "CLIENT_ID"),
            ("client_secret", "CLIENT_SECRET"),
            ("github_pat", "GITHUB_PAT"),
        )
        if os.environ.get(variable)
    }
