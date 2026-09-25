# E11 — Tests, CI, docs and the private→public sync

**Goal:** make the framework changeable with confidence, and make the public repo a
usable product rather than a snapshot.

## Problem

There are no automated tests for the automation layer. The only CI validation is BPA on
semantic models. Every change is validated by running against a live tenant, which is
slow, non-deterministic and impossible for external contributors.

## Design

### 1. Test pyramid without a tenant

| Layer | What | How |
| --- | --- | --- |
| Recipe tests | loader, schema validation, merge/overlay, token substitution | pure unit tests + fixtures in `automation/tests/recipes/` |
| Golden plan tests | recipe → plan (ordered, redacted) | snapshot files; a plan diff is a reviewable artefact in PRs |
| Runner tests | argv construction, JSON parsing, error mapping, retries, LRO, redaction | fake `fab` binary on `PATH` (a script replaying canned stdout/exit codes) |
| Handler tests | Lakehouse wait-ready, Report→SemanticModel, outputs | recorded CLI/API fixtures |
| Contract tests (nightly, live) | a disposable `ci` environment: setup → assert → teardown | scheduled workflow, own capacity, tagged `Lifecycle:CI` |

The fake-`fab` harness is the key enabler: it makes ~90% of the engine testable in
seconds with no tenant, no secrets and no cost.

### 2. CI for the automation layer

* `ruff` + `mypy` (typed dataclasses in the recipe model make this cheap).
* `pytest` on every PR; golden plans regenerated with an explicit `--update-goldens`.
* Recipe validation for **every** recipe file in the repo (E01-S2) — catches typos in
  demo recipes before readers copy them.
* `pip-audit`, pinned dependency ranges in `requirements.txt` (currently unpinned:
  `fabric-cicd`, `ms-fabric-cli` etc. — a CLI/library change can silently break `main`;
  the local venv is on `fabric-cicd` 0.3.1 while 1.3.0 is published, so pipelines and
  local runs may already differ). Pin minor ranges and add a scheduled "dependency bump"
  PR job that runs the contract test. Tracked as E09-S7.

### 3. Documentation as product

* `documentation/reference/recipe.md` generated from the JSON Schema (E01-S5).
* One quickstart per audience: *"I want to try it"* (single workspace, one environment),
  *"I want the full blueprint"* (3×7), *"I want to adapt it"* (recipe reference + handler
  extension).
* Every spec in this folder gets a short "status: planned/in progress/shipped" header so
  the repo shows direction, and each blog post links to the spec that backs it.
* An `docs/adr/` folder for the decisions that will be argued about publicly
  (canonical model vs file translation, tags-as-properties, storage strategies).

### 4. Private → public sync

Today: work happens in the private repository, then is moved into the public one manually.
Risks: tenant IDs/group GUIDs/capacity names leaking, and divergence.

**Decision 7: the sync is automated — path allowlist + sanitise gate + one squashed
commit per release.** Two layers, in this order:

1. **Clean by construction.** Public recipes carry placeholders; real values live in an
   internal-only solution folder that is never on the export allowlist. This removes the
   manual wipe pass entirely rather than automating it.

   ```yaml
   # solutions/demo/platform.yml       -> exported as-is, nothing to wipe
   defaults:
     capacity: "{env:FABRIC_CAPACITY}"
     permissions:
       admin: [ { type: Group, id: "{env:PLATFORM_ADMIN_GROUP_ID}" } ]

   # solutions/internal/platform.yml -> not on the allowlist, never exported
   ```

2. **A gate that fails the sync.** `fabricops sanitise` over the *export tree*:
   * any GUID not in the known-placeholder allowlist;
   * named strings (tenant id, the Azure DevOps organisation name, group object ids, personal email domains);
   * secret shapes (`ghp_`, `github_pat_`, JWT-like, `client_secret=`, connection strings);
   * denylisted paths (`credentials.json`, `.DS_Store`, `.claude/`, `.venv/`).
   Accepted matches are recorded in a committed exceptions file with a reason.

* Export uses a **path allowlist**, not a denylist, so a new internal-only folder is
  excluded by default instead of leaking because nobody remembered it.
* `scripts/release_public.py --tag vX.Y.Z`: build export tree → sanitise → contract test
  → one squashed commit + generated changelog into the public repo. Internal history
  never reaches public, so an old tenant value in an early commit cannot leak.
* What automation cannot decide: whether a *new* file belongs in public, and whether
  something sensitive hides in prose. So the gate blocks on unknown paths and prints the
  export diff for a single human review.
* `.gitignore` additions: `.fabricops/`, `automation/credentials/credentials.json`,
  `**/.DS_Store` (currently committed in several folders).

## User stories

**E11-S1 — Fake `fab` test harness** — AC: engine unit tests run offline in CI; a canned
failure case proves E04-S3.
**E11-S2 — Golden plan snapshots** — AC: PRs show plan diffs for the demo recipes;
`--update-goldens` documented.
**E11-S3 — Nightly live contract test** — AC: disposable `ci` environment created and
torn down nightly; failure opens/annotates an issue.
**E11-S4 — Drift/prune** — AC: `fabricops plan` in CI on a schedule; `--prune` behind an
explicit confirm.
**E11-S5 — Alias coverage is permanent** — AC: one test per legacy key alias, plus the
items-keyed-by-type shape and `merge_type`; the alias table is generated into the recipe
reference. Decision 4 means there is **no** removal release, so these tests never expire.
**E11-S6 — Sanitisation gate and automated export** — AC: `fabricops sanitise` runs in CI
and blocks a public sync on any finding; export driven by a committed path allowlist;
`scripts/release_public.py` produces one squashed commit plus changelog;
`credentials.json` and `.DS_Store` removed from the repo and gitignored.

**E11-S8 — Placeholder demo recipes** — AC: the exported default/demo recipes contain no
tenant values (capacity, group ids, tenant id, repo owner are `{env:…}` tokens);
tenant-specific values live in an internal-only solution folder; a test asserts the demo
recipe validates and renders with placeholders unresolved.
**E11-S7 — Generated recipe reference** — AC: docs page generated from the schema and
published with the repo.
