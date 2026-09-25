"""The release run: recipe in, deployed layers out (E09 §5, §6).

This module is the only place in FabricOps that imports `fabric-cicd`. That is deliberate:
Microsoft is investing in config-based deployment, and keeping the library behind one
adapter means a future switch is contained to this file rather than spread through the
codebase (E09 §1).

The library is imported lazily so that `--dry-run`, `fabricops release plan` and the whole
test suite work without it installed.
"""

from __future__ import annotations

import logging
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..errors import AuthError, ExitCode, FabricOpsError, RecipeError
from ..fabric.cli import FabricCli
from ..fabric.paths import FabPath
from ..obs.logging import RunLog
from ..recipe import Recipe
from . import parameters, policy
from .policy import DeployPolicy

DEFAULT_ITEM_TYPES = (
    "Notebook",
    "DataPipeline",
    "Lakehouse",
    "SQLDatabase",
    "SemanticModel",
    "Report",
    "VariableLibrary",
)


class Status:
    """Mirrors `fabric_cicd.DeploymentStatus`, without importing it to say "nothing ran"."""

    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    PLANNED = "planned"


@dataclass
class LayerResult:
    layer: str
    workspace: str
    status: str
    message: str = ""
    published: int = 0
    unpublished: bool = False
    item_ids: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in (Status.COMPLETED, Status.SKIPPED, Status.PLANNED)


@dataclass
class ReleaseResult:
    """The outcome of a release, in the shape `DeploymentResult` would have taken.

    `fabric-cicd` only produces a `DeploymentResult` from `deploy_with_config`, the
    config-file entry point FabricOps does not use, so the equivalent is assembled here
    from what `publish_all_items` raises or returns.
    """

    environment: str
    layers: list[LayerResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    connections: "ConnectionSync | None" = None

    @property
    def status(self) -> str:
        if any(not layer.ok for layer in self.layers):
            return Status.FAILED
        if self.layers and all(layer.status == Status.PLANNED for layer in self.layers):
            return Status.PLANNED
        return Status.COMPLETED

    @property
    def message(self) -> str:
        failed = [layer.layer for layer in self.layers if not layer.ok]
        if failed:
            return f"{len(failed)} of {len(self.layers)} layer(s) failed: {', '.join(failed)}"
        verb = "would be released to" if self.status == Status.PLANNED else "released to"
        return f"{len(self.layers)} layer(s) {verb} {self.environment}"

    @property
    def exit_code(self) -> int:
        return ExitCode.ACTION_FAILED if self.status == Status.FAILED else ExitCode.SUCCESS


@dataclass
class ConnectionSync:
    """What the post-publish connection pass did."""

    created: list[str] = field(default_factory=list)
    existed: list[str] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)


@dataclass
class ReleaseOptions:
    """Everything the caller chooses, as opposed to what the recipe declares."""

    environment: str
    repository_root: pathlib.Path = pathlib.Path(".")
    layers: tuple[str, ...] | None = None
    parameter_file: pathlib.Path = parameters.DEFAULT_PARAMETER_FILE
    extend_parameters: bool = True
    accumulate_ids: bool = True
    dry_run: bool = False
    sync_connections: bool = True
    item_types: tuple[str, ...] = DEFAULT_ITEM_TYPES


def plan(recipe: Recipe, options: ReleaseOptions) -> list[tuple[str, DeployPolicy, pathlib.Path]]:
    """The layers a release would touch, in order, with the policy and directory for each."""
    ordered = policy.order(recipe, list(options.layers) if options.layers else None)
    planned: list[tuple[str, DeployPolicy, pathlib.Path]] = []
    for layer in ordered:
        resolved = policy.resolve(recipe, layer, environment=options.environment)
        planned.append((layer, resolved, repository_directory(recipe, layer, options.repository_root)))
    return planned


