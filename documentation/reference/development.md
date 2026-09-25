# Developing FabricOps

This is the contributor's view. For using FabricOps see
[getting-started.md](getting-started.md); for the recipe format see
[recipes.md](recipes.md).

## Layout

```
automation/src/fabricops/        the package
  errors.py                     typed errors + exit codes
  obs/redaction.py              Secret type + denylist patterns
  obs/logging.py                RunLog: console, JSONL trace, run id, levels
  fabric/paths.py               FabPath + `-P` parameter rendering
  fabric/cli.py                 FabricCli: the only place we invoke `fab`
  recipe/                       load, normalise, merge, validate, substitute
  engine/                       plan -> actions -> execute
    definitions.py              item definition import, hashed for idempotency
    drift.py                    a dry run, read as a drift report
    inventory.py                feature workspace inventory and TTL reaping
  release/                      the only place we import `fabric-cicd`
automation/tests/               offline test suite (stdlib unittest)
  fakefab/fab                   fake `fab` binary driven by a JSON script
  support.py                    FakeFab helper
```

## Running the tests

No tenant, no secrets, no network:

```bash
.venv/bin/python -m unittest discover -s automation/tests -t automation -v
```

The fake `fab` is a real executable that the runner shells out to, so tests cover argv
construction, output parsing, exit codes, retries and redaction exactly as production
does. Scripting it:

```python
fab = FakeFab()
fab.add(["exists", "Store"], stdout="* true")
fab.add_json(["api", "workspaces"], {"status_code": 200, "text": {"value": []}, "headers": {}})
fab.add_sequence(["mkdir"], [{"returncode": 1, "stderr": "429 TooManyRequests, Retry-After: 2"},
                             {"returncode": 0}])
cli = FabricCli(log, executable=fab.executable, env=fab.env)
```

`fab.calls` returns the argv of every invocation, so a test can assert that a display
name arrived as a single argument.

## Using the runner

```python
from fabricops.fabric.cli import FabricCli
from fabricops.fabric.paths import FabPath, params
from fabricops.obs.logging import RunLog, Level
from fabricops.obs.redaction import Secret

log = RunLog.from_env()                       # or RunLog(level=Level.DEBUG, trace_file=...)
cli = FabricCli(log, dry_run=False)

ws = FabPath.workspace("Sales - Store [dev]")
if not cli.exists(ws):
    cli.mkdir(ws, params=params({"capacityname": "Trial-01"}))

workspace_id = cli.get_value(ws, "id")        # raises instead of returning an error string
cli.set_property(ws, "sparkSettings.pool.starterPool.maxNodeCount", 1)
response = cli.api(f"workspaces/{workspace_id}/git/status")
```

Rules worth knowing:

* **Never build a command string.** Pass an argv list; `FabPath` handles naming.
* **Mark writes** with `mutating=True` (the convenience methods already do) so `--dry-run`
  can skip them.
* **Wrap secrets** in `Secret(...)` at the point they enter the process. Only `reveal()`
  puts a secret on a command line, and the logger renders `***` for the same element.
* A non-zero exit **raises** `FabricCliError`. Pass `check=False` only when a failure is a
  legitimate outcome.

## The `fabricops` command

```bash
export PYTHONPATH=automation/src

# what does this repo define?
.venv/bin/python -m fabricops solution list

# validate every solution and environment (use this in PR validation)
.venv/bin/python -m fabricops recipe validate --all

# see exactly what the engine will read, after overlays and tokens
.venv/bin/python -m fabricops recipe render --environment dev
.venv/bin/python -m fabricops recipe render --environment dev --format json
.venv/bin/python -m fabricops recipe render --kind feature --developer peer

# which files were resolved, and which legacy keys were folded
.venv/bin/python -m fabricops --log-level debug recipe render --environment tst > /dev/null
```

### Provisioning

Global flags (`--dry-run`, `--log-level`, `--trace-file`, `--solution`, `--resources`,
credentials) work on either side of the subcommand.

