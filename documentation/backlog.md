# FabricOps backlog — IaC overhaul + blog series sync

Status: **Draft for review** · Created 2026-08-27 · Specs: [documentation/specs](specs/README.md)

Legend — **P1** must-have for the overhaul · **P2** high value, next · **P3** later/opportunistic.
Size — **S** ≤ half a day · **M** 1–3 days · **L** ≥ 1 week (as a solo side-project pace).

---

## 1. Epics

| Epic | Title | Why now | P |
| --- | --- | --- | --- |
| [E01](specs/E01-recipe-model-and-formats.md) | Recipe model, JSON + YAML, schema, merge | Definition layer everything else reads | P1 |
| [E02](specs/E02-solution-resolution.md) | Multi-solution + per-developer recipes | Unblocks "one repo, many solutions" | P1 |
| [E03](specs/E03-generic-provisioning-engine.md) | Generic provisioning engine + handlers + property/definition passthrough | Removes hardcoded item types; the core ask | P1 |
| [E04](specs/E04-cli-runner-and-observability.md) | CLI runner, logging, redaction, dry-run, exit codes | Makes the engine debuggable and honest | P1 |
| [E05](specs/E05-tags-extended-properties.md) | Tags as extended properties | Turns naming conventions into queryable metadata | P2 |
| [E06](specs/E06-variable-libraries.md) | Variable libraries where they win | Run-time config done natively | P2 |
| [E07](specs/E07-feature-dev-storage.md) | Feature-development storage strategies | The biggest unsolved problem in the flow | P2 |
| [E08](specs/E08-feature-lifecycle.md) | Feature workspace lifecycle hardening | Relations, TTL, ownership, reaping | P2 |
| [E09](specs/E09-release-and-fabric-cicd.md) | Release path aligned to `fabric-cicd` (Python API; **no** config file) | Deletes custom code the library now covers | P2 |
| [E10](specs/E10-repo-blueprint.md) | Repo blueprint, workspace folders, providers | Directly feeds blog post #3 | P1 |
| [E11](specs/E11-quality-and-dx.md) | Tests, CI, docs, private→public sync | Makes the rest safe to change | P1 |

---

## 2. Phased plan

### Phase 0 — Foundations (2–3 weeks) → tag `v2.0-alpha`
Everything here is testable offline and unblocks the rest.

**Progress (2026-08-27):** Phases 0 and 1 are largely implemented, with **196 offline
tests** passing (`automation/src/fabricops`, `automation/tests`).

Shipped: E11-S1 (fake `fab` harness), E04-S1/S2/S3/S4/S5 (runner, logging, redaction,
exit codes, dry-run), E01-S1/S2/S3/S4 and E02-S1/S2/S3/S4 (recipe layer and resolution),
E11-S5 (permanent alias coverage), E03-S1/S2/S3/S6/S7/S9 (generic items, planner,
properties, ordering, manifest, teardown), E03-S5 (connections beyond
Lakehouse/SQLDatabase), git integration shared by both flows, E05-S1/S2/S3 (tag
registry, admin sync, apply), and `fabric_setup.py` as a thin entry point over the
engine with the legacy implementation kept under `automation/scripts/legacy/`.

Outstanding in these phases: E03-S4 (item definition passthrough), E03-S8 (drift),
E11-S6 (sanitise gate), E09-S7 (dependency pinning), E10-S2 (folder support is planned
but untested against a live tenant).

Bugs the tests caught along the way, all fixed at the source: secrets stringified into
`-P` parameters (the CLI would have received `***`), short secrets scrubbing unrelated
output, a readiness poll that could spin, and git integration reporting `updated` when
nothing changed.

| Story | Size | Notes |
| --- | --- | --- |
| E11-S1 Fake `fab` test harness | M | do this **first**; it pays for itself immediately |
| E04-S1/S2 Runner + logging + redaction (off by default) | M | argv lists, JSON output, typed errors |
| E04-S3 Fail on failure + exit codes | S | highest-value single fix in the repo |
| E04-S4 Remove hand-built command strings | M | kills the `\\/` escaping hacks |
| E01-S1/S2 Loader (JSON+YAML) + JSON Schema validation | M | schema covers today's keys first |
| E01-S3/S4 Render/merge + legacy `merge_type` compatibility | M | golden-file test against current recipes |
| E10-S3 Repo restructure to `automation/src/fabricops` | M | thin entry points kept for pipelines |
| E11-S6 Sanitisation gate + `.gitignore`/`credentials.json`/`.DS_Store` cleanup | S | do before any public sync |
| E09-S7 Pin `fabric-cicd` / `ms-fabric-cli` to tested ranges | S | local is 0.3.1, published is 1.3.0, pipelines are unpinned |
| E01-S4 + E11-S5 Alias coverage tests (legacy keys, `merge_type`, items-by-type) | S | decision 4: permanent, so these never expire |

