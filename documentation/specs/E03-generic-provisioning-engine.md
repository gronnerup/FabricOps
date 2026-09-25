# E03 — Generic provisioning engine, handlers and property passthrough

**Goal:** provisioning any Fabric item type is a recipe entry (`name` + `type`), and
arbitrary Fabric properties/definitions can be passed through without new Python.

## Problem

`fabric_setup.py` is a single top-to-bottom script where the order of operations, the
console output and the item semantics are the same code. The type knowledge is inline:

```python
if item.get("connection_name") and item_type in {"Lakehouse", "SQLDatabase"}: …
if item_type in {"Lakehouse"}:   # wait for SQL endpoint provisioning
server = (… sqlEndpointProperties.connectionString if item_type == "Lakehouse"
          else … serverFqdn)
```

Consequences: a Warehouse or Eventhouse silently gets no connection; adding
`enableSchemas` to a Lakehouse needs code; the *only* generic property escape hatch is
`spark_settings` in the feature script.

## Design

### 1. Resources and actions

The planner turns the canonical model (E01) into a list of typed **actions**, each with
a stable `id`, a `depends_on` set, and `apply()` / `destroy()`:

| Resource | Backing call (primary) | Notes |
| --- | --- | --- |
| `workspace` | `fab mkdir <ws>.Workspace -P capacityname=…` | + `fab set` for properties |
| `workspace_folder` | `fab mkdir <ws>.Workspace/<folder>` | E10; git folders are 10 levels max |
| `workspace_identity` | `fab mkdir <ws>.Workspace/.managedidentities/…` | |
| `role_assignment` | `fab acl set … -I <id> -R <role>` | incl. `WorkspaceIdentity` principals |
| `item` | `fab mkdir <ws>.Workspace/<name>.<Type> -P …` | fully generic, see §2 |
| `item_definition` | `fab import <path> -i <local dir> --format …` | see §3 |
| `item_properties` | `fab set <path> -q <jsonpath> -i <value>` | see §4 |
| `connection` | `fab mkdir .connections/<name>.Connection -P …` | typed builders per connection kind |
| `connection_role` | `fab api connections/{id}/roleAssignments` | |
| `git_connect` / `git_init` / `git_update` | `fab api workspaces/{id}/git/*` | |
| `workspace_relation` | `fab api workspaces/{id}/git/workspaceRelations` | E08 |
| `tag_apply` | `fab api …/applyTags` | E05 |
| `variable_library` | item + definition parts | E06 |
| `shortcut` | `fab ln … --type oneLake --target …` | E07 |
| `private_endpoint` | `fab mkdir …/.managedprivateendpoints/…` | as today |

Ordering is derived from `depends_on`, not from statement order: workspaces →
identities → items → item definitions/properties → connections (needs item metadata)
→ role assignments → git → tags. Independent branches can run concurrently
(`--parallel N`, default 1 until E11 test coverage exists).

### 2. Generic item creation

```yaml
items:
  - name: Curated
    type: Lakehouse            # any value in the Fabric CLI item-type registry
    description: "Curated zone"
    folder: Zones/Curated      # optional workspace folder (E10)
    creation_payload:          # → -P key=value,…  (the API creationPayload)
      enableSchemas: true
    skip_creation: false       # was: skip_item_creation
```

* The item-type registry is loaded from the Fabric CLI (`fab desc all` / the CLI's own
  type table) and cached, so FabricOps does not maintain a parallel type list. Unknown
  types fail validation with the list of known types.
* `creation_payload` is passed straight through to `-P`. Known type-specific params
  today: `Lakehouse.enableSchemas`, `Warehouse.enableCaseInsensitive`,
  `KQLDatabase.{dbType,eventhouseId,clusterUri,databaseName}`,
  `MirroredDatabase.{mirrorType,connectionId,database,defaultSchema,mountedTables}`,
  `Report.semanticModelId`, `MountedDataFactory.{subscriptionId,resourceGroup,factoryName}`.
  FabricOps validates against the CLI's own required/optional lists instead of
  duplicating them.

