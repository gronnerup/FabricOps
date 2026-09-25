# E09 — Release path and `fabric-cicd` alignment

**Goal:** use everything `fabric-cicd` gives us through its Python API, keep the release
driven by the recipe, and delete the code the library has since made redundant.

**Decision (27 Aug 2026): FabricOps does not adopt the `config.yml` deployment file.**
See §1 for the reasoning. Deployment policy lives in the recipe.

## Problem

`fabric_release.py` mixes three concerns: resolving workspaces from the recipe, calling
`publish_all_items`/`unpublish_all_orphan_items` per layer, and a hand-written
semantic-model-to-SQL-endpoint binding step driven by
`resources/parameters/sqlendpoint_model_binding.yml`.

Meanwhile the library has moved on considerably. Verified by reading the source of
**`fabric-cicd` 1.3.0**, which `requirements.txt` now pins (it previously pinned nothing,
and reported 0.3.1 while running 1.x code):

* `parameter.yml` supports `find_replace` (with `is_regex`, `ignore_case` and
  `item_type` / `item_name` / `file_path` filters), `key_value_replace` (JSONPath),
  `spark_pool`, **`semantic_model_binding`**, dynamic `$workspace…`/`$items…`/
  `$sqlendpoint`/`$sqlendpointid`/`$queryserviceuri`, `$ENV:` values, the `_ALL_`
  environment key, and **`extend`** for template parameter files.
* Publish/unpublish filters are plain Python arguments (§2).
* `configure_external_file_logging` / `disable_file_logging` write the library's own log
  to a file; `append_feature_flag` sets flags; `constants` module attributes are
  overridable. Note `change_log_level` only understands `DEBUG` — there is no way to make
  the library quieter, so FabricOps calls it only when itself running verbose.
* `DeploymentResult` / `DeploymentStatus` give a structured outcome — but **only from
  `deploy_with_config`**, the config-file entry point rejected in §1. `publish_all_items`
  raises on failure and returns collected responses. *Corrected during implementation:*
  FabricOps assembles the equivalent itself (`release.ReleaseResult`), same three fields,
  so §6 still holds and the boundary in §1 stays intact.
* Variable libraries are deployed first, and the value set matching the environment name
  is activated.

## 1. Why not the config file

`fabric-cicd` (and now `fab deploy --config … --target_env …`, which wraps it) accepts a
`config.yml` with `core` / `publish` / `unpublish` / `features` / `constants` sections and
environment-mapped values. It was evaluated and rejected for FabricOps:

* `core.repository_directory` and `core.workspace`/`workspace_id` are **one directory and
  one workspace per environment**. A layered solution therefore needs **one config file
  per layer** — 7 files for a 7-layer solution, 5 for a 5-layer one — each restating the
  layer list and workspace naming that the recipe already defines. That is the
  "hardcoded to a specific shape" problem this overhaul exists to remove.
* The config grants **no capability** that the Python API lacks (§2), so the only thing
  gained is a file format.
* Unlike `parameter.yml`, the config has **no `extend`/split mechanism** and no
  generation story; its only runtime seam is `config_override` (Python) or `-P`
  (`fab deploy`), and required fields must already exist in the base file to be
  overridden.

Consequences recorded as risks, not blockers: Microsoft is clearly investing in
config-based deployment, so the release module keeps a **thin boundary** (one adapter
function) to make a future switch contained. An optional
`fabricops release export-config` that emits config files for someone who wants to run
`fab deploy` without FabricOps is a P3 nice-to-have (handover, debugging).

## 2. Deployment policy lives in the recipe

Every knob the config file exposes maps to an argument FabricOps already has in hand:

| Recipe (per layer, env-mappable) | `fabric-cicd` call |
| --- | --- |
| `deploy.item_types_in_scope` | `FabricWorkspace(item_type_in_scope=…)` |
| `deploy.exclude_regex` | `publish_all_items(item_name_exclude_regex=…)` |
| `deploy.items_to_include` | `publish_all_items(items_to_include=…)` (flag `enable_items_to_include`) |
| `deploy.folder_exclude_regex` / `deploy.folder_path_to_include` | `publish_all_items(folder_path_exclude_regex=… / folder_path_to_include=…)` (flags `enable_exclude_folder` / `enable_include_folder`) |
| `deploy.shortcut_exclude_regex` | `publish_all_items(shortcut_exclude_regex=…)` (flag `enable_shortcut_publish`) |
| `deploy.unpublish.skip` | skip the `unpublish_all_orphan_items` call |
| `deploy.unpublish.exclude_regex` / `items_to_include` | `unpublish_all_orphan_items(item_name_exclude_regex=… / items_to_include=…)` |
| `deploy.features` | `append_feature_flag(…)` |
| `deploy.constants` | `fabric_cicd.constants` attribute overrides |

```yaml
layers:
  Store:
    git: { directory: solution/store }
    deploy:
      item_types_in_scope: [Lakehouse, Notebook, SemanticModel]
      unpublish:
        skip: { prd: true }            # env-mappable like everything else
```