def repository_directory(recipe: Recipe, layer: str, root: pathlib.Path) -> pathlib.Path:
    """Where this layer's items live: `git.directory`, declared in the recipe.

    There used to be a fallback to `solution/<layer>`. It matched the flat layout and
    nothing else, and a directory guessed wrong is a release that quietly deploys nothing,
    so a layer without one is an error now.
    """
    git = recipe.value(layer, "git") or {}
    directory = git.get("directory")
    if not directory:
        raise RecipeError(
            f"layer '{layer}' declares no git directory, so release cannot find its items",
            hint="Set `git_directoryName` (or `git.directory`) for the layer in the base recipe, "
                 "e.g. \"solution/engineering/prepare\".",
        )
    return (pathlib.Path(root) / str(directory)).resolve()


def connection_resolver(cli: FabricCli, *, dry_run: bool = False) -> parameters.ConnectionResolver:
    """Look a connection id up by display name, caching within a run."""
    cache: dict[str, str | None] = {}

    def resolve(name: str) -> str | None:
        if name in cache:
            return cache[name]
        if dry_run:
            cache[name] = None
            return None
        path = FabPath.connection(name)
        cache[name] = cli.get_value(path, "id") if cli.exists(path) else None
        return cache[name]

    return resolve


def run(
    recipe: Recipe,
    options: ReleaseOptions,
    *,
    cli: FabricCli,
    log: RunLog,
    credential: Any | None = None,
    credentials: Any = None,
) -> ReleaseResult:
    """Release every selected layer, in dependency order, into one environment."""
    result = ReleaseResult(environment=options.environment)
    planned = plan(recipe, options)
    if not planned:
        log.warning("no layers selected for release")
        return result

    parameter_file = pathlib.Path(options.parameter_file)
    if not parameter_file.is_absolute():
        parameter_file = (options.repository_root / parameter_file).resolve()

    resolve_connection = connection_resolver(cli, dry_run=options.dry_run)
    if options.extend_parameters:
        overlay = parameters.render(
            recipe,
            environment=options.environment,
            resolve_connection=resolve_connection,
            layers=[layer for layer, _, _ in planned],
        )
        overlay = parameters.merge_legacy_bindings(
            overlay,
            recipe,
            legacy_path=parameter_file.parent / parameters.LEGACY_BINDING_FILE,
            resolve_connection=resolve_connection,
        )
        result.warnings.extend(overlay.warnings)
        for warning in overlay.warnings:
            log.warning(warning)
        target = (parameter_file.parent / parameters.OVERLAY_RELATIVE).resolve()
        if options.dry_run:
            log.info(f"would write parameter overlay → {target}" + (" (empty)" if overlay.is_empty else ""))
        else:
            parameters.write(overlay, parameter_file)
            log.info(f"parameter overlay → {target}" + (" (empty)" if overlay.is_empty else ""))
        missing_extend = parameters.check_extends(parameter_file)
        if missing_extend and not overlay.is_empty:
            result.warnings.append(missing_extend)
            log.warning(missing_extend)

    if options.dry_run:
        for layer, resolved, directory in planned:
            workspace = recipe.workspace_name(layer)
            log.info(f"would release {layer} → {workspace}")
            log.debug(f"  directory      {directory}")
            log.debug(f"  item types     {', '.join(resolved.item_types_in_scope or options.item_types)}")
            log.debug(f"  publish args   {resolved.publish_arguments() or '(defaults)'}")
            log.debug(
                "  unpublish      "
                + ("skipped" if resolved.unpublish_skip else str(resolved.unpublish_arguments()))
            )
            if resolved.effective_features:
                log.debug(f"  feature flags  {', '.join(resolved.effective_features)}")
            result.layers.append(LayerResult(layer=layer, workspace=workspace, status=Status.PLANNED))
        return result

    library = _library()
    _configure_logging(library, log)

    # Cross-layer id mapping: an item published in an earlier layer is referenced by its
    # logicalId in a later one, so each layer's resolved ids are carried forward as
    # find_replace entries (E09 section 5).
    accumulated: dict[str, Any] = {}

    for layer, resolved, directory in planned:
        workspace = recipe.workspace_name(layer)
        layer_result = _release_layer(
            library,
            recipe=recipe,
            layer=layer,
            workspace=workspace,
            directory=directory,
            deploy=resolved,
            options=options,
            parameter_file=parameter_file,
            accumulated=accumulated if options.accumulate_ids else None,
            cli=cli,
            log=log,
            credential=credential,
        )
        result.layers.append(layer_result)
        if not layer_result.ok:
            log.error(f"✖ {layer}: {layer_result.message}")
            return result

    if options.sync_connections:
        result.connections = sync_item_connections(recipe, cli=cli, log=log, credentials=credentials)

    return result