```bash
# what would happen, grouped by layer, without touching anything
.venv/bin/python -m fabricops plan --environment dev

# the literal execution order, numbered, with dependencies
.venv/bin/python -m fabricops plan --environment dev --sequence

# provision (or tear down) an environment
.venv/bin/python -m fabricops setup --environment dev
.venv/bin/python -m fabricops setup --environment dev --dry-run --log-level debug
.venv/bin/python -m fabricops setup --environment tst --layers Store,Model
.venv/bin/python -m fabricops setup --environment dev --action delete --confirm default/dev

# see the exact fab commands a run would issue, redacted
.venv/bin/python -m fabricops setup --environment dev --dry-run --log-level debug

# what did the last run produce? (ids, SQL endpoints, connection ids)
.venv/bin/python -m fabricops manifest show
```

### Drift

```bash
# probe the tenant; exit 3 when it differs from the recipe
.venv/bin/python -m fabricops plan --environment prd --check
```

### The one invariant

**An action that skips a read, or reports a status it did not verify, will lie about what a
real run does.** This has now been the cause of ten separate bugs: `SetProperties` assuming
it would write, `AssignRole` never reading the ACL, item connections blaming credentials for
a missing item, the git action reporting "already connected" without looking, `exists`
treating NotFound as an error, `acl_get` parsing a field listing as data, `_connection_state`
swallowing a 401 as "not connected" - and, while the guard for that last class was being
written, the guard itself, which shipped with `or ctx.dry_run` in it.

Reads are permitted in a dry run. That is the whole basis of `plan --check`. If an action
needs to know something to decide, it must go and find out, in both modes.

There is no separate checking code. A drift check *is* a dry run read differently -
`created` means missing, `updated` means differs - so it cannot disagree with what a real
run would do. Two consequences worth remembering when adding an action: it must read
before it decides, and it must report `existed` honestly in a dry run. `SetProperties` and
`AssignRole` both had to be fixed for exactly this.

### Releasing

```bash
# deploy items with fabric-cicd, layer by layer, in dependency order
.venv/bin/python -m fabricops release --environment tst --dry-run
.venv/bin/python -m fabricops release --environment tst
.venv/bin/python -m fabricops release --environment tst --layers store,model

# or through the pipeline entry point
python automation/scripts/fabric_release.py --environment tst --repo_path .
```

`fabricops.release.runner` is the only module that imports `fabric-cicd`, and it imports
it lazily, so `--dry-run` and the whole test suite run without it. Keep it that way: the
one adapter is what makes a future switch to config-based deployment contained.

The pipeline entry point keeps its old interface and now runs the same engine:

```bash
python automation/scripts/fabric_setup.py --environment dev            # as before
python automation/scripts/fabric_setup.py --environment dev --dry-run  # new
```

### Feature workspaces

```bash
# one workspace per participating layer for a branch, related back to dev
.venv/bin/python -m fabricops feature create --branch feature/prepare/add-orders --developer peer
.venv/bin/python -m fabricops feature update --branch feature/prepare/add-orders   # sync on commit
.venv/bin/python -m fabricops feature delete --branch feature/prepare/add-orders

# or through the pipeline entry point, unchanged
python automation/scripts/fabric_feature_maintainance.py --branch_name feature/prepare/add-orders
```

The middle segment of `feature/<segment>/<topic>` is resolved in this order:

1. **a group recipe** - if `feature.<segment>.yml` exists, it overlays the shared
   `feature.yml` and declares the layers that slice needs, so
   `feature/backend/add-orders` provisions whatever `feature.backend.yml` says;
2. **a layer name** - `feature/prepare/add-orders` provisions Prepare plus any layer
   marked `always`;
3. **neither** - every layer participates.

Overlay order is `feature.yml` → `feature.<group>.yml` → `feature.<developer>.yml`.
Teardown resolves the branch identically, so it deletes exactly the workspaces create
made. `create` converges everything, `update`
touches only the workspace and its git sync, and `delete` removes the workspaces.

