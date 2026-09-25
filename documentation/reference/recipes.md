# The recipe

One file describes a solution: which workspaces exist, what goes in them, who can see
them, and how they are deployed. JSON or YAML — both parse to the same model, and neither
is converted to the other on disk.

```
automation/resources/environments/
  infrastructure.json          the base recipe
  infrastructure.dev.json      overlay: dev
  infrastructure.tst.json      overlay: tst
  infrastructure.prd.json      overlay: prd
  feature.json                 the feature-branch recipe
```

Resolution is base + environment overlay, later wins. `fabricops recipe render
--environment dev` prints exactly what the engine will read after merging and token
substitution, which is the fastest way to answer "why did it do that".

## Shape

```yaml
# yaml-language-server: $schema=../schemas/solution.schema.json
apiVersion: fabricops/v1
kind: Platform
metadata:
  solution: confidence
display_name_pattern: "Confidence - {layer} [{environment}]"

defaults:
  capacity: "{env:FABRIC_CAPACITY}"
  permissions:
    admin:
      - { type: Group, id: "11111111-1111-1111-1111-111111111111" }
  tags: [ "Solution:Confidence", "ManagedBy:FabricOps" ]
  git:
    provider: GitHub
    owner: my-org
    repository: my-fabric-repo
    branch: main
    credentials: { connection: my-github-connection }
    sync_on_commit: true          # pull on every run; without it, status is reported only
    conflict_resolution: prefer_remote   # prefer_remote | prefer_workspace | stop
  connections:
    - name: Confidence-SemanticModel
      type: PowerBIDatasets
      auth: ServicePrincipal
    - name: "Confidence-Curated [{environment}]"
      type: SQL
      from_item: { layer: Store, name: Curated, type: Lakehouse }
  deploy:
    item_types_in_scope: [Notebook, DataPipeline, Lakehouse, SemanticModel, Report]

layers:
  Store:
    git: { directory: solution/store }
  Model:
    permissions:
      admin:
        - { type: WorkspaceIdentity, name: "Confidence - Orchestrate [{environment}]" }
  Orchestrate:
    workspace_identity: true
    deploy:
      unpublish:
        skip: { prd: true }
```

## What belongs in the recipe, and what does not

**The recipe owns the platform.** Workspaces, permissions, capacity, git integration,
connections, tags, variable libraries, managed private endpoints.

**The repository owns the items.** A lakehouse, notebook, semantic model or report with a
definition under `solution/` is deployed by `fabricops release`, and the recipe should not
mention it. Declaring the same item in both places is not merely redundant: a lakehouse
created by `setup` can never afterwards be schema-enabled by `release`, because
`enableSchemas` is a creation-only payload.

The exception is an item that has no repository definition and genuinely needs
provisioning — then declare it under `layers.<Layer>.items` and it is created by `setup`.

## Tokens

`{environment}`, `{layer}`, `{solution}`, `{feature}`, `{developer}`, `{capacity}`,
`{branch}`, plus `{env:VAR}` for an environment variable. An unknown token is a validation
error, never a literal — a typo cannot silently reach Fabric.

The one place substitution is lenient is inside an inline item `definition.parts`, where
braces are ordinary code and only known tokens are touched.

Legacy names still work permanently: `{feature_name}`, `{layer_name}`,
`{environment_name}`, `{solution_name}`, `{identity_username}`, `{identity_id}`.

## Merge

Overlay order: `defaults` → base recipe → environment overlay → environment variables.

| Node | Default |
| --- | --- |
| Mapping | deep merge, child wins |
| Scalar | child wins |
| List of objects with a name | merge by name |
| List of scalars (tags) | union, order-stable |

The old `merge_type: 0|1|2` is accepted permanently and mapped to `keep` / `replace` /
`merge-by-name`.

## Connections

```yaml
connections:
  # A plain Fabric connection.
  - name: Confidence-SemanticModel
    type: PowerBIDatasets
    auth: ServicePrincipal
    scope: solution              # solution | environment

  # A SQL connection over a deployed item's endpoint.
  - name: "Confidence-Curated [{environment}]"
    type: SQL
    from_item: { layer: Store, name: Curated, type: Lakehouse }
```

`from_item` resolves the endpoint off the live item, asking that item type's handler both
where the endpoint lives and when it is ready — a lakehouse that has just arrived still has
an endpoint provisioning, and reading it once would build a connection to nothing.

Ordering follows how the layer gets its items. On a **git-connected** layer the connection
is planned after that layer's git sync, so a dev `setup` completes it in one run. On a
layer deployed by `release`, the item is not there during `setup`, so the connection
reports *waiting for …* — normal, not a failure — and `release` creates it afterwards.