def sync_item_connections(
    recipe: Recipe,
    *,
    cli: FabricCli,
    log: RunLog,
    credentials: Any = None,
    sleep: Any = None,
) -> ConnectionSync:
    """Create the connections that could not be made until their items existed.

    A connection declared with `from_item` points at something the repository owns, so on a
    fresh environment `fabricops setup` has to skip it - the item is not there yet. This is
    the other half of that: once release has published the items, the same connection
    actions run again, built from the same plan, so there is one definition of what a
    connection is and no second implementation to drift from it.
    """
    from ..engine import RunContext, build_plan
    from ..engine.connections import CreateConnection

    pending = [
        action
        for action in build_plan(recipe)
        if isinstance(action, CreateConnection) and action.source_item is not None
    ]
    summary = ConnectionSync()
    if not pending:
        return summary

    log.info("")
    log.info("syncing connections that were waiting on their items")
    # Credentials have to be threaded in: building a SQL connection needs a service
    # principal, and a context built without one fails every connection with "needs service
    # principal credentials" - which reads as a missing credentials file rather than as an
    # argument that was never passed.
    from ..engine.connections import Credentials

    import time

    ctx = RunContext(
        cli=cli,
        log=log,
        recipe=recipe,
        dry_run=False,
        credentials=credentials or Credentials(),
        sleep=sleep or time.sleep,
    )
    for action in pending:
        try:
            outcome = action.apply(ctx)
        except FabricOpsError as error:
            summary.waiting.append(f"{action.connection_name}: {error}")
            log.warning(f"  {action.connection_name}: {error}")
            continue
        if outcome.status == "skipped":
            summary.waiting.append(f"{action.connection_name}: {outcome.message}")
            log.warning(f"  {action.connection_name}: {outcome.message}")
        elif outcome.status == "created":
            summary.created.append(action.connection_name)
            log.info(f"  created  {action.connection_name}")
        else:
            summary.existed.append(action.connection_name)
            log.debug(f"  existed  {action.connection_name}")
    return summary


def _release_layer(
    library: Any,
    *,
    recipe: Recipe,
    layer: str,
    workspace: str,
    directory: pathlib.Path,
    deploy: DeployPolicy,
    options: ReleaseOptions,
    parameter_file: pathlib.Path,
    accumulated: dict[str, Any] | None,
    cli: FabricCli,
    log: RunLog,
    credential: Any,
) -> LayerResult:
    if not directory.exists():
        log.info(f"skipping {layer}: {directory} does not exist")
        return LayerResult(layer=layer, workspace=workspace, status=Status.SKIPPED, message="no source directory")

    for flag in deploy.effective_features:
        library.append_feature_flag(flag)
    if deploy.destructive_features:
        log.warning(
            f"{layer}: {', '.join(deploy.destructive_features)} enabled - unpublish may delete "
            "items that hold data"
        )
    for name, value in deploy.constants.items():
        setattr(library.constants, name, value)

    workspace_path = FabPath.workspace(workspace)
    if not cli.exists(workspace_path):
        return LayerResult(
            layer=layer,
            workspace=workspace,
            status=Status.FAILED,
            message=f"workspace '{workspace}' does not exist - run `fabricops setup` first",
        )
    workspace_id = cli.get_value(workspace_path, "id")

    log.info(f"releasing {layer} → {workspace}")
    try:
        target = library.FabricWorkspace(
            workspace_id=workspace_id,
            environment=options.environment,
            repository_directory=str(directory),
            item_type_in_scope=list(deploy.item_types_in_scope or options.item_types),
            token_credential=credential,
            parameter_file_path=str(parameter_file),
        )
        if accumulated is not None and accumulated:
            target.environment_parameter = _merged_parameters(target.environment_parameter, accumulated)

        library.publish_all_items(target, **deploy.publish_arguments())
        item_ids = _item_ids(target)

        if accumulated is not None:
            _accumulate(accumulated, target, options.environment)

        if deploy.unpublish_skip:
            log.info(f"{layer}: unpublish skipped by recipe")
        else:
            library.unpublish_all_orphan_items(target, **deploy.unpublish_arguments())

    except Exception as error:  # noqa: BLE001 - the library raises many types; the caller wants one
        if _is_auth_error(error):
            raise AuthError(f"{layer}: {error}") from error
        return LayerResult(layer=layer, workspace=workspace, status=Status.FAILED, message=str(error))

    return LayerResult(
        layer=layer,
        workspace=workspace,
        status=Status.COMPLETED,
        published=len(item_ids),
        unpublished=not deploy.unpublish_skip,
        item_ids=item_ids,
    )


