"""Executing a plan: idempotent, ordered, recorded, and honest about failures."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..errors import AuthError, ExitCode, FabricOpsError
from ..obs.logging import RunLog
from .actions import Action, ActionResult, STATUS_MARKS
from .context import RunContext
from .manifest import Manifest
from .plan import Plan


@dataclass
class ExecutionReport:
    manifest: Manifest
    context: RunContext
    failures: list[tuple[Action, Exception]] = field(default_factory=list)
    #: Advisory actions that failed, as (label, layer, error). Reported, but they do not
    #: change the exit code.
    warnings: list[tuple[str, str, str]] = field(default_factory=list)
    #: Set when the run stopped early rather than working through the plan.
    aborted: str | None = None

    @property
    def exit_code(self) -> int:
        if any(isinstance(error, AuthError) for _action, error in self.failures):
            return ExitCode.AUTH_ERROR
        return ExitCode.ACTION_FAILED if self.failures else ExitCode.SUCCESS

    @property
    def counts(self) -> dict[str, int]:
        return self.manifest.counts


def execute(
    plan: Plan,
    ctx: RunContext,
    *,
    manifest: Manifest,
    destroy: bool = False,
    stop_on_error: bool = False,
) -> ExecutionReport:
    """Run every action in `plan` (or its teardown), recording results as we go."""
    log = ctx.log
    actions = plan.for_destroy() if destroy else list(plan)
    report = ExecutionReport(manifest=manifest, context=ctx)
    failed_ids: set[str] = set()
    # A run log has to stay sequential, so each line carries its own scope rather than
    # relying on a header - the plan revisits layers in several passes.
    scope_width = max((len(action.layer or "solution") for action in actions), default=8)

    for action in actions:
        scope = (action.layer or "solution").ljust(scope_width)

        blocked = [dep for dep in action.depends_on if dep in failed_ids]
        if blocked and not destroy:
            log.step(f"{scope} \u00b7 {action.describe()}")
            log.skip(f" ⚠ Skipped (depends on failed {', '.join(blocked)})")
            manifest.add(action.id, action.kind, action.layer, action.describe(), "skipped", 0.0,
                         message=f"blocked by {', '.join(blocked)}")
            failed_ids.add(action.id)
            continue

        if action.incidental:
            log.debug(f"{scope} \u00b7 {action.describe()}")
        else:
            log.step(f"{scope} \u00b7 {action.describe()}")
        started = time.monotonic()
        try:
            result = action.destroy(ctx) if destroy else action.apply(ctx)
        except FabricOpsError as error:
            duration = time.monotonic() - started
            if action.advisory and not isinstance(error, AuthError):
                log.skip(" ⚠ Skipped (optional)")
                log.warning(f"      {error}")
                manifest.add(action.id, action.kind, action.layer, action.describe(), "skipped",
                             duration, str(error))
                report.warnings.append((action.describe(), action.layer or "solution", str(error)))
                continue
            log.fail()
            log.error(f"      {error}")
            manifest.add(action.id, action.kind, action.layer, action.describe(), "failed", duration, str(error))
            report.failures.append((action, error))
            failed_ids.add(action.id)
            if isinstance(error, AuthError):
                # Nothing after this can succeed, and repeating the same message 27 times
                # buries the one line that says what to do about it.
                log.error("  stopping: the identity could not be authenticated")
                report.aborted = "authentication"
                break
            if stop_on_error:
                break
            continue

        duration = time.monotonic() - started
        if result is None:
            log.skip(" ⚠ Nothing to delete")
            manifest.add(action.id, action.kind, action.layer, action.describe(), "skipped", duration)
            continue

        if not action.incidental:
            _report_result(log, result, ctx.dry_run)
        ctx.record(action.id, result.outputs)
        manifest.add(action.id, action.kind, action.layer, action.describe(), result.status, duration,
                     result.message, result.outputs)

    return report


def _report_result(log: RunLog, result: ActionResult, dry_run: bool) -> None:
    mark = STATUS_MARKS.get(result.status, " ✔")
    if dry_run and result.status in ("created", "updated"):
        mark = " → would change"
    elif dry_run and result.status == "deleted":
        mark = " → would delete"
    if result.status in ("existed", "skipped"):
        log.skip(mark if not result.message else f"{mark} ({result.message})")
    elif result.status == "failed":
        log.fail(mark)
    else:
        log.ok(mark if not result.message else f"{mark} ({result.message})")


def summarise(log: RunLog, report: ExecutionReport, *, dry_run: bool = False) -> None:
    counts = report.counts
    # In a dry run the statuses are what *would* have happened. Printing "14 created"
    # after a run that created nothing is the kind of line people quote back at you.
    verbs = (
        {"created": "to create", "updated": "to update", "deleted": "to delete"} if dry_run else {}
    )
    parts = [
        f"{count} {verbs.get(status, status)}"
        for status in ("created", "updated", "deleted", "existed", "skipped", "failed")
        if (count := counts.get(status))
    ]
    summary = ", ".join(parts) or "nothing to do"
    log.info("")
    if report.aborted == "authentication":
        _action, error = report.failures[-1]
        log.error(f"✖ stopped before doing anything: {error}")
        return
    # One cause, one line. The same unresolvable developer id produced five identical
    # multi-line warnings - one per layer - which buried the single failure underneath them.
    for warning, layers in _group_warnings(report.warnings):
        suffix = f" ({len(layers)} layers: {', '.join(layers)})" if len(layers) > 1 else ""
        log.warning(f"⚠ {warning}{suffix}")
    if report.failures:
        log.error(f"✖ {summary}")
        for action, error in report.failures:
            log.error(f"    {action.id}: {error}")
    elif dry_run:
        log.success(f"✔ plan complete: {summary}")
    else:
        log.success(f"✔ {summary}")


def _group_warnings(warnings: list[tuple[str, str, str]]) -> list[tuple[str, list[str]]]:
    """Collapse advisory warnings that share a label and an error, keeping order.

    A per-layer action that fails for one tenant-wide reason - an id that is not an Entra
    object, a tenant setting that is off - fails identically in every layer. Repeating it
    once per layer is noise that hides whatever else went wrong.
    """
    # Keyed on the label alone, not the error: the error text carries the workspace name,
    # so five failures of one cause read as five different strings. The label is what says
    # *what* was attempted, and one attempt failing everywhere is one thing to report.
    grouped: dict[str, tuple[str, list[str]]] = {}
    for label, layer, error in warnings:
        first_error, layers = grouped.setdefault(label, (error, []))
        layers.append(layer)
    return [(f"{label}: {error}", layers) for label, (error, layers) in grouped.items()]