### Phase 1 — Generic engine (3–4 weeks) → `v2.0`
| Story | Size | Notes |
| --- | --- | --- |
| E03-S1 Any item type by name/type | M | registry from the CLI, no local type list |
| E03-S3 Generic `properties:` (generalises `spark_settings`) | M | read-compare-write idempotency |
| E03-S6 Dependency-ordered planner | M | replaces the hand-rolled second passes |
| E03-S7 Run manifest | S | outputs become the contract |
| E04-S5 Dry-run / plan output | S | falls out of the planner |
| E02-S1/S2 `--solution` resolution + fallback | M | the flat naming you asked for, plus folder layout |
| E02-S3 Per-developer feature overlay | S | |
| E03-S5 Connections beyond Lakehouse/SQLDatabase | M | Warehouse/Eventhouse via handler outputs |
| E03-S9 Symmetric teardown from the same plan | M | |
| E11-S2 Golden plan snapshots in PRs | S | |
| E10-S1 Repo blueprint doc | S | **blog post #3 asset** |
| E03-S2 3 env × 7 layer sample recipe | M | the demo that proves the ask |

### Phase 2 — Configuration & metadata (2–3 weeks) → `v2.1`
| Story | Size | Notes |
| --- | --- | --- |
| E05-S1/S2/S3 Tag registry, admin sync, apply | M | design around 10-tag budget + 25 rpm |
| E05-S4/S5 Feature ownership tags + `Retain:true` | S | |
| E06-S1/S2 Variable libraries declared + generated per workspace | L | ItemReference values from the manifest |
| E06-S3 Value-set activation (incl. feature workspaces) | S | |
| E06-S5 Configuration decision guide | S | **blog post #6 asset** |
| E03-S4 Item definition passthrough (`definition.from` / `parts`) | M | needed by generated VLs |
| E09-S1 Deployment policy in the recipe (`deploy:` block per layer) | M | replaces what a config file would hold; no per-layer config files |
| E09-S2 Adopt `semantic_model_binding`, delete custom binding | M | removes ~60 lines + a bespoke YAML |
| E09-S3 Parameter overlay via `extend` (`--extend-parameters`) | M | committed `parameter.yml` never rewritten again |
| E09-S4 Dynamic references first, then `$ENV:`, then generated | S | |
| E09-S6 Release exit codes via `DeploymentResult` + library file log | S | |

