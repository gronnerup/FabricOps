"""Item type handlers - the explicit special cases.

The engine is type-agnostic: creation, properties, definitions and tags work for any
Fabric item type from `name` + `type` alone. Where a type genuinely behaves differently -
a Lakehouse whose SQL endpoint provisions asynchronously, a Report that needs a semantic
model id - that behaviour lives in a registered handler, never in an `if` branch in the
middle of the flow (documentation/specs/E03 §5).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from ..errors import FabricOpsError
from ..fabric.paths import FabPath

if TYPE_CHECKING:  # pragma: no cover
    from .actions import CreateItem
    from .context import RunContext


class ItemHandler:
    """Default behaviour: create, then read properties back as outputs."""

    item_types: tuple[str, ...] = ()
    ready_timeout: float = 120.0
    poll_interval: float = 2.0

    # -------------------------------------------------------------------- hooks
    def pre_create(self, ctx: "RunContext", action: "CreateItem", payload: dict[str, Any]) -> None:
        """Adjust the creation payload before the item is created."""

    def wait_ready(self, ctx: "RunContext", action: "CreateItem") -> dict[str, Any]:
        """Return the item's metadata once it is usable."""
        return self._metadata(ctx, action)

    def outputs(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """Values other actions may depend on (ids, endpoints, connection strings)."""
        return {"id": metadata.get("id")} if metadata.get("id") else {}

    # ------------------------------------------------------------------ helpers
    def _metadata(self, ctx: "RunContext", action: "CreateItem") -> dict[str, Any]:
        if ctx.dry_run:
            return {}
        try:
            payload = ctx.cli.get_json(action.path, ".")
        except FabricOpsError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _poll(self, ctx: "RunContext", action: "CreateItem", is_ready) -> dict[str, Any]:
        """Poll item metadata until `is_ready(metadata)`, or the budget is exhausted.

        Bounded twice on purpose: by wall-clock deadline *and* by attempt count. A
        deadline alone is not enough - an injected clock (tests, or a sleep that a host
        ignores) would otherwise spin the loop, hammering the API.
        """
        if ctx.dry_run:
            return {}
        deadline = time.monotonic() + self.ready_timeout
        max_attempts = max(1, int(self.ready_timeout // max(self.poll_interval, 0.1)) + 1)
        metadata = self._metadata(ctx, action)
        attempts = 1

        while not is_ready(metadata):
            if attempts >= max_attempts or time.monotonic() >= deadline:
                ctx.log.warning(
                    f" ⚠ timed out waiting for {action.item_type} '{action.item_name}'"
                    f" after {attempts} attempt(s)"
                )
                break
            ctx.sleep(self.poll_interval)
            metadata = self._metadata(ctx, action)
            attempts += 1
        return metadata


class LakehouseHandler(ItemHandler):
    item_types = ("Lakehouse",)

    def wait_ready(self, ctx: "RunContext", action: "CreateItem") -> dict[str, Any]:
        def ready(metadata: dict[str, Any]) -> bool:
            properties = (metadata or {}).get("properties") or {}
            status = ((properties.get("sqlEndpointProperties") or {}).get("provisioningStatus"))
            return bool(status) and status != "InProgress"

        return self._poll(ctx, action, ready)

    def outputs(self, metadata: dict[str, Any]) -> dict[str, Any]:
        properties = (metadata or {}).get("properties") or {}
        endpoint = properties.get("sqlEndpointProperties") or {}
        return _compact(
            {
                "id": metadata.get("id"),
                "sqlendpoint": endpoint.get("connectionString"),
                "sqlendpointid": endpoint.get("id"),
                "provisioning_status": endpoint.get("provisioningStatus"),
                "schemas_enabled": properties.get("defaultSchema") is not None or None,
                "onelake_files": properties.get("oneLakeFilesPath"),
                "onelake_tables": properties.get("oneLakeTablesPath"),
            }
        )


class WarehouseHandler(ItemHandler):
    item_types = ("Warehouse",)

    def outputs(self, metadata: dict[str, Any]) -> dict[str, Any]:
        properties = (metadata or {}).get("properties") or {}
        return _compact(
            {
                "id": metadata.get("id"),
                "sqlendpoint": properties.get("connectionString") or properties.get("connectionInfo"),
                "database_name": metadata.get("displayName"),
            }
        )


class SqlDatabaseHandler(ItemHandler):
    item_types = ("SQLDatabase",)

    def outputs(self, metadata: dict[str, Any]) -> dict[str, Any]:
        properties = (metadata or {}).get("properties") or {}
        return _compact(
            {
                "id": metadata.get("id"),
                "sqlendpoint": properties.get("serverFqdn"),
                "database_name": properties.get("databaseName"),
            }
        )


class EventhouseHandler(ItemHandler):
    item_types = ("Eventhouse",)

    def outputs(self, metadata: dict[str, Any]) -> dict[str, Any]:
        properties = (metadata or {}).get("properties") or {}
        return _compact(
            {
                "id": metadata.get("id"),
                "query_service_uri": properties.get("queryServiceUri"),
                "ingestion_service_uri": properties.get("ingestionServiceUri"),
                "database_ids": properties.get("databasesItemIds"),
            }
        )


class ReportHandler(ItemHandler):
    item_types = ("Report",)

    def pre_create(self, ctx: "RunContext", action: "CreateItem", payload: dict[str, Any]) -> None:
        """Resolve `semanticModelId` from a model name, so recipes never carry a GUID."""
        model = payload.pop("semanticModel", None) or payload.pop("semantic_model", None)
        if not model or payload.get("semanticModelId"):
            return
        layer = payload.pop("semanticModelLayer", None) or action.layer
        workspace = ctx.workspace_name(layer) if layer else action.workspace
        payload["semanticModelId"] = ctx.cli.get_value(FabPath.item(workspace, str(model), "SemanticModel"), "id")


class SemanticModelHandler(ItemHandler):
    item_types = ("SemanticModel",)


class VariableLibraryHandler(ItemHandler):
    item_types = ("VariableLibrary",)


_REGISTRY: dict[str, ItemHandler] = {}
_DEFAULT = ItemHandler()


def register(handler: ItemHandler) -> ItemHandler:
    for item_type in handler.item_types:
        _REGISTRY[item_type.lower()] = handler
    return handler


for _handler in (
    LakehouseHandler(),
    WarehouseHandler(),
    SqlDatabaseHandler(),
    EventhouseHandler(),
    ReportHandler(),
    SemanticModelHandler(),
    VariableLibraryHandler(),
):
    register(_handler)


def handler_for(item_type: str) -> ItemHandler:
    """The handler for `item_type`, or the type-agnostic default."""
    return _REGISTRY.get(str(item_type).lower(), _DEFAULT)


def registered_types() -> list[str]:
    return sorted(_REGISTRY)


def _compact(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value not in (None, "", [], {})}
