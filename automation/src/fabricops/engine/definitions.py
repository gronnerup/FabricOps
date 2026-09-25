"""Item definitions: getting real content into a platform-owned item (E03-S4).

Two declarative routes, both ending in `fab import`:

    definition:
      from: solution/store/Ingest_Config.VariableLibrary   # a directory in the repo

    definition:
      parts:                                               # rendered with recipe tokens
        - path: notebook-content.py
          from: templates/bootstrap.py

This is for items FabricOps itself owns - bootstrap notebooks, generated variable
libraries, config artefacts. Deploying the *solution* tree is fabric-cicd's job (E09).

Idempotency is by content hash against what the tenant currently holds: the item is
exported to a temporary directory and compared. That is one extra read per item with a
definition, which is affordable because platform-owned items are few, and it is
authoritative in a way that a hash recorded in a previous run's manifest is not - someone
may have edited the item in the portal since.
"""

from __future__ import annotations

import hashlib
import pathlib
import tempfile
from dataclasses import dataclass
from typing import Any, Iterable

from ..errors import RecipeError
from ..recipe.tokens import substitute_content

#: Files that say nothing about the definition's content and differ between an export and
#: the repository copy, so hashing them would make every item look changed forever.
IGNORED_NAMES = frozenset({".platform", ".DS_Store", "Thumbs.db"})


@dataclass(frozen=True)
class DefinitionSource:
    """Where an item's definition comes from, and in what format."""

    directory: pathlib.Path | None = None
    parts: tuple[tuple[str, str], ...] = ()      # (path inside the item, rendered content)
    definition_format: str | None = None

    @property
    def is_inline(self) -> bool:
        return not self.directory and bool(self.parts)


def resolve(
    definition: dict[str, Any],
    *,
    root: pathlib.Path,
    context: dict[str, Any],
    where: str,
) -> DefinitionSource:
    """Turn a recipe `definition:` block into something importable."""
    source = definition.get("from")
    parts = definition.get("parts") or []
    if source and parts:
        raise RecipeError(
            f"{where}: definition has both `from` and `parts`",
            hint="Use `from` for a directory in the repository, or `parts` for inline content.",
        )
    if not source and not parts:
        raise RecipeError(
            f"{where}: definition has neither `from` nor `parts`",
            hint="Add `from: <directory>` or a `parts:` list.",
        )

    definition_format = definition.get("format")

    if source:
        directory = (root / str(source)).resolve()
        if not directory.exists():
            raise RecipeError(
                f"{where}: definition.from '{source}' does not exist",
                hint=f"Looked in {directory}. The path is relative to the repository root.",
            )
        if not directory.is_dir():
            raise RecipeError(f"{where}: definition.from '{source}' is a file, not a directory")
        return DefinitionSource(directory=directory, definition_format=definition_format)

    return DefinitionSource(
        parts=_render_parts(parts, root=root, context=context, where=where),
        definition_format=definition_format,
    )


def _render_parts(
    parts: Iterable[dict[str, Any]],
    *,
    root: pathlib.Path,
    context: dict[str, Any],
    where: str,
) -> tuple[tuple[str, str], ...]:
    rendered: list[tuple[str, str]] = []
    for index, part in enumerate(parts):
        path = part.get("path")
        if not path:
            raise RecipeError(f"{where}: definition.parts[{index}] has no `path`")
        if "from" in part and "content" in part:
            raise RecipeError(f"{where}: definition.parts[{index}] has both `from` and `content`")

        if "from" in part:
            source = (root / str(part["from"])).resolve()
            if not source.exists():
                raise RecipeError(
                    f"{where}: definition.parts[{index}].from '{part['from']}' does not exist",
                    hint=f"Looked in {source}.",
                )
            raw = source.read_text(encoding="utf-8")
        elif "content" in part:
            raw = str(part["content"])
        else:
            raise RecipeError(f"{where}: definition.parts[{index}] has neither `from` nor `content`")

        rendered.append((str(path), substitute_content(raw, context, path=f"{where}.parts[{index}]")))
    return tuple(rendered)


def materialise(source: DefinitionSource, into: pathlib.Path) -> pathlib.Path:
    """The directory to hand to `fab import`, writing inline parts out if needed."""
    if source.directory:
        return source.directory
    for path, content in source.parts:
        target = into / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return into


def content_hash(directory: pathlib.Path) -> str:
    """A stable hash of a definition directory: relative paths and bytes, sorted."""
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file() and p.name not in IGNORED_NAMES):
        digest.update(str(path.relative_to(directory).as_posix()).encode("utf-8"))
        digest.update(b"\0")
        digest.update(_normalised(path))
        digest.update(b"\0")
    return digest.hexdigest()


def _normalised(path: pathlib.Path) -> bytes:
    """Bytes with line endings normalised, so a CRLF checkout is not permanent drift."""
    raw = path.read_bytes()
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw
    return raw.replace(b"\r\n", b"\n")


def source_hash(source: DefinitionSource) -> str:
    """The hash of what we intend to import, whether it is on disk or inline."""
    if source.directory:
        return content_hash(source.directory)
    with tempfile.TemporaryDirectory(prefix="fabricops-definition-") as tmp:
        return content_hash(materialise(source, pathlib.Path(tmp)))


def exported_hash(exported_root: pathlib.Path, item_name: str, item_type: str) -> str | None:
    """Hash what `fab export` produced, or None when the item had no definition.

    Export writes `<output>/<name>.<Type>/…`, so the item folder is unwrapped first -
    otherwise the folder name itself would be part of the hash and never match.
    """
    candidate = exported_root / f"{item_name}.{item_type}"
    directory = candidate if candidate.is_dir() else exported_root
    if not directory.is_dir() or not any(p.is_file() for p in directory.rglob("*")):
        return None
    return content_hash(directory)
