"""Feature storage: the schemas a feature writes into, and cleaning them up (E07).

The important asymmetry: a feature's *code* lives in the feature workspace and disappears
with it, but the *data* it wrote lives in a schema inside the shared dev lakehouse, which
survives. Without cleanup, every merged or abandoned feature leaves a schema behind.

Dropping a schema is the only destructive operation in the storage design, so it is off by
default, matched against the configured pattern, and never runs outside feature teardown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable

from ..errors import RecipeError
from ..fabric.paths import FabPath
from .actions import Action, ActionResult

if TYPE_CHECKING:  # pragma: no cover
    from ..recipe import Recipe
    from .context import RunContext

TABLES = "Tables"


@dataclass(frozen=True)
class LakehouseRef:
    """A `<layer>/<item>` reference to a lakehouse that can hold feature schemas."""

    layer: str
    item: str

    @classmethod
    def parse(cls, reference: str) -> "LakehouseRef":
        parts = [part.strip() for part in str(reference).split("/") if part.strip()]
        if len(parts) != 2:
            raise RecipeError(
                f"storage.feature_schema.lakehouses: '{reference}' must be '<layer>/<item>', e.g. 'Store/Curated'"
            )
        return cls(layer=parts[0], item=parts[1])

    def __str__(self) -> str:
        return f"{self.layer}/{self.item}"


def configured_lakehouses(recipe: "Recipe") -> list[LakehouseRef]:
    return [LakehouseRef.parse(entry) for entry in recipe.storage["feature_schema"]["lakehouses"]]


def schema_path(workspace: str, lakehouse: str, schema: str) -> FabPath:
    """A lakehouse schema is a directory under `Tables`."""
    return FabPath.item(workspace, lakehouse, "Lakehouse") / f"{TABLES}/{schema}"


@dataclass
class DropFeatureSchema(Action):
    """Remove a feature's schema from a *shared* lakehouse when the feature is reaped."""

    workspace: str = ""
    lakehouse: str = ""
    schema: str = ""

    def describe(self) -> str:
        return f"Feature schema '{self.schema}'"

    def detail(self) -> str:  # pragma: no cover - display only
        return f"{self.lakehouse} · shared storage"

    def apply(self, ctx: "RunContext") -> ActionResult:
        """Creating the schema is the notebook's job, not ours - nothing to do on apply."""
        return ActionResult("skipped", message="created on first write by the solution")

    def destroy(self, ctx: "RunContext") -> ActionResult | None:
        path = schema_path(self.workspace, self.lakehouse, self.schema)
        if ctx.dry_run:
            return ActionResult("deleted", {"schema": self.schema}, f"would drop {path}")
        if not ctx.cli.exists(path):
            return ActionResult("skipped", message="schema does not exist")
        ctx.cli.rm(path)
        return ActionResult("created", {"schema": self.schema}, f"dropped {self.schema}")


def list_feature_schemas(ctx: "RunContext", workspace: str, lakehouse: str, prefix: str) -> list[str]:
    """Schema directories under `Tables` whose name starts with the managed prefix."""
    path = FabPath.item(workspace, lakehouse, "Lakehouse") / TABLES
    result = ctx.cli.invoke(["ls", str(path)], check=False)
    if not result.ok:
        return []
    names: list[str] = []
    for line in result.stdout.splitlines():
        name = line.strip().strip("*").strip().rstrip("/")
        if name and name.lower().startswith(prefix.lower()):
            names.append(name)
    return names


def managed_prefix(pattern: str) -> str:
    """The literal head of the pattern - what a managed schema name starts with."""
    head = pattern.split("{", 1)[0]
    return head or pattern