### Feature inventory and cleanup

```bash
.venv/bin/python -m fabricops feature list
.venv/bin/python -m fabricops feature reap --ttl-days 14           # report
.venv/bin/python -m fabricops feature reap --ttl-days 14 --apply   # delete
```

Feature workspaces are found by their description stamp, not by their name and not by
tags - tags need a tenant admin to create, and a recipe may declare none. `inventory.py`
holds the stamp format, the decision rules and the GitHub branch probe. When extending it,
keep the bias: anything the reaper cannot determine must mean "keep".

### Tags

Tag *creation* needs a Fabric administrator; *applying* does not. So sync once with an
admin, commit the registry, and let every deployment resolve names from the committed
file (see `documentation/specs/E05`).

```bash
# with an admin identity, occasionally
.venv/bin/python -m fabricops tags sync --environment dev

# in CI, or before a run: are all declared tags registered?
.venv/bin/python -m fabricops tags list --environment dev
```

Managed keys are fixed: `ManagedBy`, `Solution`, `Env`, `Layer`, `Lifecycle`, `Owner`,
`Branch`, `Retain`. Anything else is treated as a user tag - never applied, never removed.

`recipe render` is also the way to migrate a recipe: render your existing
`infrastructure.json` to YAML and commit the result as `platform.yml` - both are read by
the same loader, so you can move one solution at a time.

## Before a public sync

```bash
# what would be published, and is anything in it that must not be?
.venv/bin/python -m fabricops sanitise --list-files
.venv/bin/python -m fabricops sanitise                 # exit 1 on findings
.venv/bin/python -m fabricops sanitise --strict         # also flag unlisted GUIDs
.venv/bin/python -m fabricops export --to ../FabricOps  # refuses to run with findings
```

`automation/resources/export.yml` is the policy: `include` is an allowlist (a new
internal folder is excluded by default), `deny_paths` and `deny_values` are checked
everywhere, and `secret_exceptions` relaxes only the pattern check where secret-shaped
strings are expected (test fixtures, prose about secrets). A single line can opt out with
a `# sanitise: allow` comment.

The policy file itself is not exported - it carries the values being scanned for.

## Logging

| Control | Env var | Effect |
| --- | --- | --- |
| `--log-level off\|error\|warn\|info\|debug\|trace` | `FABOPS_LOG_LEVEL` | `info` keeps today's step output; `debug` adds one redacted line per `fab` call |
| `--trace-file <path>` | `FABOPS_TRACE_FILE` | JSONL record per invocation: run id, command, exit code, duration, output |
| `--no-redact` | `FABOPS_ALLOW_UNREDACTED=1` | Disables masking (prints a warning; never use in CI) |
| — | — | At `trace`, `cli.enable_cli_debug()` turns on the Fabric CLI's own HTTP log |

Every record carries the same `run_id`, so a pipeline log and a trace file can be
correlated.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success, or a plan was produced with `--dry-run` |
| 1 | one or more actions failed |
| 2 | recipe/config/resolution error — nothing executed |
| 3 | drift detected (`plan` only) |
| 4 | authentication or permission error |

## Local environment

The checked-in `.venv` reports Python 3.13 through `.venv/bin/python`, but it was created
with Homebrew `python@3.12`, which is no longer on this machine. Consequences: `pyvenv.cfg`
still claims 3.12, `.venv/bin/pip` has a dead shebang, and there are two `site-packages`
trees. Use `.venv/bin/python -m pip`, and recreate the venv on 3.13 when convenient.

`pytest`, `jsonschema` and `ruff` are **not** installed; the suite deliberately runs on
stdlib `unittest` so it works as-is.

Dependencies are pinned in `automation/resources/requirements.txt` (E09-S7). Bump them on
purpose, with the test suite as the gate. The scheduled bump job is still outstanding.