def _item_ids(target: Any) -> dict[str, str]:
    """`{logicalId: guid}` for everything the workspace just published."""
    mapping: dict[str, str] = {}
    for items in (getattr(target, "repository_items", {}) or {}).values():
        for details in items.values():
            logical_id = getattr(details, "logical_id", None)
            guid = getattr(details, "guid", None)
            if logical_id and guid:
                mapping[str(logical_id)] = str(guid)
    return mapping


def _accumulate(accumulated: dict[str, Any], target: Any, environment: str) -> None:
    """Add this layer's logicalId → guid pairs to the mapping later layers will use."""
    entries = accumulated.setdefault("find_replace", [])
    known = {entry["find_value"] for entry in entries}
    for logical_id, guid in _item_ids(target).items():
        if logical_id not in known:
            entries.append({"find_value": logical_id, "replace_value": {environment: guid}})


def _merged_parameters(existing: dict[str, Any] | None, accumulated: dict[str, Any]) -> dict[str, Any]:
    """The layer's own parameters plus the accumulated cross-layer id mappings."""
    merged: dict[str, Any] = {key: list(value) if isinstance(value, list) else value
                              for key, value in (existing or {}).items()}
    for key, value in accumulated.items():
        if isinstance(value, list):
            merged[key] = list(merged.get(key) or []) + value
        else:
            merged[key] = value
    return merged


_AUTH_PATTERN = re.compile(r"unauthorized|forbidden|401|403|authentication|credential", re.IGNORECASE)


def _is_auth_error(error: Exception) -> bool:
    return bool(_AUTH_PATTERN.search(str(error)))


def _configure_logging(library: Any, log: RunLog) -> None:
    """Point the library's own logging at the same place as ours.

    `change_log_level` only understands DEBUG - there is no way to make the library
    quieter - so it is called only when FabricOps is itself running verbose.
    """
    from ..obs.logging import Level

    if log.level >= Level.DEBUG:
        library.change_log_level("DEBUG")
    trace = getattr(log, "trace_file", None)
    if not trace:
        return
    external = logging.getLogger("fabricops.fabric_cicd")
    external.setLevel(logging.DEBUG)
    if not external.handlers:
        path = pathlib.Path(trace).with_suffix(".fabric-cicd.log")
        path.parent.mkdir(parents=True, exist_ok=True)
        external.addHandler(logging.FileHandler(path, encoding="utf-8"))
    library.configure_external_file_logging(external)


def _library() -> Any:
    """Import `fabric-cicd`, turning an absent install into a FabricOps error."""
    try:
        import fabric_cicd
    except ImportError as error:  # pragma: no cover - exercised by the import guard test
        raise FabricOpsError(
            "fabric-cicd is not installed, so `fabricops release` cannot run",
            hint="pip install -r automation/resources/requirements.txt",
        ) from error
    return fabric_cicd
