# E02 — Multi-solution and per-developer recipe resolution

**Goal:** one repo can describe many solutions, and a feature recipe can be
personalised per developer, without renaming files or editing scripts.

## Problem

Paths are hardcoded:

```python
# fabric_setup.py / fabric_release.py
main_json = misc.load_json(f'../resources/environments/infrastructure.json')
env_json  = misc.load_json(f'../resources/environments/infrastructure.{environment}.json')
# fabric_feature_maintainance.py
feature_json = misc.load_json(f'../resources/environments/feature.json')
```

So: one platform recipe per repo, one feature recipe for all developers, and the file
name is the contract.

## Design

Every entry point gains `--solution <name>` (env var `FABOPS_SOLUTION`, config default
`fabricops.yml`). Resolution is a documented, ordered search — **first match wins**,
and the resolved paths are printed in the run header.

### Search order for `kind: Platform`

For `--solution my-data-platform --environment dev`:

| Order | Base recipe | Environment overlay |
| --- | --- | --- |
| 1 | `resources/solutions/my-data-platform/platform.{yml,yaml,json}` | `.../platform.dev.{yml,yaml,json}` |
| 2 | `resources/environments/my-data-platform.{yml,yaml,json}` | `resources/environments/my-data-platform.dev.{…}` |
| 3 *(fallback)* | `resources/environments/infrastructure.{yml,yaml,json}` | `resources/environments/infrastructure.dev.{…}` |
| 4 *(no `--solution` only)* | the single entry under `resources/solutions/` when exactly one exists; the same last resort applies to `feature.*` | its `platform.dev.{…}` |

**Decision 3:** order 1 is what the docs, the demo and blog post #4 lead with — a
solution folder also holds that solution's `parameter.yml` and `tags.yml`, so everything
about one solution sits together. Order 2 is the flat naming for single-solution repos
(`my-data-platform.json`, `my-data-platform.dev.json`), kept documented. Order 3 is
today's behaviour, so an existing clone with only `infrastructure*.json` runs unchanged
and needs no new folder.

Order 4 (added 2026-09-25) exists for the public repository: it ships `solutions/demo/` and
nothing else, and a fresh clone must survive `plan --environment dev` without every command
naming the solution. It applies only when nothing was named and neither a `default` folder
nor the legacy file exists. Two or more solutions with none named is an error that lists
them: guessing would be worse than stopping. The `fabricops.yml` `default_solution` below
is still unimplemented; order 4 covers the common case without a new file.

```
automation/resources/solutions/spaceparts/
  platform.yml  platform.dev.yml  platform.tst.yml  platform.prd.yml
  feature.yml   feature.peer.yml
  parameter.yml  generated/dynamic.parameter.yml   # E09
  tags.yml                                          # E05
```

### Search order for `kind: Feature`

For `--solution my-data-platform` on branch `feature/peer/add-orders`:

| Order | File |
| --- | --- |
| 1 | `.../my-data-platform/feature.{developer}.{yml,json}` |
| 2 | `.../my-data-platform/feature.{yml,json}` |
| 3 | `resources/environments/feature.{developer}.{yml,json}` |
| 4 | `resources/environments/feature.{yml,json}` |

`{developer}` is resolved, in order, from: `--developer`, `FABOPS_DEVELOPER`,
`GITHUB_ACTOR` / `BUILD_REQUESTEDFOREMAIL` local part, `git config user.email` local
part — normalised to `[a-z0-9-]` (lowercased, `.`/`_`/`@` → `-`). The resolved value is
also available as the `{developer}` token (E01) and as a tag value (E05).

A `feature.<developer>.yml` overlays the shared `feature.yml` rather than replacing it
(same merge rules as E01), so a developer can override just one layer's spark settings
or storage strategy (E07).

### Repo-level defaults

Optional `fabricops.yml` at repo root:

```yaml
apiVersion: fabricops/v1
kind: Config
default_solution: my-data-platform
solutions:
  my-data-platform: { path: automation/resources/solutions/my-data-platform }
  spaceparts:       { path: automation/resources/solutions/spaceparts }
logging: { level: info, redact: true }
```

### Multi-solution in one pipeline run

`--solution` accepts a comma-separated list, and `fabricops solution list` enumerates
what the repo contains. Deployments stay per-solution (separate plan, separate log,
separate exit code) so one failing solution doesn't abort the others unless
`--fail-fast`.

## User stories

**E02-S1 — Named solutions**
*As a platform engineer I run `fabric_setup.py --solution my-data-platform
--environment dev` and the matching recipe files are used.*
AC: resolution table implemented; run header prints the resolved base + overlay paths;
missing solution exits 2 with the searched paths listed.

**E02-S2 — Fallback preserved**
AC: with no `--solution`, `infrastructure.json`/`infrastructure.<env>.json` are used
exactly as today.

**E02-S3 — Per-developer feature recipe**
*As a developer I can commit `feature.peer.yml` and my feature workspaces get my own
settings (small starter pool, my storage strategy) without touching the team file.*
AC: overlay applied over `feature.yml`; `--dry-run` shows the effective values and
their source file.

**E02-S4 — Solution discovery**
AC: `fabricops solution list` prints solution name, recipe paths, environments found,
and validation status for each.

**E02-S5 — Guardrail against cross-solution collisions**
*As a platform owner I want two solutions in one tenant to never fight over the same
workspace name.*
AC: `fabricops recipe validate --all` fails when two solutions resolve to the same
workspace display name for the same environment.

## Notes / risks

* The `{environment}` axis stays a file overlay (`platform.dev.yml`). An alternative —
  one file with an `environments:` map — was considered and rejected: per-env files
  keep PR diffs and per-env approvals clean, and match the current model.
* Solution name is also a natural tag value (`Solution:my-data-platform`, E05) and a
  natural prefix for connection names; the normalizer exposes it as `{solution}`.