### 3. Item definition passthrough

Two ways to get a real definition into an item, both declarative:

```yaml
  - name: Ingest_Config
    type: VariableLibrary
    definition:
      from: solution/store/Ingest_Config.VariableLibrary   # repo-relative dir
  - name: Notebook_Bootstrap
    type: Notebook
    definition:
      format: fabricGitSource        # optional, per CLI definition_format_mapping
      parts:                         # inline parts, rendered with recipe tokens
        - path: notebook-content.py
          from: templates/bootstrap.py
```

`definition.from` maps to `fab import <item path> -i <dir> --format <fmt> -f`.
Inline `parts` are rendered into a temp dir (tokens substituted) and imported the same
way — so the same mechanism serves "deploy this repo folder" and "generate this file".

> Release-time deployment of the *solution* items stays with `fabric-cicd` (E09).
> `definition:` here is for **platform-owned** items that are not part of the solution
> source tree (bootstrap notebooks, generated variable libraries, config artefacts).

### 4. Generic property passthrough (generalising `spark_settings`)

The feature script's `spark_settings` → `fab set -q sparkSettings.<k> -i <v>` pattern
becomes a general mechanism available on both workspaces and items:

```yaml
Prepare:
  properties:                                  # workspace-level
    sparkSettings.pool.starterPool.maxNodeCount: 1
    sparkSettings.pool.starterPool.maxExecutors: 1
    sparkSettings.environment.name: "Prepare-Env"
  items:
    - name: Curated
      type: Lakehouse
      properties:                              # item-level
        displayName: Curated                   # rename-safe
```

Rules:
* Keys are JSON paths into the item/workspace payload; values may be scalars **or**
  objects (objects are serialised to inline JSON, matching `fab set … -i <json> -f`).
* Nested mappings are accepted and flattened (so today's nested `spark_settings:` block
  still works and is mapped to `properties: sparkSettings.*`).
* Applied **after** creation, always with `-f`, and skipped when the current value
  already matches (read-compare-write → idempotent, and visible in `--dry-run`).
* An `api:` escape hatch exists for anything not reachable via `fab set`:

```yaml
      api:
        - { method: post, path: "workspaces/{workspace_id}/items/{item_id}/applyTags",
            body: { tags: ["{tag:Layer:Store}"] }, when: created }
```
  `when: always|created|missing` controls re-execution; `{…}` tokens are resolved from
  the run context (E01 tokens plus `workspace_id`, `item_id`, `connection_id`, and
  handler outputs).

### 5. Type-specific handlers (the explicit special cases)

A handler registry keyed by item type, each declaring hooks:
`pre_create`, `post_create`, `wait_ready`, `outputs`, `pre_destroy`.

