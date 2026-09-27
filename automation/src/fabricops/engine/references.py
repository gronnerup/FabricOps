"""Rewriting committed references to match a deployed environment.

Some Fabric references can only be item ids. A report's semantic model must be a GUID -
naming the workspace and model in a connection string is rejected outright:

    The semantic model identifier is invalid. Ensure that a valid GUID is provided either
    in 'byConnection.pbiModelDatabaseName' or as the 'semanticModelId' parameter

The same is true of `AzureStorage.DataLake(<workspaceId>/<itemId>)` in a Direct Lake
expression, and of a pipeline's `notebookId` / `workspaceId`.

Item ids are per-tenant. So a repository that ships working example content - which this
one does, so that FabricOps can be tried out - cannot possibly ship ids that resolve in
anyone else's tenant. Somebody has to rewrite them after the first deployment, and doing it
by hand is both tedious and the sort of thing that is wrong for weeks before anyone notices.

That is what this module is for. The recipe declares *where* each reference lives and *what
it points at*; the ids come from the tenant.
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..errors import RecipeError
from ..recipe import Recipe

GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


@dataclass(frozen=True)
class Target:
    """What a reference points at: a workspace, or an item inside one."""

    layer: str
    item: str | None = None
    item_type: str | None = None

    def describe(self) -> str:
        return f"{self.item_type} '{self.item}' in {self.layer}" if self.item else f"the {self.layer} workspace"


@dataclass(frozen=True)
class Reference:
    """One id in one committed file, and what it should be."""

    file: pathlib.Path
    target: Target
    json_path: str | None = None      # exact address in a JSON document
    pattern: str | None = None        # regex with one capture group, for text formats
    label: str = ""
    #: Whether Fabric resolves this reference when git syncs the item. True for a report's
    #: semantic model and a pipeline's notebooks; false for a semantic model's M expression,
    #: which is opaque text and syncs regardless.
    blocks_sync: bool = False

    def describe(self) -> str:
        return self.label or f"{self.file.name} -> {self.target.describe()}"


@dataclass
class Rewrite:
    reference: Reference
    old: str | None
    new: str
    changed: bool

    @property
    def state(self) -> str:
        if self.old is None:
            return "not found"
        return "rewritten" if self.changed else "already correct"


@dataclass
class ReferenceReport:
    rewrites: list[Rewrite] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    applied: bool = False

    @property
    def changed(self) -> list[Rewrite]:
        return [rewrite for rewrite in self.rewrites if rewrite.changed]

    def describe(self) -> list[str]:
        if not self.rewrites:
            return ["  no references declared"]
        width = max(len(rewrite.reference.describe()) for rewrite in self.rewrites)
        verb = "" if self.applied else "would be "
        return [
            f"  {rewrite.reference.describe().ljust(width)}  "
            + (f"{verb}rewritten: {rewrite.old} -> {rewrite.new}" if rewrite.changed else rewrite.state)
            for rewrite in self.rewrites
        ]


def declared(recipe: Recipe, root: pathlib.Path) -> list[Reference]:
    """The `references:` block, as a list of resolvable references."""
    found: list[Reference] = []
    for index, entry in enumerate(recipe.data.get("references") or []):
        where = f"references[{index}]"
        file = entry.get("file")
        if not file:
            raise RecipeError(f"{where}: `file` is required")
        path = (root / str(file)).resolve()

        pointers = entry.get("replace")
        if not isinstance(pointers, list) or not pointers:
            raise RecipeError(
                f"{where}: `replace` must be a non-empty list",
                hint="Each entry needs `at` (a JSON path) or `pattern` (a regex), plus a target.",
            )
        for position, pointer in enumerate(pointers):
            layer = pointer.get("layer")
            if not layer:
                raise RecipeError(f"{where}.replace[{position}]: `layer` is required")
            if layer not in recipe.layers:
                raise RecipeError(
                    f"{where}.replace[{position}]: '{layer}' is not a layer",
                    hint=f"Known layers: {', '.join(recipe.layers)}",
                )
            if not pointer.get("at") and not pointer.get("pattern"):
                raise RecipeError(
                    f"{where}.replace[{position}]: needs `at` (JSON path) or `pattern` (regex)",
                    hint="JSON documents take a dotted path; text formats take a regex with one group.",
                )
            found.append(
                Reference(
                    file=path,
                    target=Target(
                        layer=str(layer),
                        item=pointer.get("item"),
                        item_type=pointer.get("type"),
                    ),
                    json_path=pointer.get("at"),
                    pattern=pointer.get("pattern"),
                    label=pointer.get("label") or f"{pathlib.Path(str(file)).name}:{pointer.get('at') or 'regex'}",
                    blocks_sync=bool(pointer.get("blocks_sync", False)),
                )
            )
    return found


def read(reference: Reference) -> str | None:
    """The id currently sitting at the reference's location."""
    if not reference.file.exists():
        return None
    text = reference.file.read_text(encoding="utf-8")
    if reference.json_path:
        node = _walk(json.loads(text), reference.json_path.split("."))
        return str(node) if isinstance(node, str) else None
    match = re.search(str(reference.pattern), text)
    return match.group(1) if match else None