Safety note to document: unpublishing data-bearing items is opt-in behind
`enable_lakehouse_unpublish`, `enable_warehouse_unpublish`,
`enable_sqldatabase_unpublish`, `enable_eventhouse_unpublish` and
`enable_kqldatabase_unpublish`. FabricOps never enables these implicitly.

## 3. Parameter file: committed base + generated overlay

`parameter.yml` stays **hand-authored and committed**, with its hardcoded entries intact.
Generated, run-specific entries go into a separate overlay file referenced from
`extend:` — so nothing machine-written is ever merged into the file you edit (today
`utils_build_parameter_file_dynamic.py` upserts straight into it).

```yaml
# automation/resources/solutions/<solution>/parameter.yml   (committed, yours)
extend:
  - "./generated/dynamic.parameter.yml"     # rendered per run; path relative to this file
find_replace:
  - find_value: "https://dev-storage.dfs.core.windows.net/landing/"
    replace_value: { tst: "…", prd: "…" }
```

Controlled by `--extend-parameters true|false` (default **true**; `false` skips rendering
and ignores the overlay). The overlay is written **even when empty** - a committed
`extend:` pointing at a missing file fails the deployment, and "nothing to generate this
run" is a normal state.

Corrected during implementation: the main parameter file does **not** have to sit in the
root of `repository_directory`. `FabricWorkspace(parameter_file_path=...)` takes any path,
so all layers share one committed `automation/resources/parameters/parameter.yml`. This is
the concrete difference from the config file, which really is one per layer (§1).

Three ways to express an environment-specific value, in order of preference:
1. **dynamic notation** — `$workspace.<ws>.$items.<Type>.<name>.$id` / `$sqlendpoint` /
   `$sqlendpointid`: survives a dev workspace re-create, needs no generation;
2. **`$ENV:` values** (flag `enable_environment_variable_replacement`): pipeline variables;
3. **generated overlay entries**: only for what neither of the above can express.

## 4. Native semantic model binding

Adopt `semantic_model_binding` in the parameter file (`default.connection_id` plus
per-model entries, `_ALL_` supported, list of connection ids supported for multi-source
models) and delete the custom binding code in `fabric_release.py`. A shim translates the
existing `sqlendpoint_model_binding.yml` into generated parameter entries for one
release, with a deprecation warning.

## 5. Multi-layer release, made explicit

Layer order comes from the recipe's dependency graph (E03). Cross-layer id mapping — the
current trick of accumulating `environment_parameter` and appending
`logical_id → guid` between layers — is documented and unit-tested, with an option to run
layers in one accumulated session or independently.

## 6. Release logging and exit codes

