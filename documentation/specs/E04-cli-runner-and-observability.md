# E04 — Fabric CLI runner, structured logging, redaction and dry-run

**Goal:** exactly one place invokes Fabric, and every invocation is logged (with
secrets redacted), retried where appropriate, and fails loudly. Logging is off by
default and switchable to console and/or file.

## Problem

```python
def run_command(command: str) -> str:
    result = subprocess.run(["fab", "-c", command], capture_output=True,
                            text=True, check=EXIT_ON_ERROR)   # EXIT_ON_ERROR = False
    …
    return clean_result
```

Real consequences in the current code:

1. **Failures become values.** With `check=False` and stderr swallowed, a failed
   `get … -q id` returns an error string, which is then used as a workspace id:
   `workspace_id = fabcli.run_command(f"get '{ws}.Workspace' -q id").strip()`.
   Downstream calls hit a malformed URL and fail with an unrelated message.
2. **Shell-string quoting.** Paths are hand-escaped (`workspace_name.replace("/","\\/")`)
   and quoted inline; a display name with an apostrophe or comma breaks the command.
3. **Text parsing.** `exists` is parsed with `.replace("*","").strip().lower()=="true"`,
   and `-P key=value,key=value` parameter strings are concatenated by hand — including
   secrets (`credentialDetails.servicePrincipalSecret={client_secret}`).
4. **No record.** Nothing persists which commands ran, in what order, with what result.
5. **Secrets in argv.** Secrets are passed as command-line parameters, so they are
   visible to `ps`, to CI verbose modes and to any accidental `print`.

## Design

### 1. One runner

```python
class FabricCli:
    def invoke(self, argv: list[str], *, expect_json=False, retry: RetryPolicy | None = None,
               redact: Redaction = Redaction.DEFAULT, timeout: int = 300) -> CliResult
```

* **Argv lists, never strings.** `subprocess.run([...], shell=False)` — no quoting, no
  escaping helper, no `\\/` hacks. A thin `FabPath` type builds `ws.Workspace/item.Type`
  paths and is the only place naming rules live.
* **JSON by default.** All reads use `--output_format json` (or `-q .`) and are parsed;
  `CliResult` exposes `.json`, `.text`, `.returncode`, `.duration`, `.command_id`.
* **Typed errors.** Non-zero exit or an error payload raises
  `FabricCliError(command, returncode, stderr, hint)`. `exists`-style probes return
  `bool` from a dedicated method, never from string comparison.
* **Retries.** Declarative policy per call site: transient HTTP (429/5xx, with
  `Retry-After`), plus `wait_ready` polling with exponential backoff and jitter.
  Fabric's own rate limits are respected (e.g. tags APIs: 25 requests/min/principal —
  see E05).
* **LRO handling** in one place (202 + `x-ms-operation-id` → poll `operations/{id}`),
  replacing the current 5-attempt/2-second loop that gives up after ~10 s.
* **Secrets stay off argv where possible.** Prefer `fab api … -i @file` / stdin and
  `FAB_SPN_*` environment variables (`FAB_TENANT_ID`, `FAB_SPN_CLIENT_ID`,
  `FAB_SPN_CLIENT_SECRET` / `FAB_SPN_CERT_PATH` / `FAB_SPN_FEDERATED_TOKEN`) over
  `auth login -u … -p …`; federated/OIDC credentials are the documented default for
  pipelines.

### 2. Secrets are typed

```python
secret = Secret("…")        # __repr__/__str__ → '***'; .reveal() is explicit and audited
```

Redaction is structural: only `reveal()` puts a secret into an argv, and the logger
records the redacted form of the *same* argv it built. A regex denylist
(`client_secret`, `servicePrincipalSecret`, `credentialDetails.key`, `token`, `pat`,
`password`, `Authorization`, `sig=`, connection strings, JWT-shaped strings) runs as a
second net over stdout/stderr, which we do not control.

### 3. Logging model

