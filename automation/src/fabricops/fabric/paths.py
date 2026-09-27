"""Fabric CLI path construction.

The Fabric CLI addresses everything as a filesystem-like path
(`ws.Workspace/item.Type`). Building those paths by hand is where today's code grows
quoting hacks such as `workspace_name.replace("/", "\\\\/")`. Every naming rule lives
here instead, and paths are passed to `subprocess` as single argv elements, so no shell
quoting is involved at all.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..obs.redaction import MaskedValue, Secret

# The CLI treats "/" as a path separator, so a "/" inside a display name is escaped.
_ESCAPES = (("/", "\\/"),)


def escape_name(name: str) -> str:
    """Escape a Fabric display name for use inside a CLI path."""
    out = name
    for raw, escaped in _ESCAPES:
        out = out.replace(raw, escaped)
    return out


@dataclass(frozen=True)
class FabPath:
    """A Fabric CLI path, e.g. `Sales - Store [dev].Workspace/Curated.Lakehouse`."""

    value: str

    def __str__(self) -> str:
        return self.value

    def __truediv__(self, other: str) -> "FabPath":
        return FabPath(f"{self.value}/{other}")

    # ------------------------------------------------------------- constructors
    @classmethod
    def workspace(cls, display_name: str) -> "FabPath":
        return cls(f"{escape_name(display_name)}.Workspace")

    @classmethod
    def item(cls, workspace: str, name: str, item_type: str) -> "FabPath":
        return cls.workspace(workspace) / f"{escape_name(name)}.{item_type}"

    @classmethod
    def folder(cls, workspace: str, folder_path: str) -> "FabPath":
        parts = [escape_name(part) for part in folder_path.strip("/").split("/") if part]
        return cls.workspace(workspace) / "/".join(parts)

    @classmethod
    def connection(cls, name: str) -> "FabPath":
        return cls(f".connections/{escape_name(name)}.Connection")

    @classmethod
    def capacity(cls, name: str) -> "FabPath":
        return cls(f".capacities/{escape_name(name)}.Capacity")

    @classmethod
    def managed_identity(cls, workspace: str) -> "FabPath":
        escaped = escape_name(workspace)
        return cls.workspace(workspace) / f".managedidentities/{escaped}.ManagedIdentity"

    @classmethod
    def managed_private_endpoint(cls, workspace: str, name: str) -> "FabPath":
        return cls.workspace(workspace) / f".managedprivateendpoints/{escape_name(name)}.ManagedPrivateEndpoint"

    @classmethod
    def spark_pool(cls, workspace: str, name: str) -> "FabPath":
        return cls.workspace(workspace) / f".sparkpools/{escape_name(name)}.SparkPool"


def params(pairs: dict[str, object]) -> str | MaskedValue:
    """Render a `-P key=value,key=value` argument value.

    Booleans are lowercased (the CLI expects `true`/`false`) and nested mappings are
    flattened with dots, so `{"pool": {"starterPool": {"maxNodeCount": 1}}}` becomes
    `pool.starterPool.maxNodeCount=1`.

    When any value is a `Secret`, a `MaskedValue` is returned: the process receives the
    real parameter string while every log sink receives the masked one.
    """
    real: list[str] = []
    masked: list[str] = []
    has_secret = False

    def walk(prefix: str, value: object) -> None:
        nonlocal has_secret
        if isinstance(value, dict):
            for key, inner in value.items():
                walk(f"{prefix}.{key}" if prefix else str(key), inner)
            return
        if isinstance(value, Secret):
            has_secret = True
            real.append(f"{prefix}={value.reveal()}")
            masked.append(f"{prefix}={value}")
            return
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = str(value)
        real.append(f"{prefix}={rendered}")
        masked.append(f"{prefix}={rendered}")

    walk("", pairs)
    joined_real = ",".join(real)
    return MaskedValue(joined_real, ",".join(masked)) if has_secret else joined_real