`DeploymentResult` / `DeploymentStatus` drive the release exit code (E04's table), the
library's `change_log_level` is wired to `--log-level`, and
`configure_external_file_logging` points its log at the run directory so it lands in the
same place as the FabricOps trace.

## 7. Dependency pinning

`requirements.txt` currently pins nothing, while the local venv has `fabric-cicd` 0.3.1
and the published docs are 1.3.0 — pipelines may already behave differently from local
runs. Pin a tested minor range for `fabric-cicd` and `ms-fabric-cli`, and add a scheduled
bump job that runs the contract test (E11).

## User stories

**E09-S1 — Deployment policy in the recipe** — AC: `deploy:` block per layer, env-mappable,
mapped to the publish/unpublish arguments and feature flags in the table above; no config
file required anywhere.
**E09-S2 — Native semantic model binding** — AC: bindings expressed in the recipe are
emitted as `semantic_model_binding` parameters; custom binding code removed; legacy YAML
shim plus deprecation warning.
**E09-S3 — Parameter overlay via `extend`** — AC: committed `parameter.yml` never rewritten;
overlay rendered to `generated/dynamic.parameter.yml` and referenced from `extend:`;
`--extend-parameters false` disables it; overlay path is gitignored (E11 decides commit vs
ignore per artefact).
**E09-S4 — Dynamic references first** — AC: generated entries use dynamic notation where the
item type supports it; re-creating the dev workspace does not require regeneration.
**E09-S5 — Deterministic layer order** — AC: release order derives from the recipe graph;
test covers a Report→SemanticModel cross-layer case.
**E09-S6 — Release exit codes and logs** — AC: non-zero exit on any item failure via
`DeploymentResult`; library log file written into the run directory; pipeline annotations
on failure.
**E09-S7 — Pin the dependencies** — AC: `fabric-cicd` and `ms-fabric-cli` pinned to tested
ranges; scheduled bump job runs the contract test.
*Done — pinned to exact versions (`fabric-cicd==1.3.0`, `ms-fabric-cli==1.5.0`) rather than
ranges; the local venv reported 0.3.1 while running 1.x code, so a range would have left
the same ambiguity. The scheduled bump job is still outstanding.*
**E09-S8 (P3) — `release export-config`** — AC: emits per-layer config files for use with
`fab deploy` outside FabricOps; explicitly not part of the release path.


## 9. Why committed artefacts carry dev's ids, not placeholders

Discovered on the first live run, and it invalidates the placeholder scheme §3 implied.

**Dev workspaces are git-connected.** Whatever is committed is synced into them verbatim -
no parameter file runs, because fabric-cicd is not in the dev loop at all. So a placeholder
id in a committed artefact reaches dev unrewritten.

Fabric will not accept that for anything it treats as a real dependency. A report's
`pbiModelDatabaseName` and a pipeline's `notebookId`/`workspaceId` are resolved at sync
time, and an unresolvable one fails the whole sync:

```
Present     : DiscoverDependenciesFailed - Dependency discovery failed for one or more items
Orchestrate : MissingDependency - Dependencies can't be found for one or more items
```

A semantic model's M expression is *not* such a dependency - it is opaque text - which is
why the Model layer synced happily with two nonsense GUIDs in it and would only have failed
later, at query time. That difference is worth knowing: sync success does not mean the ids
are right.

Nor can logicalIds help here. Git integration resolves a logicalId **within a workspace**;
the pipeline is in Orchestrate and its notebooks are in Ingest and Prepare, so there is
nothing for it to resolve against. `_replace_logical_ids` in fabric-cicd has the same
boundary, which is why the release path accumulates the mapping across layers (§5).

### A cost, and where it can be avoided

Committed ids have a sharp edge, hit twice on the first day: **recreating an item changes
its id, and every committed reference to it breaks.** Deleting an item is not exotic - it
is the *only* repair for a deployed item whose stored state is broken, because a later good
commit cannot fix it (that is the `The Fabric artifact ... is not found` case above). So
the recreate path is one you will use.

Where a reference can be made by **name**, it should be. Names survive recreation:

| Reference | By | Why |
| --- | --- | --- |
| Report → semantic model | **name** | `definition.pbir` can carry a connection string naming the workspace and the model, so only the environment suffix needs swapping. |
| Direct Lake expression | id | `AzureStorage.DataLake` takes a workspace id and an item id. No name form. |
| Pipeline → notebook | id | `notebookId` and `workspaceId` are ids. No name form. |

**Settled on 7 Sep 2026, and not the way the table above implied.** Two findings:

1. The id is **required**. A connection string naming only the workspace and the model is
   rejected: *"The semantic model identifier is invalid. Ensure that a valid GUID is
   provided either in 'byConnection.pbiModelDatabaseName' or as the 'semanticModelId'
   parameter."* So the report keeps an id, and `references sync` exists to maintain it.

2. **Git integration does not accept the `pbiModelDatabaseName` form at all** - it answers
   `DiscoverDependenciesFailed` with no further detail, which is what cost the most time
   here. That six-key shape (`connectionString: null`, `pbiServiceModelId`,
   `pbiModelVirtualServerName: sobe_wowvirtualserver`, `pbiModelDatabaseName`, `name`,
   `connectionType`) is what **fabric-cicd** writes for the *items API*. Git integration is
   a different code path and wants what Power BI Desktop writes: a single
   `connectionString` key, schema `definitionProperties/2.0.0`, with the id appended as
   `semanticmodelid=`.

Worth generalising: a definition shape that deploys through fabric-cicd is not necessarily
one that git integration will take. Dev goes through git and tst/prd through fabric-cicd, so
anything committed has to satisfy both, and the Desktop form is the one that does.

So: **committed artefacts carry dev's real ids**, and `parameter.yml` maps them onward.
This is also the shape fabric-cicd's own documentation assumes - `find_value` is the
development id. The `replace_value` side stays dynamic notation, so promoting to a new
environment needs no new ids; only re-creating a *dev* workspace requires updating the
`find_value` side.

## Implementation notes

Landed as `automation/src/fabricops/release/`:

* `policy.py` — the `deploy:` block resolved per layer and environment, plus layer
  ordering. Arguments that need a feature flag to work at all (`items_to_include`,
  the folder filters, `shortcut_exclude_regex`) turn their flag on automatically, because
  passing one without its flag is silently ignored by the library and reads as a bug in
  the recipe. The five `*_unpublish` flags are never implied — they delete data, so they
  have to be named in `deploy.features`, and the run warns when they are.
* `parameters.py` — the overlay. Bindings are declared as `binding: { connection: ... }`
  on the SemanticModel item; the connection id is looked up at release time. The legacy
  `sqlendpoint_model_binding.yml` is translated for one run with a deprecation warning,
  and a recipe-declared binding always wins over it.
* `runner.py` — the only module that imports `fabric-cicd`, imported lazily so `--dry-run`
  and the tests work without it. Cross-layer id accumulation is explicit and tested.

Left for later: **E09-S8** (`release export-config`) stays P3, and the scheduled
dependency-bump job (S7) belongs with E11's CI work.
