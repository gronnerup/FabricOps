# E01 — Recipe model, formats (JSON + YAML), schema and merge semantics

**Goal:** one canonical description of a Fabric solution, authorable in either JSON or
YAML, validated before anything touches Fabric.

## Problem

* The recipe format is JSON-only. Anyone doing IaC elsewhere (Bicep, Terraform,
  Kubernetes, GitHub/ADO pipelines) writes YAML and expects comments and anchors.
* The recipe is read with `misc.load_json()` + `misc.merge_json()` and then consumed
  directly by the scripts. There is no schema, so a typo (`git_directoryname` vs
  `git_directoryName`) is a silent no-op discovered in Fabric, not in CI.
* `merge_json()` carries a magic `merge_type` key (0/1/2) that leaks into the data
  model and is also matched *by list item identity* on `item_name` only.

## Decision: canonical **model**, not a canonical **file format**

Do **not** transform YAML→JSON files (or the reverse) on disk. Both formats parse to
the same Python structure anyway; a file-level translator adds a build step, a second
source of truth and diff noise.

```
platform.yml ─┐
              ├─→ loader (ruamel.yaml / json) ─→ dict ─→ JSON Schema validate ─→ normalize ─→ Solution model
platform.json ┘
```

* **Loader**: extension-driven (`.yml`, `.yaml`, `.json`). `ruamel.yaml` is already a
  dependency. YAML is parsed in safe mode; duplicate keys are an error.
* **Schema**: one JSON Schema (draft 2020-12) in
  `automation/resources/schemas/solution.schema.json`, valid for both formats.
  Published so authors get IDE completion:
  * YAML: `# yaml-language-server: $schema=../schemas/solution.schema.json`
  * JSON: `"$schema": "../schemas/solution.schema.json"`
* **Normalizer**: produces a frozen `Solution` dataclass graph
  (`Solution → Environment → Workspace(layer) → Item[] / Connection[] / Tag[] ...`).
  Everything downstream (planner, release, feature, parameter generation) consumes the
  model, never raw dicts.
* **Round-trip:** `fabricops recipe convert --to yaml|json` is offered as a one-off
  authoring convenience (comments are lost going to JSON — documented), *not* as part
  of the execution path.

## Canonical recipe shape

Backwards-compatible with today's keys; new keys are additive. YAML shown; JSON is the
same tree.

```yaml
# yaml-language-server: $schema=../schemas/solution.schema.json
apiVersion: fabricops/v1          # NEW, optional: absent means fabricops/v1
kind: Platform                    # optional: inferred from which recipe resolved it
metadata:
  solution: my-data-platform      # NEW: solution name (see E02)
  display_name_pattern: "Confidence - {layer} [{environment}]"   # was: name

defaults:                         # was: generic
  capacity: "{env:FABRIC_CAPACITY}"   # was: capacity_name
  permissions:
    admin:
      - { type: Group, id: "{env:PLATFORM_ADMIN_GROUP_ID}" }
  tags: [ "Solution:Confidence", "ManagedBy:FabricOps" ]         # NEW (E05)
  git:                            # was: git_settings
    provider: GitHub              # GitHub | AzureDevOps
    owner: gronnerup
    repository: FabricOps
    branch: main
    credentials: { connection: FabricOps-GitHub }

connections:                      # was: generic.fabric_connections
  - name: Confidence-SemanticModel
    type: PowerBIDatasets
    auth: ServicePrincipal
    scope: solution               # solution | environment  (replaces is_primary logic)

layers:                           # ordered map preserved; list also accepted
  Store:
    git: { directory: solution/store }
    items:
      - name: Curated
        type: Lakehouse
        creation_payload: { enableSchemas: true }     # → fab mkdir -P  (E03)
        properties: {}                                # → fab set -q … (E03)
        connection: { name: "Confidence-Curated [{environment}]", type: SQL }
        tags: [ "Layer:Store", "Zone:Curated" ]
      - name: Landing
        type: Lakehouse
  Orchestrate:
    workspace_identity: true      # was: create_workspace_identity
    permissions:
      admin:
        - { type: WorkspaceIdentity, workspace: "Confidence - Orchestrate [{environment}]" }
```

### Token substitution

One documented token set, applied by the normalizer to every string value (not just
names): `{environment}`, `{layer}`, `{solution}`, `{feature}`, `{developer}`,
`{capacity}`, plus `{env:VAR}` for environment variables. Unknown tokens are a
validation error, not a literal.

### Merge semantics (replacing `merge_type`)

`merge_type: 0|1|2` is replaced by explicit, per-key strategies with sane defaults:

| Node kind | Default | Override |
| --- | --- | --- |
| Mapping | deep merge, child wins | `$merge: replace` on the mapping |
| Scalar | child wins | `$merge: keep` to protect a base value |
| List of objects with `name` | merge by `name` | `$merge: replace` / `$merge: append` |
| List of scalars (e.g. tags) | union, order-stable | `$merge: replace` |

`merge_type` is still accepted and mapped to the new strategies (`0→keep`, `1→replace`,
`2→merge-by-name`). Per decision 4 this alias is **permanent**, not time-boxed: it warns
once per run at `debug` level and never fails.

Overlay order (later wins): `defaults` → base recipe → environment overlay →
CLI `--set key=value` → environment variables.

## User stories

**E01-S1 — Author a solution in YAML**
*As a platform engineer I can write `platform.yml` instead of `infrastructure.json` and
the setup runs identically.*
AC: same recipe expressed in both formats produces byte-identical plan output
(`--dry-run --output json`); a fixture test asserts this.

**E01-S2 — Get told about mistakes before Fabric does**
*As an author, an invalid recipe fails fast with a path-qualified message.*
AC: `fabricops recipe validate` exits non-zero and prints e.g.
`layers.Store.items[0].typ: unknown property (did you mean 'type'?)`;
runs in PR validation for every recipe file in the repo.

**E01-S3 — Predictable overlays**
*As an author I can see how base + env overlay merged.*
AC: `fabricops recipe render --environment dev [--format yaml|json]` prints the fully
merged, token-substituted recipe; `--explain key.path` shows which file set the value.

**E01-S4 — Legacy recipes keep working, permanently**
AC: current `infrastructure.json` + `infrastructure.dev.json` (incl. `merge_type` and
items keyed by type) render to the same effective configuration as before; covered by a
golden-file test plus one test per alias; the generated recipe reference carries the
alias table (decision 4 — no removal release).

**E01-S5 — Schema is the documentation**
AC: `solution.schema.json` carries `title`/`description`/`examples` for every property;
`documentation/reference/recipe.md` is generated from it in CI.

## Non-goals

* A templating language (no loops/conditionals in recipes). If a recipe needs
  generation, generate it outside and commit the result — or use `--set`.
* Terraform/Bicep provider parity. This is a recipe, not a state-tracking IaC tool
  (state is discussed in E03 "drift").