| Sink | Default | Content |
| --- | --- | --- |
| Console | `info` | human-readable step lines (today's ✔/⚠/✖ output, preserved) |
| Console `debug` | off | one line per CLI invocation: `→ fab mkdir 'X.Workspace' -P capacityname=Y` (redacted), status, duration |
| Trace file (JSONL) | off | full record per invocation: `run_id`, `action_id`, argv (redacted), returncode, duration, stdout/stderr (redacted, truncated), retry count |
| Fabric CLI HTTP log | off | when `--log-level trace`, FabricOps sets `fab config set debug_enabled true` for the run and reports the path of `fabcli_debug.log` (the CLI masks `Authorization` itself) |

Controls (all optional, all documented in one table):

```
--log-level off|error|warn|info|debug|trace     FABOPS_LOG_LEVEL
--trace-file .fabricops/runs/<run-id>/trace.jsonl   FABOPS_TRACE_FILE
--no-redact            (requires FABOPS_ALLOW_UNREDACTED=1; prints a loud warning)
--output text|json     (machine-readable step/plan output for pipelines)
```

* One `run_id` per process, printed in the header and included in every record, so a
  pipeline log and a trace file can be correlated.
* GitHub Actions / Azure DevOps grouping (`::group::` / `##[group]`) and problem
  matchers for failures, so failures surface in the pipeline UI instead of scrollback.
* The run header prints: FabricOps version, `fab --version`, solution, environment,
  resolved recipe paths, identity (client id — never the secret), redaction state.

### 4. Dry-run / plan

`--dry-run` executes the planner and the *read* side of every action (so `exists`
checks and current-property reads still happen) and prints the write commands it would
run, in order, redacted. Exit code 0 with `--dry-run` means "plan produced";
`--dry-run --output json` emits the plan for diffing in PRs.

### 5. Non-zero exit codes

| Code | Meaning |
| --- | --- |
| 0 | success (or plan produced) |
| 1 | one or more actions failed |
| 2 | recipe/config/resolution error (nothing executed) |
| 3 | drift detected (`plan` only) |
| 4 | authentication/permission error |

Today every script exits 0 unless Python raises, which means a pipeline can be green
while Fabric was never touched. This is the highest-value single fix in the overhaul.

## User stories

**E04-S1 — See every command that ran**
*As a platform engineer debugging a failed setup, I can turn on a log and see the exact
`fab` commands, in order, with results — and no secrets.*
AC: `--log-level debug` prints one redacted line per invocation; `--trace-file` writes
JSONL with argv, status, duration; a test asserts that a known secret value never
appears in either sink.

**E04-S2 — Off by default**
AC: default output is unchanged from today's console UX; no files are written unless
`--trace-file`/`--log-level trace` is requested (the run manifest of E03 is written
under `.fabricops/`, which is git-ignored).

**E04-S3 — Failures fail**
AC: a failing `fab` command raises `FabricCliError` and the run exits non-zero with the
failing action id; regression test covers "workspace id lookup fails → no downstream
call is attempted".

**E04-S4 — No hand-built command strings**
AC: `grep -r "subprocess" automation/scripts` matches only the runner; no `.replace("/",
"\\/")` remains; display names with spaces, `&`, `'` and `,` are covered by tests.

**E04-S5 — Dry-run**
AC: `--dry-run` performs no writes (asserted with a runner in "record" mode) and prints
an ordered plan; `--output json` plan is stable enough to diff.

**E04-S6 — Rate limit and LRO resilience**
AC: 429 with `Retry-After` is honoured; a 202 long-running operation is polled to
completion with backoff and an overall timeout; both are logged.

**E04-S7 — Pipeline-native logs**
AC: step grouping in GitHub Actions and Azure DevOps; failure annotations include the
action id and the redacted command.

## Non-goals

* A logging framework of our own — `logging` + a JSONL formatter is enough.
* Sending telemetry anywhere. Everything stays local to the run/pipeline artefact.
