"""Typed errors and process exit codes for FabricOps.

Exit codes are part of the contract with CI (see documentation/specs/E04):

    0  success (or a plan was produced with --dry-run)
    1  one or more actions failed
    2  recipe/config/resolution error - nothing was executed
    3  drift detected (plan only)
    4  authentication or permission error
"""

from __future__ import annotations


class ExitCode:
    SUCCESS = 0
    ACTION_FAILED = 1
    RECIPE_ERROR = 2
    DRIFT = 3
    AUTH_ERROR = 4


class FabricOpsError(Exception):
    """Base class for every error FabricOps raises deliberately."""

    exit_code = ExitCode.ACTION_FAILED

    def __init__(self, message: str, *, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return f"{self.message}\n  hint: {self.hint}" if self.hint else self.message


class RecipeError(FabricOpsError):
    """The recipe could not be found, parsed, validated or merged."""

    exit_code = ExitCode.RECIPE_ERROR


class AuthError(FabricOpsError):
    """Authentication failed, or the identity lacks the required permission."""

    exit_code = ExitCode.AUTH_ERROR


class DriftError(FabricOpsError):
    """`plan` found a difference between the recipe and the live tenant."""

    exit_code = ExitCode.DRIFT


def _first_line(text: str) -> str:
    """The error line, with the CLI's `x <command>: ` prefix trimmed.

    The Fabric CLI interleaves progress with errors on stdout, so the *first* line is often
    "Creating a new Connection..." and the actual reason is further down. Error lines start
    with `x `, so those are preferred; the first non-empty line is only a fallback.
    """
    lines = [line.strip() for line in (text or "").strip().splitlines() if line.strip()]
    for line in lines:
        if line.startswith("x "):
            return line[2:].split(": ", 1)[1].strip() if ": " in line[2:] else line[2:].strip()
    for line in lines:
        if line.startswith("!"):          # the CLI's warning marker, still more use than progress
            return line.lstrip("! ").strip()
    return lines[0] if lines else ""


class FabricCliError(FabricOpsError):
    """A `fab` invocation failed.

    Carries the redacted command so the message is safe to print anywhere.
    """

    def __init__(
        self,
        redacted_command: str,
        returncode: int,
        stderr: str = "",
        *,
        stdout: str = "",
        hint: str | None = None,
        command_id: str | None = None,
    ):
        # The Fabric CLI writes its errors to *stdout*, prefixed with `x <command>:`, and
        # leaves stderr empty. Reading only stderr turned "AuthenticationFailed, run
        # fab auth login" into "no error output", which is worse than saying nothing.
        detail = _first_line(stderr) or _first_line(stdout) or "no error output"
        super().__init__(
            f"fab command failed (exit {returncode}): {redacted_command}\n  {detail}",
            hint=hint,
        )
        self.redacted_command = redacted_command
        self.returncode = returncode
        self.stderr = stderr
        self.command_id = command_id


def summarise_api_error(body: str) -> str:
    """The parts of a Fabric error payload a person needs, before the parts they do not.

    Fabric nests the real explanation in `error.moreDetails[].message` behind a generic
    top-level `message`, and the whole thing is long. Truncating the raw JSON cut the one
    sentence that mattered mid-word - an actual message ended "...is not foun". So the
    detail messages are lifted out first and the raw body is kept only as a fallback.
    """
    import json as _json

    try:
        payload = _json.loads(body)
    except (TypeError, ValueError):
        return body[:800]
    if not isinstance(payload, dict):
        return body[:800]

    error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
    parts: list[str] = []
    code = error.get("errorCode")
    if code:
        parts.append(f"[{code}]")

    details = [
        str(detail.get("message") or detail.get("errorCode"))
        for detail in (error.get("moreDetails") or [])
        if isinstance(detail, dict) and (detail.get("message") or detail.get("errorCode"))
    ]
    parts.extend(details or [str(error.get("message") or "")])

    summary = " ".join(part for part in parts if part).strip()
    request_id = payload.get("requestId") or error.get("requestId")
    if request_id:
        summary = f"{summary}  (requestId {request_id})"
    return summary[:1200] or body[:800]


class FabricApiError(FabricOpsError):
    """A Fabric REST call returned a non-success status."""

    def __init__(self, method: str, path: str, status_code: int, body: str = "", *, hint: str | None = None):
        super().__init__(f"{method.upper()} {path} returned {status_code}: {summarise_api_error(body)}", hint=hint)
        self.method = method
        self.path = path
        self.status_code = status_code
        self.body = body
        if status_code in (401, 403):
            self.exit_code = ExitCode.AUTH_ERROR
