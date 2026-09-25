"""The single place FabricOps invokes the Fabric CLI.

Design rules (documentation/specs/E04):

* argv lists, never interpolated shell strings - no quoting, no escaping helpers;
* JSON in, JSON out - `exists`/`get` are parsed, never string-compared;
* a non-zero exit raises `FabricCliError`; failures never come back as *values*;
* every invocation is logged once, redacted, with its duration and exit code;
* retries, long-running-operation polling and rate limiting live here, not at call sites.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ..errors import AuthError, FabricApiError, FabricCliError, _first_line
from ..obs.logging import Level, RunLog
from ..obs.redaction import Secret, render_argv, to_process_argv

# Errors worth trying again: throttling, transient service faults, transport timeouts.
_TRANSIENT_MARKERS = (
    "429",
    "too many requests",
    "toomanyrequests",
    "throttl",
    "500",
    "502",
    "503",
    "504",
    "internal server error",
    "service unavailable",
    "gateway timeout",
    "timed out",
    "timeout",
    "connection reset",
    "temporarily unavailable",
)


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 20.0
    jitter: float = 0.25

    def delay_for(self, attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None:
            return min(retry_after, 120.0)
        raw = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        return raw * (1 + random.uniform(0, self.jitter))


NO_RETRY = RetryPolicy(attempts=1)


@dataclass(frozen=True)
class CliResult:
    """The outcome of one `fab` invocation. `command` is redacted and safe to print."""

    command: str
    returncode: int
    stdout: str
    stderr: str
    duration: float
    command_id: str
    attempts: int = 1
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def text(self) -> str:
        return self.stdout.strip()

    def json(self, default: Any = None) -> Any:
        if not self.stdout.strip():
            return default
        try:
            return json.loads(self.stdout)
        except json.JSONDecodeError:
            return default


@dataclass(frozen=True)
class ApiResponse:
    status_code: int
    body: Any
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def get(self, key: str, default: Any = None) -> Any:
        return self.body.get(key, default) if isinstance(self.body, dict) else default


#: The CLI reports these on stdout. They mean "the identity is wrong or gone", which is
#: exit code 4 and worth stopping for - not one of 27 identical failures.
_AUTH_MARKERS = (
    "authenticationfailed",
    "failed to get access token",
    "unauthorized",
    "forbidden",
    "tokenexpired",
    "please run `fab auth login`",
)


def _is_auth_failure(result: CliResult) -> bool:
    haystack = f"{result.stderr}\n{result.stdout}".lower()
    return any(marker in haystack for marker in _AUTH_MARKERS)


#: `[NotFound]` is the Fabric CLI's own error code, and the message it builds is
#: "The <type> '<name>' could not be found" (fabric_cli/errors/common.py).
_NOT_FOUND_MARKERS = ("[notfound]", "could not be found", "does not exist")


def _is_not_found(result: CliResult) -> bool:
    haystack = f"{result.stderr}\n{result.stdout}".lower()
    return any(marker in haystack for marker in _NOT_FOUND_MARKERS)


def _is_transient(result: CliResult) -> bool:
    haystack = f"{result.stderr}\n{result.stdout}".lower()
    return any(marker in haystack for marker in _TRANSIENT_MARKERS)


def _retry_after_seconds(text: str) -> float | None:
    lowered = text.lower()
    marker = "retry-after"
    idx = lowered.find(marker)
    if idx == -1:
        return None
    tail = lowered[idx + len(marker) :]
    digits = ""
    for char in tail:
        if char.isdigit():
            digits += char
        elif digits:
            break
    return float(digits) if digits else None


class FabricCli:
    """A thin, logged, retrying wrapper around the `fab` executable."""

    def __init__(
        self,
        log: RunLog | None = None,
        *,
        executable: str | None = None,
        dry_run: bool = False,
        env: dict[str, str] | None = None,
        default_timeout: int = 300,
        retry: RetryPolicy = RetryPolicy(),
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.log = log or RunLog.from_env()
        self.executable = executable or os.environ.get("FABOPS_FAB_BIN", "fab")
        self.dry_run = dry_run
        self.env = env
        self.default_timeout = default_timeout
        self.retry = retry
        self._sleep = sleep
        self.invocations = 0
        self.skipped_writes: list[str] = []

    # ------------------------------------------------------------------- core
    def invoke(
        self,
        argv: Sequence[object],
        *,
        mutating: bool = False,
        expect_json: bool = False,
        retry: RetryPolicy | None = None,
        timeout: int | None = None,
        check: bool = True,
    ) -> CliResult:
        """Run one `fab` command.

        `mutating=True` marks a write, which `--dry-run` reports instead of executing.
        `check=True` (the default) raises `FabricCliError` on a non-zero exit.
        """
        full: list[object] = [self.executable, *argv]
        if expect_json and "--output_format" not in [str(a) for a in argv]:
            full += ["--output_format", "json"]

        command = render_argv(full, enabled=self.log.redaction_enabled)
        command_id = uuid.uuid4().hex[:8]
        policy = retry or self.retry

        if self.dry_run and mutating:
            self.skipped_writes.append(command)
            self.log.debug(f"→ [dry-run] {command}")
            self.log.record("cli", command_id=command_id, command=command, dry_run=True, mutating=True)
            return CliResult(command, 0, "", "", 0.0, command_id, dry_run=True)

        process_argv = to_process_argv(full)
        result: CliResult | None = None

        for attempt in range(1, max(1, policy.attempts) + 1):
            started = time.monotonic()
            try:
                completed = subprocess.run(  # noqa: S603 - argv list, shell=False
                    process_argv,
                    capture_output=True,
                    text=True,
                    timeout=timeout or self.default_timeout,
                    env=self.env,
                    shell=False,
                    check=False,
                )
                stdout, stderr, code = completed.stdout, completed.stderr, completed.returncode
            except subprocess.TimeoutExpired as exc:
                stdout, stderr, code = "", f"timed out after {exc.timeout}s", 124
            except FileNotFoundError as exc:
                raise FabricCliError(
                    command,
                    127,
                    str(exc),
                    hint=f"'{self.executable}' was not found on PATH. Install the Fabric CLI (pip install ms-fabric-cli).",
                    command_id=command_id,
                ) from exc

            duration = time.monotonic() - started
            self.invocations += 1
            result = CliResult(command, code, _clean(stdout), stderr, duration, command_id, attempt)

            self.log.debug(f"→ {command}  [{'ok' if result.ok else f'exit {code}'} in {duration:.2f}s]")
            self.log.record(
                "cli",
                command_id=command_id,
                command=command,
                returncode=code,
                duration_s=round(duration, 3),
                attempt=attempt,
                mutating=mutating,
                stdout=result.stdout[:4000],
                stderr=stderr[:2000],
            )
            if result.ok or not _is_transient(result) or attempt == policy.attempts:
                break

            wait = policy.delay_for(attempt, _retry_after_seconds(f"{stderr}\n{stdout}"))
            self.log.warning(f"  transient failure, retrying in {wait:.1f}s (attempt {attempt}/{policy.attempts})")
            self._sleep(wait)

        assert result is not None
        if check and not result.ok:
            if _is_auth_failure(result):
                raise AuthError(
                    f"{_first_line(result.stderr) or _first_line(result.stdout)}",
                    hint="Run `fab auth login`, or set FAB_SPN_CLIENT_ID / FAB_SPN_CLIENT_SECRET / "
                         "FAB_TENANT_ID for a service principal.",
                )
            raise FabricCliError(
                command, result.returncode, result.stderr, stdout=result.stdout, command_id=command_id
            )
        return result

    # --------------------------------------------------------------- reads
    def exists(self, path: object) -> bool:
        """True/False from `fab exists`, parsed rather than string-compared.

        `fab exists` only answers when it can resolve the whole path. Ask it about an item
        in a workspace that does not exist and it exits 1 with
        `[NotFound] The Workspace '...' could not be found` - which is an answer, not a
        failure, and the answer is no. Any other non-zero exit is still an error.
        """
        result = self.invoke(["exists", str(path)], check=False)

        if not result.ok:
            if _is_auth_failure(result):
                raise AuthError(
                    _first_line(result.stderr) or _first_line(result.stdout),
                    hint="Run `fab auth login`, or set FAB_SPN_CLIENT_ID / FAB_SPN_CLIENT_SECRET / "
                         "FAB_TENANT_ID for a service principal.",
                )
            if _is_not_found(result):
                self.log.trace(f"  {path} does not exist (parent missing)")
                return False
            raise FabricCliError(
                result.command, result.returncode, result.stderr,
                stdout=result.stdout, command_id=result.command_id,
            )

        answer = result.stdout.replace("*", "").strip().strip(".").lower()
        if answer in ("true", "yes"):
            return True
        if answer in ("false", "no"):
            return False
        raise FabricCliError(
            result.command,
            result.returncode,
            f"could not parse existence answer {result.stdout!r}",
            hint="The Fabric CLI output format may have changed; update FabricCli.exists().",
            command_id=result.command_id,
        )

    def get_json(self, path: object, query: str = ".", *, force: bool = True) -> Any:
        """Item/workspace properties as parsed JSON (JMESPath `query`)."""
        argv: list[object] = ["get", str(path)]
        if force:
            argv.append("-f")
        argv += ["-q", query]
        result = self.invoke(argv)
        payload = result.json()
        if payload is None:
            raise FabricCliError(
                result.command,
                result.returncode,
                f"expected JSON, got {result.stdout[:200]!r}",
                command_id=result.command_id,
            )
        return payload

    def get_value(self, path: object, query: str, *, force: bool = True) -> str:
        """A single scalar property (e.g. `id`), as trimmed text."""
        argv: list[object] = ["get", str(path)]
        if force:
            argv.append("-f")
        argv += ["-q", query]
        value = self.invoke(argv).stdout.strip().strip('"')
        if not value:
            raise FabricCliError(
                render_argv([self.executable, *argv], enabled=self.log.redaction_enabled),
                0,
                f"query '{query}' returned no value for {path}",
                hint="The object may not exist yet, or the property name is wrong.",
            )
        return value

    # -------------------------------------------------------------- writes
    def mkdir(self, path: object, *, params: str | None = None, check: bool = True) -> CliResult:
        argv: list[object] = ["mkdir", str(path)]
        if params:
            argv += ["-P", params]
        return self.invoke(argv, mutating=True, check=check)

    def rm(self, path: object, *, check: bool = True) -> CliResult:
        return self.invoke(["rm", str(path), "-f"], mutating=True, check=check)

    def set_property(self, path: object, query: str, value: object) -> CliResult:
        rendered = json.dumps(value, separators=(",", ":")) if isinstance(value, (dict, list)) else value
        return self.invoke(["set", str(path), "-q", query, "-i", rendered, "-f"], mutating=True)

    def import_definition(
        self, path: object, source: object, *, definition_format: str | None = None, check: bool = True
    ) -> CliResult:
        """`fab import` an item definition from a local directory."""
        argv: list[object] = ["import", str(path), "-i", str(source)]
        if definition_format:
            argv += ["--format", definition_format]
        return self.invoke([*argv, "-f"], mutating=True, check=check)

    def export_definition(
        self, path: object, target: object, *, definition_format: str | None = None, check: bool = False
    ) -> CliResult:
        """`fab export` an item definition to a local directory.

        A read, so it runs in a dry run too. `check` defaults to False because "the item
        has no definition yet" is an ordinary answer, not a failure.
        """
        argv: list[object] = ["export", str(path), "-o", str(target)]
        if definition_format:
            argv += ["--format", definition_format]
        return self.invoke([*argv, "-f"], mutating=False, check=check)

    #: `acl get` with no query returns a listing of available *field paths*, not the rows -
    #: `[0].principal.id`, `[0].role` and so on. Asking for the rows takes a JMESPath query.
    #: Without one the parse found nothing, every role looked unassigned, and every run
    #: reported `Updated` on every layer.
    ACL_QUERY = "[*].{id: principal.id, type: principal.type, role: role}"

    def acl_get(self, path: object) -> list[dict[str, Any]]:
        """Current role assignments as `{id, type, role}`, or [] when they cannot be read.

        A read, so it runs in a dry run. An unreadable ACL (the workspace does not exist
        yet, or the identity is not an admin) is reported as "no assignments" rather than
        raised - the caller's next move is to assign, which will fail loudly enough.
        """
        result = self.invoke(["acl", "get", str(path), "-q", self.ACL_QUERY], check=False)
        if not result.ok:
            return []
        payload = result.json(default=[])
        if isinstance(payload, dict):
            payload = payload.get("value") or payload.get("accessDetails") or []
        return [entry for entry in payload if isinstance(entry, dict)]

    def acl_set(self, path: object, identity: str, role: str) -> CliResult:
        return self.invoke(["acl", "set", str(path), "-I", identity, "-R", role.lower(), "-f"], mutating=True)

    def config_set(self, key: str, value: str) -> CliResult:
        return self.invoke(["config", "set", key, value], mutating=False)

    def login_service_principal(self, client_id: str, client_secret: Secret | str, tenant_id: str) -> CliResult:
        """Interactive-free login.

        Prefer the FAB_SPN_* environment variables in pipelines; this exists for local
        runs and for parity with the current scripts. The secret is wrapped so it is
        masked in every log sink.
        """
        secret = client_secret if isinstance(client_secret, Secret) else Secret(client_secret, "client_secret")
        return self.invoke(
            ["auth", "login", "-u", client_id, "-p", secret, "--tenant", tenant_id],
            mutating=False,
        )

    # ----------------------------------------------------------------- api
    def api(
        self,
        path: str,
        *,
        method: str = "get",
        body: Any = None,
        audience: str | None = None,
        headers: str | None = None,
        expect: Sequence[int] = (200, 201, 202, 204),
        poll_lro: bool = True,
        retry: RetryPolicy | None = None,
        mutating: bool | None = None,
        check: bool = True,
    ) -> ApiResponse:
        """Call the Fabric REST API through `fab api`, with LRO handling."""
        argv: list[object] = ["api", path, "-X", method.lower()]
        if body is not None:
            argv += ["-i", json.dumps(body, separators=(",", ":")) if not isinstance(body, str) else body]
        if audience:
            argv += ["-A", audience]
        if headers:
            argv += ["-H", headers]
        argv.append("--show_headers")

        is_write = method.lower() != "get" if mutating is None else mutating
        result = self.invoke(argv, mutating=is_write, retry=retry, check=check)
        if result.dry_run:
            return ApiResponse(0, None, {})

        payload = result.json(default={}) or {}
        status = int(payload.get("status_code") or 0)
        response = ApiResponse(status, payload.get("text"), payload.get("headers") or {})

        # Requests were logged at debug; responses were logged nowhere, so a failure whose
        # explanation was only in the response body - a relation refused with a bare
        # `BadRequest`, say - could not be investigated without changing the code. Goes
        # through the same redacting sink as everything else.
        if self.log.level >= Level.TRACE:
            self.log.trace(f"  ← {status} {json.dumps(response.body)[:4000]}")

        if poll_lro and status == 202:
            operation_id = _header(response.headers, "x-ms-operation-id")
            if operation_id:
                return self.poll_operation(operation_id)

        # An authentication failure is raised even with check=False. Callers pass check=False
        # to mean "not found is an answer", never "I do not mind if you could not ask" - and
        # a 401 swallowed as an empty body reads as "not connected", "no roles", "no items".
        if status == 401 and status not in expect:
            raise FabricApiError(
                method, path, status, json.dumps(response.body),
                hint="Run `fab auth login`, or set FAB_SPN_CLIENT_ID / FAB_SPN_CLIENT_SECRET / "
                     "FAB_TENANT_ID for a service principal.",
            )
        if check and status and status not in expect:
            # Not truncated here: `summarise_api_error` needs valid JSON to find
            # `error.moreDetails[].message`, and a 500-character slice is not valid JSON. It
            # shortens the result itself.
            raise FabricApiError(method, path, status, json.dumps(response.body))
        return response

    def poll_operation(self, operation_id: str, *, timeout: float = 300.0, interval: float = 2.0) -> ApiResponse:
        """Poll a long-running operation until it settles, with backoff."""
        deadline = time.monotonic() + timeout
        wait = interval
        while True:
            state = self.api(f"operations/{operation_id}", poll_lro=False, mutating=False)
            status = (state.body or {}).get("status") if isinstance(state.body, dict) else None
            if status in ("Succeeded", "Completed"):
                self.log.debug(f"  operation {operation_id}: {status}")
                return state
            if status in ("Failed", "Cancelled", "Undefined"):
                raise FabricApiError(
                    "get", f"operations/{operation_id}", state.status_code, json.dumps(state.body)
                )
            if time.monotonic() >= deadline:
                raise FabricApiError(
                    "get",
                    f"operations/{operation_id}",
                    state.status_code,
                    f"operation still '{status}' after {timeout:.0f}s",
                )
            self._sleep(wait)
            wait = min(wait * 1.5, 15.0)

    # --------------------------------------------------------------- misc
    def enable_cli_debug(self) -> None:
        """At trace level, ask the Fabric CLI for its own HTTP log (it masks tokens)."""
        if self.log.level < Level.TRACE:
            return
        self.config_set("debug_enabled", "true")
        self.log.info("Fabric CLI debug logging enabled (see the fabcli_debug.log path printed by fab)")


def _clean(stdout: str) -> str:
    """Drop the CLI's decorative/status lines so payloads parse cleanly."""
    lines = [
        line
        for line in (stdout or "").splitlines()
        if not line.strip().startswith("!") and not line.strip().startswith("&#x27")
    ]
    return "\n".join(lines).strip()


def _header(headers: dict[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None