### Phase 3 — Feature flow & storage (3–4 weeks) → `v2.2`
| Story | Size | Notes |
| --- | --- | --- |
| E08-S1 Workspace relation (branched workspace) | S | preview API, big UX payoff |
| E08-S2/S4/S5/S6 Inventory, layer selection, dev-as-admin, shared git module | M | |
| E08-S3 TTL reap workflow | M | scheduled, dry-run first |
| E07-S1 Schema-enabled lakehouses (prerequisite; do it while they're empty) | S | blocks everything else in E07 |
| E07-S2 `storage` recipe block (`feature_schema.enabled` off by default) | S | defaults reproduce today's behaviour exactly |
| E07-S3 Resolver contract documented (implementation lives with the solution) | M | read overlay, write without fallback, no-op elsewhere |
| E07-S4 `feature/store/<topic>` flow documented | S | already supported by the layer-from-branch filter |
| E07-S5 Teardown + orphan schema report | M | the only destructive step; dry-run by default |
| E07-S6 Coordination policy when the flag is off | S | policy, not folklore |
| E07-S7 Storage blog assets (three spinoff posts) | S | **spinoff series, not Episode 5** |
| E08-S7 Storage lifecycle tied to feature teardown | S | |

### Phase 4 — Hardening & polish (ongoing) → `v2.3+`
| Story | Size | Notes |
| --- | --- | --- |
| E03-S8 / E11-S4 `plan` drift report, then `--prune` | M | nightly |
| E11-S3 Nightly live contract test (`ci` environment) | M | |
| E11-S7 / E01-S5 Generated recipe reference docs | S | |
| E05-S6 Tag reconciliation report | S | |
| E02-S4/S5 Solution discovery + collision guardrail | S | |
| E10-S2 Workspace folder support | M | |
| E10-S4/S5 Provider validation + credentials out of repo | M | |
| E04-S6/S7 Rate-limit/LRO resilience + pipeline-native logs | M | |
| E11-S8 Placeholder demo recipes (no tenant values in exported files) | S | pairs with E11-S6 |

### Long term / opportunistic (P3)
| Item | Trigger |
| --- | --- |
| Parallel execution (`--parallel N`) across independent branches of the plan | after golden-plan + contract tests are stable |
| Domain assignment + domain-scoped tags as part of the recipe | when a demo needs >1 domain |
| Capacity-aware provisioning (assign/unassign, pause/resume for cost) | cost blog post |
| Bulk Import/Export item definitions path (unsupported git providers, cross-tenant clone) | first request for GitLab/Bitbucket |
| OneLake security roles in the recipe (per-developer read scopes) | when E07 option 3/4 goes wide |
| Deployment-pipeline-based promotion as an alternative to `fabric-cicd` | if a reader/customer needs it |
| "Golden subset" data lakehouse for deterministic PR validation | after E07 lands |
| `fabricops init` scaffolding (new repo from blueprint) | when the public repo gets external users |
| E09-S8 `release export-config` (emit `fabric-cicd` config files for `fab deploy` outside FabricOps) | handover/debugging, or if config-based deploy becomes the first-class path |
| Shortcut-target-as-variable storage strategy | when the API ships (tracked in research §7) |
| Cost/usage reporting joined on tags (capacity metrics app) | governance post |

---

## 3. Blog series sync

Two posts are published. The framework work below is sequenced so each post ships with
working code in the public repo.

| # | Working title | Backs onto | Repo state needed | Assets to produce |
| --- | --- | --- | --- | --- |
| 3 | **Structuring the repo: the blueprint on disk** — folders, item layout, one-workspace-one-directory, workspace folders vs layers, git providers | E10 (+ E01 preview) | Phase 0 merged; blueprint doc | layout diagram, naming table, folders/git-sync caveat box |
| 4 | **Automating the platform: recipes, provisioning, git integration** — declarative recipes (JSON *or* YAML), generic item provisioning, multiple solutions | E01, E02, E03 (+ E04 sidebar) | Phase 1 tagged `v2.0` | 3 env × 7 layer sample recipe, `--dry-run` plan screenshot, before/after code diff |
| 5 | **Feature development at scale** — branch → workspaces, branched-workspace relations, ownership, TTL reaping | E08 | Phase 3 (E08 subset) | flow diagram, reap report screenshot |
| 6 | **Where does configuration live?** — recipe vs `parameter.yml` vs variable library vs tags | E06, E05 | Phase 2 tagged `v2.1` | decision table (E06-S5), VL before/after, tag registry example |
| 7 | **The storage problem in feature development** — shared dev vs schema-per-dev vs shortcuts vs shallow clone, and what Databricks does differently | E07 | Phase 3 tagged `v2.2` | option/cost/isolation matrix, clone caveat, indirection code snippet |
| 8 | **Release and promotion** — `fabric-cicd`, generated deploy config, dynamic references, multi-workspace ordering | E09 | Phase 2 | pipeline diagram, parameter file before/after |
| 9 | **Testing your automation** — fake CLI harness, golden plans, nightly contract environment, honest exit codes | E04, E11 | Phase 0/4 | test pyramid diagram, "the pipeline was green and nothing happened" war story |
| 10 | **Governance at scale** — tags, domains, inventory, drift, cost attribution | E05, E03-S8 | Phase 4 | inventory/drift report, scanner-API join example |

Recurring per post: link to the backing spec, a runnable command, and one honest
limitation (the research gaps table is a ready-made source).

---

## 4. Decisions made (2026-08-27)

| # | Decision | Rationale | Affects |
| --- | --- | --- | --- |
| 1 | **Feature storage:** shared per environment. Per-feature schema isolation is an opt-in *solution* convention (not architecture), off by default. Shortcut isolation dropped — writes pass through to the target; shallow clone possible but deferred | Shared storage is what both published enterprise patterns do; the isolation options cost more than they return until several people write tables concurrently | E07 |
| 2 | **Do not adopt the `fabric-cicd` config file.** Deployment policy moves into the recipe per layer; `parameter.yml` stays committed with a generated `extend:` overlay (`--extend-parameters`, default true) | The config allows one workspace + one `repository_directory` per environment, so an N-layer solution needs N config files restating what the recipe already defines — and it grants no capability the Python API lacks | E09, E06 |
| 3 | **Recipe layout:** support all three resolution orders; docs, demo and blog #4 lead with `resources/solutions/<name>/` (holding that solution's platform, feature, parameter and tag files); `infrastructure*.json` stays the no-solution fallback | New setups get a clear home per solution; existing clones need no edits | E02, E10 |
| 4 | **Key renames with permanent aliases.** Legacy spellings, `merge_type`, and items-keyed-by-type are supported indefinitely; no removal release | No breaking changes, ever, for recipes already in the wild | E01, E11 |
| 5 | **`apiVersion` / `kind` optional**, absent means `fabricops/v1`; used in docs and the demo | Makes a future incompatible schema change cheap without forcing a migration now | E01 |
| 6 | **Tags use `Key:Value`** with a fixed managed-key list: `ManagedBy`, `Solution`, `Env`, `Layer`, `Lifecycle`, `Owner`, `Branch`, `Retain`. Unknown keys are user tags — never applied, never removed | Automation can parse tags back into properties for inventory, reaping and teardown protection | E05, E08 |
| 7 | **Private → public sync automated:** placeholder demo recipes + committed path allowlist + `fabricops sanitise` gate + one squashed commit per release via `scripts/release_public.py` | Replaces the manual copy-and-wipe pass; internal history never reaches public | E11 |

### Follow-ups these created

* Recipe gains a `deploy:` block (E09-S1) — deployment policy has a home now.
* `parameter.yml` overlay lands in `solutions/<name>/generated/dynamic.parameter.yml`,
  gitignored; the committed file only gains an `extend:` entry.
* Keep the release module boundary thin: Microsoft ships `fab deploy --config`, so
  config-based deployment may become the first-class path later (E09 §1, E09-S8).
* Open question still worth a decision later: whether to add a shared "golden subset"
  lakehouse as the deterministic PR-validation data source (E07 discussion §3).
