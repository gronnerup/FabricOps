# E06 — Where variable libraries fit in FabricOps (and where they don't)

**Goal:** decide, with evidence, which configuration belongs in a Fabric variable
library (VL) versus the recipe/parameter file — and generate VLs from the recipe where
they win.

## What variable libraries actually are (verified, Aug 2026)

| Property | Reality | Source |
| --- | --- | --- |
| Scope | A VL is a **workspace item**; consumers must be **in the same workspace**. Cross-workspace consumption is not supported | [variable-library-overview](https://learn.microsoft.com/fabric/cicd/variable-library/variable-library-overview), [notebookutils-variable-library](https://learn.microsoft.com/fabric/data-engineering/notebookutils/notebookutils-variable-library) |
| Value sets | Multiple named value sets; exactly **one active per workspace**; the active set is workspace state, **not** part of the item definition | [value-sets](https://learn.microsoft.com/fabric/cicd/variable-library/value-sets), [understand-best-practices-fabric-cicd](https://learn.microsoft.com/fabric/fundamentals/understand-best-practices-fabric-cicd) |
| Types | Boolean, Integer, Number, String, DateTime, Guid, **ItemReference** (workspace+item id, preview), **ConnectionReference** | [variable-types](https://learn.microsoft.com/fabric/cicd/variable-library/variable-types) |
| Consumers | Data pipelines, notebooks (NotebookUtils and `%%configure`), **lakehouse shortcuts**, Dataflow Gen2, Copy job, User data functions, Airflow jobs, Plan | variable-library-overview |
| Notebook access | `notebookutils.variableLibrary.get("$(/**/lib/var)")`; **read-only**, and **service principal is not supported** | notebookutils-variable-library |
| `%%configure` | Supported for basic variables, **not** for ItemReference | [item-reference-variable-type](https://learn.microsoft.com/fabric/cicd/variable-library/item-reference-variable-type) |
| Shortcut variables | Assignable **only through the UI today** — "REST API assignment isn't supported" | [assign-variables-to-shortcuts](https://learn.microsoft.com/fabric/onelake/assign-variables-to-shortcuts) |
| Git / CI-CD | VL is git-supported and deployment-pipeline-supported; `fabric-cicd` deploys VLs **first** and can activate the value set whose name matches the target environment | understand-best-practices-fabric-cicd |
| Limits | ≤1,000 variables, ≤1,000 value sets, <10,000 cells, ≤1 MB per item | variable-library-overview |

## The verdict

The instinct to avoid VLs "because they are not cross-workspace" is **correct for the
platform layer and wrong for the solution layer**.

```
Recipe (git)                     → what to build            → FabricOps engine
Run manifest (.fabricops)        → what was built (ids)      → parameter generation
parameter.yml (fabric-cicd)      → deploy-time rewriting     → item definitions
Variable library (per workspace) → run-time configuration    → pipelines/notebooks/shortcuts
```

* **Platform/infrastructure configuration stays in the recipe.** Capacities, layers,
  permissions, git settings, item inventory are cross-workspace by nature and must be
  readable *before* any workspace exists. A VL cannot serve this.
* **Deploy-time GUID/URL rewriting stays with `fabric-cicd` `parameter.yml`.** It
  operates on definitions before publish and works across workspaces.
* **Run-time configuration belongs in a variable library** — and FabricOps should
  *generate* it. A pipeline that needs "which lakehouse do I write to in this stage",
  or a notebook that needs "landing container path", is better served by a VL than by
  a hardcoded value + find/replace, because the value resolves at run time in the
  consumer's own context.

The cross-workspace limitation is handled by **generating one VL per workspace that
needs one**, from a single definition in the recipe. The recipe stays the single source
of truth; the VL is a deployed projection of it — the same pattern as tags (E05), with
the important difference that VLs *are* item definitions and therefore git- and
deployment-pipeline-friendly.

### Recipe surface

```yaml
layers:
  Orchestrate:
    variable_libraries:
      - name: Platform_Config
        # generated into every workspace listed under 'workspaces:' (default: this layer)
        workspaces: [Orchestrate, Ingest, Prepare]
        value_sets: [dev, tst, prd]        # value set names == fabricops environments
        variables:
          - name: curated_lakehouse
            type: ItemReference
            value: { item: Curated, type: Lakehouse, layer: Store }   # resolved per env
          - name: landing_path
            type: String
            values: { default: "abfss://…/dev", tst: "abfss://…/tst", prd: "abfss://…/prd" }
          - name: max_parallel_copies
            type: Integer
            values: { default: 4, prd: 16 }
```

Generation rules:
* Variables of type `ItemReference`/`ConnectionReference` are resolved from the run
  manifest (E03 outputs) per environment — this is exactly the information FabricOps
  already has and currently only writes into `parameter.yml`.
* Written as a real item definition under
  `automation/generated/<solution>/<layer>/<name>.VariableLibrary/`, committed or
  deployed depending on `--generated-mode commit|deploy`. Committing means the VL is
  reviewable in PRs and deployable by `fabric-cicd` like any other item.
* Value set activation is delegated to `fabric-cicd` (it activates the value set whose
  name matches the environment passed to the release), with
  `fabricops varlib activate --environment tst` for workspaces outside the release path
  (e.g. feature workspaces). No config file is involved — see E09 §1.

### Hard constraints to design around

1. **SPN cannot read VLs from notebooks.** Any notebook that runs under a service
   principal (scheduled pipeline runs are the common case) cannot use
   `notebookutils.variableLibrary`. → Keep notebook configuration reachable *both* ways:
   pipeline passes the value as a parameter (pipeline *can* read the VL), or the
   notebook falls back to a config item. Document this in the notebook template.
2. **Shortcut variables are UI-only.** The attractive pattern "shortcut target as a
   variable per environment/developer" (E07) **cannot be automated today**. Track it;
   do not build on it yet.
3. **Active value set is workspace state.** After any workspace re-create, the active
   value set must be re-applied — put it in the plan, not in a runbook.

## User stories

**E06-S1 — Declare variable libraries in the recipe**
AC: `variable_libraries:` supported; validation enforces name rules, type validity, and
that every `value_sets` entry matches a known environment.

**E06-S2 — Generate VL definitions per workspace**
AC: one recipe entry produces N workspace-scoped VL definitions with default + per-env
value sets; `ItemReference` values resolved from the manifest; regeneration is
deterministic (stable ordering, no diff churn).

**E06-S3 — Activate the right value set**
AC: after deployment the target workspace's active value set matches the environment;
verified by reading it back; also applied for feature workspaces.

**E06-S4 — Replace find/replace where a VL is better**
AC: at least one demo pipeline and one notebook in `solution/` consume a VL instead of
a `parameter.yml` find/replace entry; the removed entries are documented in the blog
post as a before/after.

**E06-S5 — Guardrails doc**
AC: `documentation/reference/configuration-decision.md` — a one-page decision table
(recipe vs parameter.yml vs VL vs tag) that the blog post can reuse verbatim.

## Non-goals

* Using a VL as the platform recipe. Rejected: not readable before workspaces exist,
  not cross-workspace, and it would split the source of truth.
* Writing to VLs from notebooks (not supported).
