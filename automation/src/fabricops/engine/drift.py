"""Drift: does the tenant still look like the recipe says it should (E03-S8)?

There is no separate "check" implementation. A drift check *is* a dry run: every action
already reads before it acts and reports whether it would create, update or leave alone,
so drift is that same report read differently. Keeping one code path means the check can
never disagree with what a real run would do.

FabricOps has no state file. Fabric is the source of truth, and the recipe is the desired
state; this compares the two live.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import ExitCode
from ..obs.logging import RunLog
from .context import RunContext
from .executor import execute
from .manifest import Manifest
from .plan import Plan

#: How an action's dry-run status reads as drift.
MISSING = "missing"      # would be created - the resource is not there
DIFFERS = "differs"      # would be updated - it is there but not as declared
IN_SYNC = "in sync"

_FROM_STATUS = {
    "created": MISSING,
    "updated": DIFFERS,
    "existed": IN_SYNC,
    "skipped": IN_SYNC,
}


@dataclass
class Finding:
    action_id: str
    kind: str
    layer: str | None
    description: str
    state: str
    message: str = ""

    @property
    def is_drift(self) -> bool:
        return self.state in (MISSING, DIFFERS)


@dataclass
class DriftReport:
    findings: list[Finding] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def drifted(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.is_drift]

    @property
    def exit_code(self) -> int:
        # A check that could not read the tenant has not proved anything, so an outright
        # failure outranks drift rather than being folded into it.
        if self.failures:
            return ExitCode.ACTION_FAILED
        return ExitCode.DRIFT if self.drifted else ExitCode.SUCCESS

    def describe(self) -> list[str]:
        if self.failures:
            return [f"  could not check: {failure}" for failure in self.failures]
        if not self.drifted:
            return [f"  {len(self.findings)} resource(s) checked, all in sync"]

        width = max(len(finding.kind) for finding in self.drifted)
        lines = []
        for finding in self.drifted:
            scope = f"{finding.layer} · " if finding.layer else ""
            detail = f"  {finding.message}" if finding.message else ""
            lines.append(f"  {finding.state:<8} {finding.kind.ljust(width)}  {scope}{finding.description}{detail}")
        return lines


def detect(plan: Plan, ctx: RunContext, *, log: RunLog | None = None) -> DriftReport:
    """Run `plan` as a dry run and read the result as a drift report.

    `ctx` must be a dry-run context. Passing a live one would make a "check" write to the
    tenant, so it is refused rather than trusted.
    """
    if not ctx.dry_run:
        raise ValueError("drift detection needs a dry-run context; it must never write")

    manifest = Manifest(
        run_id=(log or ctx.log).run_id,
        command="plan --check",
        solution=ctx.recipe.solution,
        environment=ctx.recipe.environment,
        kind=ctx.recipe.kind,
        dry_run=True,
    )
    report = execute(plan, ctx, manifest=manifest)

    findings = [
        Finding(
            action_id=str(entry.get("id")),
            kind=str(entry.get("kind")),
            layer=entry.get("layer"),
            description=str(entry.get("description")),
            state=_FROM_STATUS.get(str(entry.get("status")), IN_SYNC),
            message=str(entry.get("message") or ""),
        )
        for entry in manifest.actions
        if entry.get("status") != "failed"
    ]
    failures = [f"{action.describe()}: {error}" for action, error in report.failures]
    return DriftReport(findings=findings, failures=failures)
