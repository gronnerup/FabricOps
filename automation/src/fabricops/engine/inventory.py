"""Feature workspace inventory and TTL reaping (E08-S2, E08-S3).

A feature workspace is identified by what it *says about itself*, never by parsing its
display name. Two records carry that, in order of reliability:

1. the **workspace description**, stamped at creation
   (`FabricOps: solution=… layer=… branch=… developer=… created=…`). Always available,
   API-readable, survives a rename;
2. **tags** (E05). Better for querying at scale, but tags can only be created by a tenant
   admin and the recipe may declare none at all, so they are a bonus rather than the
   mechanism.

Reaping is dry-run by default and deletes nothing that does not identify itself as a
feature workspace of this solution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from ..fabric.cli import FabricCli
from ..fabric.paths import FabPath
from ..obs.logging import RunLog

DESCRIPTION_PREFIX = "FabricOps:"
DEFAULT_TTL_DAYS = 14

#: Set `Retain:true` (tag) or `retain=true` (description) to make a workspace un-reapable.
RETAIN_TAG = "retain:true"

_FIELD = re.compile(r"(\w+)=([^\s]+)")


def stamp(
    *,
    solution: str | None,
    layer: str,
    branch: str,
    developer: str | None,
    created: str,
    base: str | None = None,
) -> str:
    """The description a feature workspace carries so cleanup can identify it later.

    `base` is the base environment's workspace name as a template with `{layer}` left in -
    `Brickyard - {layer} [dev]` - so a notebook in the feature workspace can find the shared
    storage it reads without guessing from its own name. It goes last, because it contains
    spaces and the other fields do not.
    """
    parts = [
        f"solution={solution or 'default'}",
        f"layer={layer}",
        f"branch={branch}",
        f"developer={developer or 'unknown'}",
        f"created={created}",
    ]
    if base:
        parts.append(f"base={base}")
    return f"{DESCRIPTION_PREFIX} " + " ".join(parts)


def parse_stamp(description: str | None) -> dict[str, str]:
    """Fields out of a workspace description, or {} when it is not one of ours."""
    if not description or DESCRIPTION_PREFIX not in description:
        return {}
    body = description.split(DESCRIPTION_PREFIX, 1)[1]
    base = None
    if " base=" in body:
        body, base = body.rsplit(" base=", 1)      # the one field that may contain spaces
    fields = {key: value for key, value in _FIELD.findall(body)}
    if base:
        fields["base"] = base.strip()
    return fields


@dataclass
class FeatureWorkspace:
    """One feature workspace, as the tenant describes it."""

    id: str
    name: str
    solution: str | None = None
    layer: str | None = None
    branch: str | None = None
    developer: str | None = None
    created: datetime | None = None
    last_sync: datetime | None = None
    tags: list[str] = field(default_factory=list)

    @property
    def retained(self) -> bool:
        return any(tag.lower() == RETAIN_TAG for tag in self.tags)

    def age_days(self, now: datetime) -> float | None:
        reference = self.last_sync or self.created
        return None if reference is None else (now - reference).total_seconds() / 86400


@dataclass
class ReapDecision:
    workspace: FeatureWorkspace
    delete: bool
    reason: str
    #: Why: "retain", "branch-gone", "ttl", "unknown-age" or "keep". A branch that no
    #: longer exists is an unambiguous signal; age alone is a guess, and the two deserve
    #: different levels of trust when deleting unattended.
    signal: str = ""


@dataclass
class ReapReport:
    decisions: list[ReapDecision] = field(default_factory=list)
    #: Doomed on a signal the caller chose not to act on automatically. Reported, not deleted.
    deferred: list[str] = field(default_factory=list)
    applied: bool = False
    deleted: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def doomed(self) -> list[ReapDecision]:
        return [decision for decision in self.decisions if decision.delete]

    def describe(self) -> list[str]:
        if not self.decisions:
            return ["  no feature workspaces found"]
        width = max(len(decision.workspace.name) for decision in self.decisions)
        verb = "deleted" if self.applied else "would delete"
        return [
            f"  {(verb if decision.delete else 'keep'):<13} "
            f"{decision.workspace.name.ljust(width)}  {decision.reason}"
            for decision in self.decisions
        ]


def list_feature_workspaces(
    cli: FabricCli, *, solution: str | None = None, log: RunLog | None = None
) -> list[FeatureWorkspace]:
    """Every workspace that identifies itself as a feature workspace of this solution."""
    response = cli.api("workspaces", check=False)
    payload = response.get("value") if response.ok else None
    found: list[FeatureWorkspace] = []

    for entry in payload or []:
        fields = parse_stamp(entry.get("description"))
        if not fields:
            continue
        if solution and fields.get("solution") not in (solution, None):
            continue
        found.append(
            FeatureWorkspace(
                id=str(entry.get("id")),
                name=str(entry.get("displayName") or entry.get("name") or ""),
                solution=fields.get("solution"),
                layer=fields.get("layer"),
                branch=fields.get("branch"),
                developer=fields.get("developer"),
                created=_parse_time(fields.get("created")),
                tags=[str(tag.get("displayName") or tag) for tag in (entry.get("tags") or [])],
            )
        )

    for workspace in found:
        workspace.last_sync = _last_sync(cli, workspace.id)
        if log:
            log.debug(f"  {workspace.name}: branch={workspace.branch} last_sync={workspace.last_sync}")
    return found


def _last_sync(cli: FabricCli, workspace_id: str) -> datetime | None:
    """When git last agreed with this workspace, if it is connected at all."""
    response = cli.api(f"workspaces/{workspace_id}/git/connection", check=False)
    if not response.ok:
        return None
    body = response.body if isinstance(response.body, dict) else {}
    state = body.get("gitSyncDetails") or {}
    return _parse_time(state.get("lastSyncTime"))


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def decide(
    workspaces: Iterable[FeatureWorkspace],
    *,
    now: datetime,
    ttl_days: int = DEFAULT_TTL_DAYS,
    branch_exists: Callable[[str], bool | None] | None = None,
) -> list[ReapDecision]:
    """Which feature workspaces have outlived their branch.

    `branch_exists` returns True, False, or None when it cannot tell. None is treated as
    "do not know", never as "gone" - a git provider being unreachable must not delete
    someone's workspace.
    """
    decisions: list[ReapDecision] = []
    for workspace in workspaces:
        if workspace.retained:
            decisions.append(ReapDecision(workspace, False, "Retain:true", signal="retain"))
            continue

        gone = branch_exists(workspace.branch) is False if (branch_exists and workspace.branch) else None
        if gone:
            decisions.append(ReapDecision(workspace, True, f"branch '{workspace.branch}' no longer exists", signal="branch-gone"))
            continue

        age = workspace.age_days(now)
        if age is None:
            decisions.append(ReapDecision(workspace, False, "no created or last-sync time to age against", signal="unknown-age"))
        elif age > ttl_days:
            since = "last sync" if workspace.last_sync else "created"
            decisions.append(ReapDecision(workspace, True, f"{age:.0f} days since {since} (ttl {ttl_days})", signal="ttl"))
        else:
            decisions.append(ReapDecision(workspace, False, f"{age:.0f} days old (ttl {ttl_days})", signal="keep"))
    return decisions


def reap(
    cli: FabricCli,
    decisions: list[ReapDecision],
    *,
    log: RunLog,
    apply: bool = False,
    apply_when: str = "both",
) -> ReapReport:
    """Act on `decisions`. Deletes nothing unless `apply` is set.

    `apply_when="branch-gone"` deletes only workspaces whose branch no longer exists and
    leaves the age-based ones in the report. A deleted branch is a fact; fourteen days of
    quiet is a colleague on holiday as often as an abandoned feature.
    """
    report = ReapReport(decisions=decisions, applied=apply)
    if not apply:
        return report

    for decision in report.doomed:
        if apply_when == "branch-gone" and decision.signal != "branch-gone":
            report.deferred.append(decision.workspace.name)
            log.info(f"  left for review: {decision.workspace.name} ({decision.reason})")
            continue
        path = FabPath.workspace(decision.workspace.name)
        try:
            cli.rm(path)
        except Exception as error:  # noqa: BLE001 - one failure must not stop the sweep
            report.failures.append(f"{decision.workspace.name}: {error}")
            log.error(f"  failed to delete {decision.workspace.name}: {error}")
            continue
        report.deleted.append(decision.workspace.name)
        log.info(f"  deleted {decision.workspace.name}")
    return report


def azure_devops_branch_checker(organization: str, project: str, repository: str, token: str | None):
    """A `branch_exists` for Azure DevOps. Returns None for anything it cannot determine.

    Uses the refs API with a prefix filter and compares the *exact* ref name, because the
    filter is a prefix: asking for `heads/feature/x` also returns `feature/x-2`. Accepts
    the pipeline's System.AccessToken as a bearer token and a PAT as basic auth. Without a
    token nothing can be read, so the answer is "do not know", never "gone".
    """
    import base64
    import json as _json
    import urllib.error
    import urllib.parse
    import urllib.request

    def exists(branch: str) -> bool | None:
        if not token:
            return None
        ref = f"refs/heads/{branch}"
        url = (f"https://dev.azure.com/{organization}/{urllib.parse.quote(project)}/_apis/git/repositories/"
               f"{urllib.parse.quote(repository)}/refs?filter={urllib.parse.quote('heads/' + branch, safe='')}"
               f"&api-version=7.1")
        for auth in (f"Bearer {token}", "Basic " + base64.b64encode(f":{token}".encode()).decode()):
            request = urllib.request.Request(url, headers={"Accept": "application/json", "Authorization": auth})
            try:
                with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - fixed https host
                    body = _json.loads(response.read().decode() or "{}")
                    return any(item.get("name") == ref for item in body.get("value") or [])
            except urllib.error.HTTPError as error:
                if error.code in (401, 403):
                    continue                     # try the other auth scheme
                return None
            except Exception:  # noqa: BLE001 - offline, DNS, proxy: all mean "do not know"
                return None
        return None

    return exists


def github_branch_checker(owner: str, repository: str, token: str | None):
    """A `branch_exists` for GitHub. Returns None for anything it cannot determine.

    GitHub is not a Fabric API, so this goes over plain HTTPS rather than through the
    Fabric CLI. Without a token it will only see public repositories, which is why an
    unauthenticated 404 is reported as "do not know" rather than "gone".
    """
    import urllib.error
    import urllib.request

    def exists(branch: str) -> bool | None:
        url = f"https://api.github.com/repos/{owner}/{repository}/branches/{branch}"
        request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - fixed https host
                return response.status == 200
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return False if token else None
            return None
        except Exception:  # noqa: BLE001 - offline, DNS, proxy: all mean "do not know"
            return None

    return exists