def write(reference: Reference, old: str, new: str) -> None:
    """Replace exactly the one occurrence the reference addresses."""
    text = reference.file.read_text(encoding="utf-8")
    if reference.json_path:
        document = json.loads(text)
        keys = reference.json_path.split(".")
        parent = _walk(document, keys[:-1])
        _assign(parent, keys[-1], new)
        reference.file.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        return
    # Text formats: rewrite only inside the matched region, so an id that appears elsewhere
    # in the file for another reason is left alone.
    match = re.search(str(reference.pattern), text)
    if not match:
        return
    start, end = match.span(1)
    reference.file.write_text(text[:start] + new + text[end:], encoding="utf-8")


def _walk(node: Any, keys: list[str]) -> Any:
    """Follow a dotted path, where a numeric segment indexes a list.

    A pipeline's activities are a list, so `properties.activities.0.typeProperties` has to
    work as naturally as a mapping path does.
    """
    for key in keys:
        if isinstance(node, list):
            if not key.lstrip("-").isdigit():
                return None
            index = int(key)
            if not -len(node) <= index < len(node):
                return None
            node = node[index]
        elif isinstance(node, dict) and key in node:
            node = node[key]
        else:
            return None
    return node


def _assign(parent: Any, key: str, value: str) -> None:
    if isinstance(parent, list):
        parent[int(key)] = value
    elif isinstance(parent, dict):
        parent[key] = value


def sync(
    references: list[Reference],
    *,
    resolve: Callable[[Target], str | None],
    apply: bool = False,
) -> ReferenceReport:
    """Bring every declared reference into line with the deployed environment."""
    report = ReferenceReport(applied=apply)
    for reference in references:
        current = read(reference)
        if current is None:
            report.warnings.append(f"{reference.describe()}: nothing found at that location")
            report.rewrites.append(Rewrite(reference, None, "", False))
            continue

        resolved = resolve(reference.target)
        if not resolved:
            report.warnings.append(f"{reference.describe()}: {reference.target.describe()} is not deployed")
            report.rewrites.append(Rewrite(reference, current, current, False))
            continue

        changed = current.casefold() != resolved.casefold()
        if changed and apply:
            write(reference, current, resolved)
        report.rewrites.append(Rewrite(reference, current, resolved, changed))
    return report


def tenant_resolver(cli: Any, recipe: Recipe) -> Callable[[Target], str | None]:
    """Resolve a target to its deployed id, caching within a run."""
    from ..fabric.paths import FabPath

    cache: dict[Target, str | None] = {}

    def resolve(target: Target) -> str | None:
        if target in cache:
            return cache[target]
        workspace = recipe.workspace_name(target.layer)
        path = (
            FabPath.item(workspace, str(target.item), str(target.item_type))
            if target.item
            else FabPath.workspace(workspace)
        )
        cache[target] = cli.get_value(path, "id").strip() if cli.exists(path) else None
        return cache[target]

    return resolve