| Handler | Behaviour |
| --- | --- |
| `Lakehouse` | `wait_ready`: poll `properties.sqlEndpointProperties.provisioningStatus != InProgress` (today's inline loop, with backoff + configurable timeout); `outputs`: `sqlendpoint`, `sqlendpointid`, `schemas_enabled` |
| `Warehouse` | `outputs`: `connectionString`, `serverFqdn` (so `connection:` works for Warehouse too — a gap today) |
| `SQLDatabase` | `outputs`: `serverFqdn`, `databaseName` |
| `Eventhouse` | `outputs`: `queryServiceUri`, child `KQLDatabase` |
| `SemanticModel` | `post_create`: optional takeover; binding is delegated to `fabric-cicd` (E09) |
| `Report` | `pre_create`: resolve `semanticModelId` by name |
| `VariableLibrary` | `post_create`: set active value set (E06) |
| `default` | create + properties + definition + tags only |

`outputs` are the contract for everything downstream: connection creation, parameter
file generation, variable library values, shortcut targets. They are written to a
**run manifest** (see §6) instead of being stuffed back into the recipe dict as today
(`layer_definition["workspace_id"] = …`).

### 6. Run manifest, idempotency and drift

* Every run writes `.fabricops/runs/<run-id>/manifest.json`: resolved recipe hash,
  every action with status (`created|existed|updated|skipped|failed`), duration, and
  all `outputs` (ids, endpoints, connection ids).
* The manifest — not the live recipe object — is the input to parameter-file generation
  (`utils_build_parameter_file_dynamic.py` becomes a consumer of it) and to teardown.
* `fabricops plan --environment dev` diffs recipe vs. live Fabric and reports drift
  (items in Fabric not in recipe, properties that differ). Read-only; no auto-remediate
  in v1 (`--prune` is a later story, E11-S4).
* Idempotency rule for every action: **read first, act only on difference**, and never
  treat "already exists" as failure.

### 7. Teardown

Teardown is the same plan in reverse, filtered by `destroy()`-capable actions, with
`--keep tags=Retain:true` (E05) and a required `--confirm <solution>/<environment>`
for non-feature environments. Today's delete path re-implements the walk separately;
after this spec it is one code path.

## User stories

**E03-S1 — Any item type by name and type**
*As a platform engineer I add `- {name: Sales, type: Warehouse}` (or Eventhouse,
Environment, GraphQLApi, …) and it is provisioned, with no engine change.*
AC: matrix test provisions ≥8 distinct item types incl. one with a `creation_payload`;
no item type appears in engine control flow.

**E03-S2 — 3 environments × 7 layers from one recipe**
AC: sample recipe with 7 layers and 3 environments provisions end-to-end; plan output
shows dependency-ordered actions; total runtime reported per action.

**E03-S3 — Generic properties replace `spark_settings`**
AC: workspace and item `properties:` applied via `fab set`; nested legacy
`spark_settings` still honoured; re-run is a no-op (0 writes) proven by the run log.

**E03-S4 — Item definitions from the repo**
AC: `definition.from` imports a local item folder; `definition.parts` renders tokens
and imports; both idempotent (skip when unchanged by content hash).
*Done. Idempotency compares against the tenant, not against a recorded hash: the item is
exported to a temp directory and hashed, so an edit made in the portal is seen. `.platform`
is excluded from the hash and line endings are normalised, or a CRLF checkout would read
as permanent drift. Inline `parts` use **lenient** token substitution — in a recipe an
unknown `{token}` is a typo worth raising on, but in a notebook body braces are ordinary
syntax and only known tokens may be touched.*

**E03-S5 — Connections beyond Lakehouse/SQLDatabase**
AC: `connection:` on a Warehouse and on an Eventhouse creates the right connection type
using handler `outputs`; unsupported type fails validation, not at runtime.

**E03-S6 — Dependency-correct ordering**
AC: a Report that references a SemanticModel in another layer provisions after it; a
`WorkspaceIdentity` permission provisions after the identity exists (today this is a
hand-rolled second pass).

**E03-S7 — Run manifest**
AC: manifest written for every run; `fabricops manifest show --last` prints outputs;
parameter-file generation consumes it.

**E03-S8 — Plan / drift**
AC: `fabricops plan` exits 0 (no drift) / 3 (drift) with a readable report; used in a
nightly pipeline.
*Done as `fabricops plan --check`, deliberately not as the default. A bare `plan` stays
offline so a recipe change can be reviewed in a PR without credentials; the nightly job
passes `--check`. There is no separate checking code: a drift check **is** a dry run,
read differently — `created` means missing, `updated` means differs — so the check can
never disagree with what a real run would do. This forced two actions to become honest:
`SetProperties` now compares in a dry run rather than assuming it would write, and
`AssignRole` reads the current ACL first. Without the latter every role reported drift on
every run, which would have made a nightly check permanently red.*

**E03-S9 — Symmetric teardown**
AC: `--action delete` derives from the same plan; `Retain` tag honoured; deleting a
non-existent resource is a warning, not an error.

## Non-goals

* Full state management/locking (no `terraform.tfstate` equivalent). The manifest is a
  run record, and Fabric remains the source of truth for what exists.
* Deploying solution content (notebooks/models/reports) — that is `fabric-cicd` (E09).
