# Platform research findings (verified 2026-08-27)

Everything the specs rely on, with the constraint that actually matters for automation.
Verified against Microsoft Learn and against the installed Fabric CLI (`fab 1.6.1`).

## 1. Fabric CLI (`fab` 1.6.1)

| Finding | Detail | Impact |
| --- | --- | --- |
| Item creation is generic | `mkdir <ws>.Workspace/<name>.<Type> [-P k=v,…]`; payload is `{displayName, type, folderId, description}` + type-specific `creationPayload` | A generic provisioning engine (E03) needs **no per-type code** for creation |
| Type-specific params are enumerable | `Lakehouse.enableSchemas`, `Warehouse.enableCaseInsensitive`, `KQLDatabase.{dbType,eventhouseId,clusterUri,databaseName}`, `MirroredDatabase.{mirrorType,connectionId,database,defaultSchema,mountedTables}`, `Report.semanticModelId`, `MountedDataFactory.{subscriptionId,resourceGroup,factoryName}`; `mkdir <item> -P` prints them | Validate against the CLI's own lists instead of maintaining a copy |
| Folders are supported | `mkdir` folder command exists; `folderId` on item create; `folder_listing_enabled` config | Workspace folders are automatable (E10) |
| Definitions | `import`/`export` with `--format` (`Notebook: ipynb|fabricGitSource`, `SemanticModel: TMDL|TMSL`, `SparkJobDefinition: V1|V2`) | Declarative `definition:` passthrough is possible (E03) |
| Properties | `set <path> -q <jsonpath> -i <value> [-f]` accepts inline JSON | Generalises today's `spark_settings` to any property (E03 §4) |
| Shortcuts | `ln <path> --type oneLake|adlsGen2|amazonS3|…  --target|-i` | Storage strategies are automatable (E07) |
| Tables | `table load|optimize|schema|vacuum` | Useful for maintenance actions later |
| **No tag support** | no tag commands/paths in the package | Tags must go via `fab api` (E05) |
| Debug logging exists | `config set debug_enabled true` → rotating `fabcli_debug.log`, HTTP request/response, `Authorization` masked | Reuse it for `--log-level trace` instead of rebuilding (E04) |
| Auth via env vars | `FAB_TENANT_ID`, `FAB_SPN_CLIENT_ID`, `FAB_SPN_CLIENT_SECRET`, `FAB_SPN_CERT_PATH`, `FAB_SPN_FEDERATED_TOKEN`, `FAB_MANAGED_IDENTITY` | Keeps secrets off argv; enables OIDC/federated pipelines (E04) |
| Output format | `--output_format json` / `-q <jsonpath>` | Stop text-parsing `exists`/`get` (E04) |

## 2. Tags