### Git, and who wins

Both providers take the same shape, but not the same identity. `GitHub` wants `owner` and
`repository` and authenticates with a **PAT**; `AzureDevOps` wants `organization`,
`project` and `repository` and authenticates as the **service principal**, which must
first be a member of that Azure DevOps organization. That membership is granted in Azure
DevOps and nowhere else — see
[Git provider access](getting-started.md#git-provider-access). FabricOps creates the
connection named in `credentials: { connection: … }` if it does not exist; it cannot
create the organization membership behind it.

`sync_on_commit` gates the **write**, not the read. Status is always read, so a run always
tells you where the workspace stands relative to the branch:

```
up to date with git
behind git (sync_on_commit is off, so nothing was pulled)
behind git, 2 uncommitted workspace change(s)
pulled from git, overwriting 2 workspace change(s)
```

`conflict_resolution` decides what happens when both sides changed the same item.
`prefer_remote` is the default, because Fabric normalises items when it imports them - so a
"conflict" is the routine state in a git-connected workspace after any edit, not an
exceptional one. Whatever is overwritten is named in the log. Use `stop` where losing a
workspace change would matter more than staying in step with the branch.

## Items, when you do declare one

```yaml
items:
  - name: Metadata
    type: SQLDatabase             # any type the Fabric CLI knows
    description: "Control tables"
    folder: Zones/Control         # workspace folder
    creation_payload:             # passed straight to `fab mkdir -P`
      enableSchemas: true
    properties:                   # applied after creation, read-compare-write
      sparkSettings.pool.starterPool.maxNodeCount: 1
    definition:                   # platform-owned content, imported from the repo
      from: automation/generated/confidence/store/Ingest_Config.VariableLibrary
    tags: [ "Zone:Control" ]
    skip_creation: false
```

`definition:` also takes inline `parts`, rendered with tokens:

```yaml
    definition:
      parts:
        - path: notebook-content.py
          from: templates/bootstrap.py      # or: content: "..."
```

Both are idempotent: the item is exported and hashed before anything is imported, so an
unchanged definition is a no-op and an edit made in the portal is actually noticed.

## Properties

Keys are JSON paths into the item or workspace payload; values may be scalars or objects.
Applied after creation, always compared before writing, and visible in `--dry-run`. This
generalises the old `spark_settings` block, which still works and is mapped onto
`properties: sparkSettings.*`.

## Deployment policy

Everything `fabric-cicd`'s config file exposes, per layer and per environment, without a
config file per layer:

```yaml
deploy:
  item_types_in_scope: [Lakehouse, Notebook, SemanticModel]
  exclude_regex: "^tmp_"
  items_to_include: ["Curated.Lakehouse"]
  folder_exclude_regex: "^scratch/"
  folder_path_to_include: ["published"]
  shortcut_exclude_regex: "^raw_"
  unpublish:
    skip: { prd: true }            # any value can be a per-environment mapping
    exclude_regex: "^keep_"
  features: [enable_shortcut_publish]
  constants: { }
```

Two rules worth knowing:

* Arguments that need a feature flag to work at all — `items_to_include`, the folder
  filters, `shortcut_exclude_regex` — **turn their flag on for you**. Passing one without
  its flag is silently ignored by the library, which reads as a broken filter.
* The five `*_unpublish` flags are **never** implied. They let unpublish delete items that
  hold data, so they must be named in `deploy.features`, and the run warns when they are.

Use `_ALL_` as an environment key to mean "every environment".

## Semantic model binding

```yaml
items:
  - name: Sales
    type: SemanticModel
    binding: { connection: "Confidence-Curated [{environment}]" }
```

At release time the connection id is resolved by name and emitted as a
`semantic_model_binding` parameter. A Direct Lake **on OneLake** model needs no binding —
it reads OneLake directly. Import and Direct Lake on SQL do.

## References: ids that can only be ids

Some Fabric references must be an item id, and no name form exists:

* a report's semantic model — Fabric rejects a connection string that names the workspace
  and model with *"The semantic model identifier is invalid. Ensure that a valid GUID is
  provided"*;
* a Direct Lake expression — `AzureStorage.DataLake(<workspaceId>/<itemId>)`;
* a pipeline's activities — `notebookId` and `workspaceId`.

Item ids are per-tenant, so **a repository that ships working example content cannot ship
ids that resolve in anyone else's tenant.** The recipe declares where each id lives and what
it points at, and one command puts the right values in:

```yaml
references:
  - file: solution/analytics/present/Rebrickable.Report/definition.pbir
    replace:
      - at: datasetReference.byConnection.pbiModelDatabaseName   # JSON: dotted path
        layer: Model
        item: Rebrickable
        type: SemanticModel
        label: "report: semantic model"

  - file: solution/analytics/model/Rebrickable.SemanticModel/definition/expressions.tmdl
    replace:
      - pattern: 'onelake\.dfs\.fabric\.microsoft\.com/([0-9a-fA-F-]{36})'   # text: regex
        layer: Store                                            # no item = the workspace
        label: "model: Store workspace"

  - file: solution/engineering/orchestrate/Load Rebrickable.DataPipeline/pipeline-content.json
    replace:
      - at: properties.activities.0.typeProperties.notebookId    # a numeric segment
        layer: Ingest                                            # indexes a list
        item: Rebrickable_Ingest
        type: Notebook
        label: "pipeline: Ingest notebook"
```

```bash
fabricops references sync --environment dev            # report
fabricops references sync --environment dev --apply    # rewrite, then commit
```

Add `blocks_sync: true` where Fabric resolves the reference **at git sync time** - a
report's semantic model, a pipeline's notebooks. Those layers then report as *deferred*
until the reference matches, instead of the sync failing with `DiscoverDependenciesFailed`.
Leave it off for a reference Fabric treats as opaque: a semantic model's M expression syncs
whatever it contains, and blocking on it would only add a bootstrap round.

JSON documents take a dotted path, where a numeric segment indexes a list. Text formats
take a regex whose **capture group** surrounds the id, so the same id appearing elsewhere in
the file for another reason is left alone. Nothing is written without `--apply`, and a
target that is not deployed yet is a warning rather than a silent skip.

This is the first thing to run after a first `setup` and `release` in a new tenant.

**Only the git-connected environment's ids belong in the repository.** `--apply` refuses on
any other environment, because writing tst's ids into git would point the git-connected
workspaces at the wrong items - and it would look like it had worked, since every id
involved is real. Other environments get their ids from `parameter.yml` at release time.
Read-only runs are allowed anywhere, and are a reasonable way to see what a deployed
environment holds.

## Parameters

`automation/resources/parameters/parameter.yml` is yours and is never rewritten.
Generated entries go to `generated/dynamic.parameter.yml`, pulled in by `extend:`. The
overlay is written even when empty, so the reference never dangles.

Prefer, in order:

1. fabric-cicd's **dynamic notation** — `$workspace.<name>`,
   `$workspace.<name>.$items.<Type>.<name>.$id`, `$sqlendpoint` — resolved at deploy time
   and survives a workspace being recreated;
2. **`$ENV:`** values for what the pipeline already knows;
3. a **generated overlay** entry, for what neither can express.

Note `$workspace.<name>` already *is* the workspace id — a trailing `.$id` is only valid
after an `$items.` segment.

## Feature recipes

```yaml
kind: Feature
display_name_pattern: "*{feature} ({layer})"
branch:
  pattern: "feature/{developer}/{topic}"
  layer_from_branch: true
layers:
  Prepare: { always: false }
  Model:   { always: true }
storage:
  default_schema: dbo
  feature_schema:
    enabled: false
    pattern: "dev_{feature}"
    drop_on_teardown: false
    lakehouses: ["Store/Curated"]
```

The second segment of the branch name decides which layers a feature gets, in this order:

1. **A group recipe exists** — `feature.<segment>.json` next to `feature.json` — and its
   `layers` are the whole answer.
2. **The segment is a layer name** — that layer, plus any layer marked `always: true`.
3. **Neither** — every layer.

A group recipe *selects*. It names its layers and inherits their definitions from
`feature.json`, so it is usually two lines:

```json
{ "layers": { "Ingest": {}, "Prepare": {} } }
```

`feature/engineering/add-orders` then creates two workspaces, whatever `feature.json`
marks `always`: a group is explicit, and explicit wins. Say something about a layer inside
the group and it merges on top of the inherited definition. Name a layer `feature.json`
does not define and validation fails; a group cannot introduce one.

Developer overlays (`feature.<developer>.json`) are different: they tune settings and never
select layers. The two compose — a group picks the layers, the developer file adjusts them.

See [feature-storage.md](feature-storage.md) for the storage strategy.

## Validating

```bash
python -m fabricops recipe validate --all              # every solution and environment
python -m fabricops recipe render --environment dev    # the merged result
```

`recipe validate --all` is what PR validation should run. An unknown key is an error with
a suggestion, not a silent no-op.
