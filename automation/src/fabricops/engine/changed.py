"""Which layers a set of changed repository paths touches.

A sync on every merge used to check all seven layers, and the checking was the cost: a
`git/status` call is a server-side diff and takes seconds, times seven, for a merge that
touched one folder. The merge commit already says which paths changed, and the recipe
already says which folder each layer syncs, so the two together name the layers worth
asking about.

The mapping is pure so it can be tested without git. The git call is separate and returns
None when it cannot answer - a bad ref, a shallow clone, no git at all - so the caller can
fall back to syncing everything rather than quietly syncing nothing.
"""

from __future__ import annotations

import pathlib
import subprocess
from typing import Iterable


def layers_touched(recipe, changed_paths: Iterable[str]) -> set[str]:
    """Layers whose git directory contains at least one of `changed_paths`.

    A path inside `solution/engineering/prepare/...` touches the layer bound to
    `solution/engineering/prepare`. It does *not* touch a layer bound to
    `solution/engineering` unless one is - a combined layer at a smaller tier binds the
    parent folder, and then it is touched. Paths outside every layer directory touch
    nothing: a change to automation/ or a pipeline needs no workspace sync.
    """
    directories = {}
    for layer in recipe.layers:
        git = recipe.value(layer, "git") or {}
        directory = str(git.get("directory") or "").strip("/")
        if directory:
            directories[layer] = directory
    touched = set()
    for raw in changed_paths:
        path = str(raw).replace("\\", "/").strip("/")
        for layer, directory in directories.items():
            if path == directory or path.startswith(directory + "/"):
                touched.add(layer)
    return touched


def changed_paths_since(ref: str, root: str | pathlib.Path = ".") -> list[str] | None:
    """Paths changed between `ref` and HEAD, or None if git cannot say."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{ref}", "HEAD"],
            cwd=str(root), capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]