See the table in [E05](../specs/E05-tags-extended-properties.md#what-tags-actually-are-verified-aug-2026). Headlines:

* Tags apply to **workspaces and items**, are **flat names ≤40 chars** (no value field),
  max **10 per object**, 10,000 per tenant.
* **Creating** tags is an admin API (`POST /v1/admin/tags/bulkCreateTags`,
  `Tenant.ReadWrite.All`); **applying** is `POST …/applyTags` with **tag GUIDs**
  (workspace variant is **preview**).
* **25 requests/minute/principal** on tag APIs.
* Tags are **not** in `.platform`/item definitions → not in git, not carried by
  deployment pipelines → must be reconciled by automation every run.
* Portal discovery/indexing can lag by hours.

Sources: [tags-overview](https://learn.microsoft.com/fabric/governance/tags-overview) ·
[tags-define](https://learn.microsoft.com/fabric/governance/tags-define) ·
[tags-apply](https://learn.microsoft.com/fabric/governance/tags-apply) ·
[apply-tags (item)](https://learn.microsoft.com/en-us/rest/api/fabric/core/tags/apply-tags) ·
[apply-workspace-tags](https://learn.microsoft.com/en-us/rest/api/fabric/core/workspaces/apply-workspace-tags) ·
[bulk-create-tags](https://learn.microsoft.com/en-us/rest/api/fabric/admin/tags/bulk-create-tags)

## 3. Variable libraries

* Workspace-scoped item; **consumers must be in the same workspace** (no cross-workspace).
* Exactly **one active value set per workspace**; the active set is workspace state, not
  part of the item definition → must be (re)applied by automation.
* Types: Boolean, Integer, Number, String, DateTime, Guid, **ItemReference** (preview),
  **ConnectionReference**.
* Consumers: data pipelines, notebooks (NotebookUtils + `%%configure`), **lakehouse
  shortcuts**, Dataflow Gen2, Copy job, User data functions, Airflow jobs, Plan.
* **Service principal is not supported** for `notebookutils.variableLibrary`.
* `%%configure` does not support ItemReference.
* **Shortcut variable assignment is UI-only** — "REST API assignment isn't supported".
* Limits: ≤1,000 variables, ≤1,000 value sets, <10,000 cells, ≤1 MB.
* `fabric-cicd` deploys VLs first and activates the value set matching the environment name.

Sources: [variable-library-overview](https://learn.microsoft.com/fabric/cicd/variable-library/variable-library-overview) ·
[value-sets](https://learn.microsoft.com/fabric/cicd/variable-library/value-sets) ·
[variable-types](https://learn.microsoft.com/fabric/cicd/variable-library/variable-types) ·
[item-reference-variable-type](https://learn.microsoft.com/fabric/cicd/variable-library/item-reference-variable-type) ·
[notebookutils-variable-library](https://learn.microsoft.com/fabric/data-engineering/notebookutils/notebookutils-variable-library) ·
[assign-variables-to-shortcuts](https://learn.microsoft.com/fabric/onelake/assign-variables-to-shortcuts) ·
[automate-variable-library](https://learn.microsoft.com/fabric/cicd/variable-library/automate-variable-library)

## 4. Storage / OneLake

* **Delta `SHALLOW CLONE` is supported** in Fabric (`VERSION AS OF` / `TIMESTAMP AS OF`);
  `DEEP CLONE` is **not**. Clones share source files → source `VACUUM` can break a clone.
* **Warehouse `CREATE TABLE AS CLONE OF`**: zero-copy, carries RLS/CLS/DDM, supports
  point-in-time — but **not across warehouses or workspaces**, and not on a Lakehouse SQL
  analytics endpoint.
* **Schema-enabled lakehouses**: schema grouping, schema-level access control, four-part
  cross-workspace queries (`workspace.lakehouse.schema.table`), **schema shortcuts**
  (schema-enabled lakehouses only); schema names allow letters/numbers/`_`.
* **OneLake shortcuts**: internal (Lakehouse, Warehouse, KQL DB, SQL DB, semantic model,
  mirrored DB/catalog) and external; cross-workspace and cross-item-type; ≤100,000
  shortcuts per item, ≤10 shortcuts per target path, ≤5 chained hops, no `%`/`+` in names,
  no non-Latin characters; access uses the **calling user's** identity, except Direct Lake
  over SQL / delegated identity where the calling item owner's identity is used.
* **A write through a shortcut goes to the target.** There is no copy-on-write and no
  materialisation: writing requires permission on *both* the shortcut path and the target
  path, so `ALTER TABLE … ADD COLUMN` through a shortcut either changes the shared source or
  fails. This is what rules shortcut-based write isolation out (E07).
* **A schema shortcut covers a whole schema** — one shortcut, not one per table — and new
  tables plus schema changes in the source are reflected automatically. Requires a
  schema-enabled lakehouse.

Sources: [delta-lake-clone](https://learn.microsoft.com/fabric/data-engineering/delta-lake-clone) ·
[clone-table](https://learn.microsoft.com/fabric/data-warehouse/clone-table) ·
[lakehouse-schemas](https://learn.microsoft.com/fabric/data-engineering/lakehouse-schemas) ·
[onelake-shortcuts](https://learn.microsoft.com/fabric/onelake/onelake-shortcuts)

Databricks comparison (for the blog): Asset Bundles `mode: development` prefixes
resources per user, and Unity Catalog schema-per-user
(`${workspace.current_user.short_name}`) gives run-time write isolation — the Fabric
equivalent is per-developer schema or per-developer lakehouse plus run-time indirection.
Source: [bundles deployment modes](https://learn.microsoft.com/en-us/azure/databricks/dev-tools/bundles/deployment-modes).

## 5. Git integration & branched workspaces

* **Create Workspace Relation** (preview): `POST /v1/workspaces/{id}/git/workspaceRelations`
  with `{relatedWorkspaceId, relationType: Base|Branch}` — the documented way to make an
  automation-created feature workspace behave like a native "Branch out" workspace.
  Requires admin on the branch workspace + contributor on the base; **SPN supported**;
  both workspaces must share the same git root directory; base cannot itself be a branch.
* Git automation covers connect, disconnect, get/update credentials, initialize, status,
  commit, update-from-git — all already used by FabricOps.
* **`initializeConnection` takes an `initializationStrategy`** (`None` | `PreferRemote` |
  `PreferWorkspace`) and *requires* one when content exists on both the remote and the
  workspace. An empty body works on a fresh workspace and fails with
  `MissingInitializationStrategy` once the workspace holds anything - so a layer that
  disconnects after initialising fails on its **second** run, not its first. Documented
  error code is `MissingInitializationPolicy`; the service returns
  `MissingInitializationStrategy`. Verified on a live tenant, 2026-09-08.
* There is **no API that lists workspace relations** - Create Workspace Relation is the only
  operation - so an existing relation can only be detected from the error code, and a
  generic `BadRequest` only from the response `message`.
* **`gitConnectionState` has three values, not two**: `NotConnected`, `Connected` and
  `ConnectedAndInitialized`. A workspace that has been connected *and* initialised reports
  the third, so treating "connected" as `== "Connected"` sends the code down the connect
  path and Fabric answers `WorkspaceAlreadyConnectedToGit`. The three states need three
  branches: connect + initialise, initialise only, sync. Verified on a live tenant,
  2026-09-05.
* **The two providers authenticate as different identities.** A `GitHubSourceControl`
  connection carries `credentialDetails.type=Key` (a PAT, `connectionEncryption=Encrypted`)
  and therefore borrows a *human's* repo access. An `AzureDevOpsSourceControl` connection
  carries `credentialDetails.type=ServicePrincipal` (`connectionEncryption=NotEncrypted`)
  and Fabric connects **as the service principal**, so the service principal must be added
  to the Azure DevOps **organization** (*Organization settings → Users → Add users*, Basic
  access) and to a **team** in the project (*Project settings → Teams*). Neither Fabric
  tenant settings nor the Entra app registration grant this; connection creation fails
  until it is done. Verified on contact with a live tenant, 2026-09-07.
* Multitenant: if the DevOps organization is in a different tenant from the app
  registration, the service principal does not exist there to be added. Register the app
  as multitenant and `az ad sp create --id <app-id>` against the DevOps tenant.
* **Folders**: workspace folder structure is mirrored in git, retained to **10 levels**;
  empty folders are not committed; empty git folders are auto-deleted; empty workspace
  folders are not. Connecting a foldered workspace to a folder-less branch yields
  uncommitted changes; updating first overwrites the workspace structure.
* **`.platform` is written with no trailing newline**, and git integration compares bytes -
  so one extra `\n`, which most editors and every JSON formatter add, makes that item report
  as *Modified* in every workspace connected to the branch, permanently and on every run. It
  is invisible in a diff viewer until Fabric itself commits, whose diff is a single removed
  blank line. Line endings are LF. Found on a live tenant 2026-09-09: three lakehouses whose
  only file is a `.platform` reported as changed for ever. Guarded by a test now.
* Source format v2 `.platform` carries only `type`, `displayName`, `description`,
  `logicalId` — **no tags, no properties** → confirms tags/properties are runtime
  metadata that automation must apply (E05, E03 §4).
* Branch-out limitations that matter for automation: needs capacity, only git-supported
  items, settings are **not** copied to the new workspace (spark settings, OAP, etc. must
  be applied by automation).

Sources: [create-workspace-relation](https://learn.microsoft.com/en-us/rest/api/fabric/core/git/create-workspace-relation) ·
[branched-workspace](https://learn.microsoft.com/fabric/cicd/git-integration/branched-workspace) ·
[git-automation](https://learn.microsoft.com/fabric/cicd/git-integration/git-automation) ·
[source-code-format](https://learn.microsoft.com/fabric/cicd/git-integration/source-code-format) ·
[git-integration-process](https://learn.microsoft.com/fabric/cicd/git-integration/git-integration-process) ·
[git-integration-with-service-principal](https://learn.microsoft.com/fabric/cicd/git-integration/git-integration-with-service-principal)

## 6. `fabric-cicd`

Verified against the **installed 0.3.1** (local venv) and the **published 1.3.0** docs.
Note the drift: `requirements.txt` pins nothing, so pipelines may already run 1.x while
local runs are on 0.3.1.

### Python API (what FabricOps depends on)

| Finding | Detail |
| --- | --- |
| Exports | `FabricWorkspace`, `publish_all_items`, `unpublish_all_orphan_items`, `deploy_with_config`, `DeploymentResult`, `DeploymentStatus`, `FeatureFlag`, `ItemType`, `append_feature_flag`, `change_log_level`, `configure_external_file_logging`, `disable_file_logging` |
| Publish filters are plain arguments | `publish_all_items(ws, item_name_exclude_regex=…, folder_path_exclude_regex=…, folder_path_to_include=…, items_to_include=…, shortcut_exclude_regex=…)` |
| Unpublish filters | `unpublish_all_orphan_items(ws, item_name_exclude_regex="^$", items_to_include=…)` |
| Structured outcome | `DeploymentResult(status, message)` on success; failures raise → usable for real exit codes |
| Library file logging | `configure_external_file_logging()` / `disable_file_logging()` |
| Feature flags (0.3.1) | `enable_shortcut_publish`, `enable_shortcut_exclude`, `enable_items_to_include`, `enable_exclude_folder`, `enable_include_folder`, `enable_environment_variable_replacement`, `enable_experimental_features`, `enable_response_collection`, and per-type unpublish opt-ins: `enable_lakehouse_unpublish`, `enable_warehouse_unpublish`, `enable_sqldatabase_unpublish`, `enable_eventhouse_unpublish`, `enable_kqldatabase_unpublish` |

### Parameter file

* Top-level keys: `extend`, `find_replace`, `key_value_replace`, `spark_pool`,
  `semantic_model_binding`.
* `find_replace`: `is_regex`, `ignore_case`, and `item_type` / `item_name` / `file_path`
  filters; `key_value_replace` uses JSONPath and auto-detects JSON/YAML content
  regardless of file extension.
* Dynamic values: `$workspace…`, `$items.<Type>.<name>.$id`, `$sqlendpoint`,
  `$sqlendpointid`, `$queryserviceuri`; `$ENV:` values behind
  `enable_environment_variable_replacement`; `_ALL_` environment key.
* **`extend`** ("Parameter File Templates"): a list of template files **relative to the
  main parameter file location**; all entries from main + templates merge into one
  parameter dictionary. The *main* file must sit in the root of `repository_directory`
  (or wherever the release points it); templates may live elsewhere.
* `semantic_model_binding`: `default.connection_id` plus per-model overrides, `_ALL_`
  supported, and a **list** of connection ids per model (multi-source models bound in one
  pass).

### Config file (evaluated and **not adopted** — see E09 §1)

* Sections/keys (identical in 0.3.1 and the 1.3.0 docs): `core` (`workspace`,
  `workspace_id`, `repository_directory`, `item_types_in_scope`, `parameter`), `publish`
  (`exclude_regex`, `folder_exclude_regex`, `folder_path_to_include`, `items_to_include`,
  `shortcut_exclude_regex`, `skip`), `unpublish` (`exclude_regex`, `items_to_include`,
  `skip`), `features`, `constants`. Every field supports environment-mapped values.
* **One workspace and one `repository_directory` per environment** → a layered solution
  needs one config file per layer.
* No `extend`/split and no documented generation story; the only runtime seam is
  `config_override` (Python) or `-P` (`fab deploy`), and required fields must exist in the
  base file to be overridden. Relative paths resolve against the config file location.
* `fab deploy --config <file> --target_env <env> [-P …] [-f]` exists in the installed
  **Fabric CLI 1.6.1** and wraps this flow — i.e. Microsoft is investing in config-based
  deployment, which is why E09 keeps a thin release boundary.

* Bulk Import/Export Item Definitions APIs remain the documented alternative for
  unsupported git providers or clone/migration scenarios (custom parameterisation
  required).

Sources: [parameterization (1.3.0)](https://microsoft.github.io/fabric-cicd/1.3.0/how_to/parameterization/) ·
[config deployment (1.3.0)](https://microsoft.github.io/fabric-cicd/1.3.0/how_to/config_deployment/) ·
[CI/CD concepts and best practices](https://learn.microsoft.com/fabric/fundamentals/understand-best-practices-fabric-cicd) ·
installed `fabric-cicd` 0.3.1 and `fab` 1.6.1

## 7. Gaps worth tracking (things we want but cannot automate today)

| Want | Status | Watch |
| --- | --- | --- |
| Assign a variable-library variable to a shortcut property via API | UI only | OneLake docs / release notes |
| Tag creation without tenant-admin rights | admin-only | Fabric admin API changes |
| Workspace `applyTags` GA | preview | Fabric core API changes |
| Cross-workspace variable library consumption | not supported | VL roadmap |
| SPN support for `notebookutils.variableLibrary` | not supported | Data Engineering release notes |
| Cross-workspace / cross-warehouse table clone | not supported | Warehouse release notes |
| Tags in item definition / git | not supported | git source format changes |
