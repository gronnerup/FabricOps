# E07 — Storage in feature development

**Decision (2026-08-31):** storage stays **shared per environment**. Feature work reads and
writes the shared dev lakehouse by default. Per-feature schema isolation is an **opt-in
solution convention**, off by default. Shortcut-based and clone-based isolation are
evaluated and **out of scope** for now.

## Scope: this is a solution concern, not an architecture one

The architecture is settled and does not change here: **Store is its own layer, shared per
environment** (Episode 2's one non-negotiable rule). Feature workspaces are created for the
code layers; storage is not branched out.

What *is* in scope is a convention: **which schema does a notebook write to?** That is a
property of a solution, not of the platform. Two solutions on the same platform can answer
it differently, and the same solution can answer it differently over time. Getting this
distinction right matters, because it keeps a runtime convention out of the architecture
where it would otherwise calcify.

## The default: shared dev storage

Feature workspaces contain notebooks and pipelines. They read and write `dbo` in the shared
dev lakehouse. Nothing to configure, nothing to clean up.

This is the default because the alternatives cost more than they return at small scale, and
because the platform facts make the obvious alternatives worse than they look:

* A branched-out Store workspace gets **empty lakehouses** — definitions deploy, data does
  not. It is useless for feature work without rehydration.
* **Writes through a OneLake shortcut go to the target**, so a feature-local lakehouse
  fed by shortcuts gives no write isolation for anything that already exists (§Platform facts).
* Ten shortcuts per OneLake target path caps shortcut-based approaches at roughly ten
  concurrent feature workspaces per shared schema.

The known cost of the default is the one that started this whole discussion: two developers
writing the same table in shared dev interfere with each other. §When the flag is off says
what to do about that.

## The option: per-feature schemas, resolved at runtime

When a solution turns this on, notebooks stop naming schemas and ask a resolver instead:

```python
source = read_schema("orders", lakehouse="Curated")     # dev_add_orders if forked, else dbo
target = write_schema("orders", lakehouse="Curated")    # dev_add_orders in a feature workspace

fork_table("orders", lakehouse="Curated")               # before an incremental write
```

The contract the resolver must satisfy:

| Behaviour | Rule |
| --- | --- |
| **Mode detection** | Derived from the **workspace**, never from committed configuration. A workspace matching the feature naming pattern is a feature workspace; everything else is not. Detection must fail *toward shared*. |
| **Outside a feature workspace** | A no-op. Returns the default schema with **no metadata calls, no cache, no cost**. Dev, test and prod behave identically to today. |
| **Read, in a feature workspace** | Overlay: the feature schema if the table exists there, otherwise the default schema. |
| **Write, in a feature workspace** | Always the feature schema by default. **Never falls back** — a feature run cannot write to shared data by accident. `write_always_to_feature: false` switches to opt-in per table: only tables already forked resolve to the feature schema. |
| **A deliberate shared write** | `write_schema(shared=True)`. Explicit, never inferred. |
| **Forkable lakehouses** | Feature schemas are allowed only in the lakehouses named in `forkable_lakehouses` — `Curated` by default. Elsewhere the resolver returns the default schema. |
| **Materialising a fork** | `fork_table()` creates the feature's copy with a Delta `SHALLOW CLONE` of the shared table. Metadata only, so it is near-instant at any size. Required before an *incremental* write; a full overwrite creates the table itself. |
| **Table list** | Fetched **once** per session and cached as a set; resolution is a membership test, not a metadata lookup per table. Absent schema resolves to an empty set, not an error. |
| **Cache invalidation** | A write registers the table in the cached set, so a table created earlier in the session resolves to the overlay later in the same session. An explicit `refresh()` covers tables created elsewhere. |
| **Transparency** | The resolved mode is logged loudly on first use: `mode=feature · write=dev_add_orders · read=[dev_add_orders→dbo]`. Silent resolution is how people lose an afternoon. |
| **Naming** | `dev_{feature}` — **per feature, not per developer**, because feature workspaces are per feature. Two features by the same person are two branches, two workspaces, two schemas. Sanitised to letters, numbers and underscores (Fabric's schema naming rule). |

Why derived-from-workspace rather than a setting: there is then **no developer- or
feature-specific value anywhere in the repo or in any config**, so nothing can leak into
test or prod. In prod the resolver returns `dbo` regardless of what any configuration says.
That is a structural guarantee rather than a discipline.

Why a write never falls back to shared, even though detection fails toward shared: the two
errors are not comparable. Resolving a write to the feature schema when it did not need to
be isolated wastes storage. Resolving it to the shared schema when it did need to be
isolated silently mutates dev for everybody, from a branch. Take the cheap mistake.

A tempting rule that was rejected: *write to `dbo` when the table does not exist there yet,
and to the feature schema when it does.* It reads as the best of both, and it is a trap.
Behaviour becomes a function of remote state and timing, two people creating the same table
race each other, and the protection disappears at exactly the moment somebody starts
depending on the table. Predictability is worth more than cleverness here.

### How the resolver reaches a notebook

`%run` **only references notebooks in the same workspace**, so a single shared helper in
Core cannot be included from Ingest or Prepare. The alternatives were weighed:

| Option | Verdict |
| --- | --- |
| Helper notebook per notebook-bearing layer, `%run` locally | **Chosen.** Zero session-start cost, no build step, no cross-workspace reference |
| Environment item per layer, resolver as a wheel | The documented pattern, and where this ends up — but a full-mode environment adds 1-3 minutes to *every* session start, which is absurd for one small module |
| One shared Environment in Core, attached cross-workspace | Works, and `fabric-cicd` can rewrite the pinned environment id per stage. Costs a parameter entry and a mapping maintained by hand; needs identical capacity and network settings across workspaces |
| `notebookutils.notebook.run()` | Runs a separate job and does not share variable context - orchestration, not a library |

So the delivery ladder is:

1. **Now:** `fabricops_util.Notebook` in each notebook-bearing layer
   (`solution/engineering/ingest/Utils/`, `solution/engineering/prepare/Utils/`), included with `%run
   fabricops_util` in a cell of its own. Two copies of one file, guarded by a CI check
   that asserts they are byte-identical.
2. **Later:** a `.py` module in the Environment's *Resources* folder (mounted at `/env`),
   versioned with the environment, no wheel build.
3. **Eventually:** a wheel as a custom library in an Environment per layer, with notebook
   and environment versioned together as the notebook docs prescribe.

FabricOps owns the contract, the recipe surface, the teardown and the drift check. The
resolver itself is solution content.

## Addressing data: how a notebook names its target

A related decision, because it has the same shape. A notebook can reach a lakehouse three
ways, and in *this* architecture one of them is a trap.

**Store is its own workspace**, so a notebook in Ingest or Prepare always references a
lakehouse **cross-workspace** - and a cross-workspace reference never auto-binds. The Git
definition stores an object ID pinned to the source workspace, so the usual argument for
the attached default lakehouse ("it rebinds itself on promotion") does not apply here.
"Lakehouse Auto-Binding in Git" only helps when the lakehouse sits in the same workspace,
and it is off by default in any case.

| | Default bound lakehouse | Absolute `abfss://` | Resolved through the helper |
| --- | --- | --- | --- |
| Where the target lives | notebook metadata, invisible in code | in the code | derived at run time |
| Promotion | needs deploy-time rewriting | needs parameterisation | nothing to rewrite |
| Multiple lakehouses | one default only | any number | any number |
| Visible in review | no | yes | yes |

**Decision:** resolve it. Keep a default lakehouse attached for interactive convenience
(explorer, `Files/` browsing) but never depend on it in code. Prefer the four-part name
`workspace.lakehouse.schema.table` where lakehouses are schema-enabled; fall back to
`abfss://` for `Files` and anything the SQL surface cannot express. Because the helper
derives the target from the runtime context, there is no `parameter.yml` entry to forget -
and a forgotten one points production at dev.

## Ingestion: pipelines land to `Files`, Spark writes tables

The resolver lives in Spark. A Data Pipeline Copy activity writing straight into a lakehouse
table names its schema in pipeline JSON and calls no Python, so it is outside the resolver
entirely. Three ways to close that were considered and all rejected:

* **A second resolver for pipelines.** Two implementations of one policy, which drift. This
  codebase has made that mistake often enough to recognise it.
* **Pipeline parameters.** Something has to supply the value, and in a feature workspace
  nothing does — the pipeline is run by hand or by a schedule, neither of which knows the
  feature.
* **A variable library per feature workspace.** A variable library is a git item, so a
  per-developer value is either committed — the leak this whole design exists to prevent —
  or a permanent uncommitted change in the workspace.

**The rule instead: a pipeline lands raw data into `Files`, and a notebook promotes it to a
Delta table.** `Files` has no schema, so there is nothing to isolate, and the promotion runs
in Spark where the resolver applies. Landing → Base → Curated already has this shape.

Copy-activity-direct-to-table is therefore **unsupported with the flag on**, and that is
stated rather than half-solved. It is also not a Fabric peculiarity: dbt has exactly the
same boundary. Ingestion tools land outside the model graph into a fixed schema and dbt
reads them through `source()`, which is not environment-swapped by default. Raw is shared
everywhere; isolation starts at the transformation layer.

## A new table does not need isolation

Worth stating plainly, because the instinct runs the other way: **isolation protects
existing consumers, not namespace tidiness.** A brand-new table that nothing reads yet
cannot break anybody, and writing it to `dbo` in dev is the additive case the policy already
blesses. What needs a feature schema is *changing the shape of a table other people read*.

So: ingest a new dimension into `dbo` with the flag off. If a fortnight later it has three
consumers and needs restructuring, that is when the flag earns its cost.

## Which layers are forkable

`Curated` only, by default. Reshaping a table in `Base` means everything downstream of it
should run off the fork too, so a single Base change pulls a chain of Curated tables into
the feature schema — much more divergence than it looks like from the call site. Base stays
shared and a Base change follows the additive policy.

This is a policy, not a limitation: `forkable_lakehouses: None` allows every lakehouse for
solutions that want it.

## What the option costs: the Spark boundary

**The resolver only applies where it runs — Spark.** A semantic model in Direct Lake, a
query through the SQL analytics endpoint, a Power BI report: none of them call a Python
helper, so all of them read the default schema.

Two consequences, both of which belong in the docs rather than in a footnote:

1. **Feature work in Model and Present has no data isolation.** A branched-out Model
   workspace reads shared dev data. That is a deliberate gap, not an oversight.
2. **A feature spanning engineering *and* reporting cannot be validated end to end with the
   flag on.** The notebook writes `dev_x.orders`; the semantic model reads `dbo.orders` and
   never sees the new column. Either sequence it as two features — engineering merged and
   *run* in dev first, so the table exists in `dbo` with the new shape — or leave the flag
   off for that work.

So the flag trades **cross-developer safety** for **cross-layer validation**. That is the
real reason it is off by default: turn it on for engineering-heavy work where several people
write tables, leave it off when a feature spans engineering and reporting.

## Recipe surface

```yaml
storage:
  default_schema: dbo
  feature_schema:
    enabled: false                 # today's behaviour is the default
    pattern: "dev_{feature}"
    drop_on_teardown: false
```

Deliberately small. Each value has exactly one consumer:

| Value | Consumed by |
| --- | --- |
| `default_schema` | the resolver's default; the docs |
| `feature_schema.enabled` | a solution-authoring decision — whether the solution's notebooks call the resolver at all. Nothing plumbs it at runtime |
| `feature_schema.pattern` | the naming contract shared by the resolver, teardown and the orphan report |
| `feature_schema.drop_on_teardown` | feature teardown (§Teardown) |

No pipeline involvement, no provisioning action, no variable library, nothing written into
the feature workspace. The recipe is a contract so that the resolver, the reaper and the
documentation agree on a name.

## Configuration surface: the notebook, not the recipe

The recipe carries the names that FabricOps itself needs — the pattern, so teardown and the
orphan report agree with the resolver. **The resolver's own behaviour is configured in the
notebook**, in a single dictionary at the top of the feature-storage cell of
`fabricops_util`:

```python
FEATURE_STORAGE = {
    "enabled": False,                      # master switch; off is a no-op with no metadata calls
    "default_schema": DEFAULT_SCHEMA,
    "pattern": "dev_{feature}",
    "workspace_pattern": r"^\*(?:.*\s)?(?P<feature>\S+)\s+\([^)]+\)$",
    "write_always_to_feature": True,       # False: opt in per table, only where already forked
    "read_overlay": True,                  # False: always read the shared schema
    "clone_on_first_write": True,          # fork with SHALLOW CLONE rather than starting empty
    "forkable_lakehouses": ["Curated"],    # None for all
}
```

It lives there rather than in the recipe for the same reason `enabled` does: this is a
**solution authoring decision**, taken once when the platform is set up, by whoever writes
the notebooks. A small team working additively sets `enabled: False` and never thinks about
it again. Nothing here is per-developer, per-feature or per-environment, so there is still
no value anywhere that could leak into test or production.

The split is worth being explicit about, because two places to configure one feature invites
drift:

| Lives in the recipe | Lives in `FEATURE_STORAGE` |
| --- | --- |
| `pattern`, so teardown and `storage report` can find the schemas | everything the resolver does with that pattern |
| `drop_on_teardown`, because FabricOps performs the delete | `enabled`, the write mode, the read overlay, cloning, forkable lakehouses |

The rule: FabricOps needs a name, the notebook needs a behaviour. If a value changes what
the CLI does it belongs in the recipe; if it changes what a notebook does it belongs here.

### Two switches, and they must agree

There are two `enabled` flags and they do different jobs:

| | Says | Consumed by |
| --- | --- | --- |
| `storage.feature_schema.enabled` (recipe) | *this solution uses feature schemas* | FabricOps — feature teardown only drops schemas when this is on |
| `FEATURE_STORAGE["enabled"]` (notebook) | *notebooks resolve at runtime* | the resolver |

Set them together. The mismatches are not symmetric:

* **Recipe on, notebook off** — teardown goes looking and finds nothing. Harmless.
* **Recipe off, notebook on** — notebooks create feature schemas and **nothing ever drops
  them**. They accumulate in shared dev until somebody notices. This is the one to avoid,
  and a test fails the build on it.


## Changing storage *definitions*: `feature/store/<topic>`

Shortcuts and lakehouse settings are item definition, so changing them needs a branch — and
that already works: the feature planner filters layers from the branch name, so
`feature/store/add-crm-shortcut` provisions **only** the Store feature workspace.

The lakehouses in that workspace are empty, and that is fine: you are editing metadata, not
running transformations. Add the shortcut, commit `shortcuts.metadata.json`, PR, merge, and
dev Store picks it up on sync.

Two notes: the shortcut's target still needs per-environment parameterisation at deploy time
(E09), and a transient shortcut in a feature workspace consumes one of the ten slots on its
target path while it exists.

## Prerequisite: schema-enabled lakehouses

None of the schema work is possible without them, and this repo's Landing, Base and Curated
lakehouses were created without `enableSchemas`.

* **New lakehouses:** `creation_payload: { enableSchemas: true }` in the recipe (E03 passes
  it straight through to the CLI).
* **Existing lakehouses:** a documented migration, not a toggle.

**Do it now.** Enabling schemas changes the physical layout from `Tables/<table>` to
`Tables/<schema>/<table>`, which breaks ABFS paths in notebooks, shortcuts pointing into
`Tables/`, and semantic model bindings. Today the lakehouses hold no committed tables and
`shortcuts.metadata.json` is `[]`, so the migration is free. With 200 tables and a dozen
shortcuts it is a project.

## Teardown

The thing to understand: **the data a feature writes does not live in the feature
workspace.** It lives in `dev_<feature>` inside the *shared dev lakehouse*, which survives.
So deleting the feature workspace (E08) leaves the schema and its tables behind, quietly
holding storage, forever.

`drop_on_teardown` means: when a feature is reaped, also drop `dev_<feature>` in the shared
dev lakehouse. Never the lakehouse, never a workspace, never anything outside dev.

It is the only destructive operation in this design, so:

* default **off**;
* only schemas matching `feature_schema.pattern`;
* only during feature teardown, never as a standalone command;
* dry-run by default, like the rest of the reaper.

And when it is off, `fabricops storage report` lists orphaned feature schemas — otherwise
"off" just means the mess accumulates invisibly.

## When the flag is off

With `enabled: false`, everyone writes `dbo` in shared dev, and this happens:

> Developer A is adding a column to `curated.orders` and iterating on the notebook that
> populates it. For an hour, `dbo.orders` has a half-finished shape and partly-loaded data.
> Developer B, on an unrelated feature, reads `dbo.orders` from their own feature workspace,
> and either fails or — worse — succeeds with wrong numbers, debugging someone else's change.

The mitigations are procedural, and that is acceptable as long as they are written down
rather than folklore:

1. **Keep changes additive** while others are working. A new nullable column is usually
   safe; renaming, dropping or retyping is not.
2. **Announce it and keep the window short.**
3. **Escalate:** turn the flag on for that piece of work, accepting the Spark boundary above.

## Evaluated, not in scope

Kept as analysis — it is the material for the storage blog posts — but not on the build list.

| Option | Why not now |
| --- | --- |
| **Shortcut workspace** (feature-local lakehouse, shortcuts to shared dev) | Writes pass through to the target, so it isolates only *new* tables and gives nothing for evolving existing ones. Ten shortcuts per target path caps concurrency. Schema shortcuts fix the table-count problem (one shortcut per schema, new tables reflected automatically) but not the write-through one |
| **Delta `SHALLOW CLONE`** into a feature schema | **Possible** — Fabric supports `SHALLOW CLONE` (not `DEEP CLONE`), and it is the natural way to get a writable copy of an existing table in seconds. Deferred by choice, not by capability. Revisit once the resolver is in use, since the two compose well |
| **Warehouse `CREATE TABLE AS CLONE OF`** | Zero-copy and carries RLS/CLS, but cannot cross warehouses or workspaces, and does not apply to a Lakehouse SQL endpoint |
| **Full/subset copy per developer** | Slow and expensive; only sensible for small curated fixtures |
| **Synthetic data set** | Best fit for deterministic PR validation, not for feature development against real shapes |
| **Shortcut target as a variable library value** | The most elegant option on paper, and **not automatable**: assignment is UI-only, with no REST API |

## Platform facts (verified 2026-08-31)

| Fact | Consequence |
| --- | --- |
| A write through a shortcut goes **to the target**; no copy-on-write, no materialisation. Writing needs permission on both shortcut and target path | Shortcut-based isolation covers new tables only |
| `SHALLOW CLONE` supported; `DEEP CLONE` not. Clones share source files, so `VACUUM` on the source can break a clone | Clone-based isolation is viable but needs retention awareness |
| One schema shortcut covers a whole schema; new tables and schema changes are reflected automatically | 200 tables across 3 lakehouses is 3 shortcuts, not 200 |
| 100,000 shortcuts per item; **10 per target path**; 5 chained hops; no `%`/`+` or non-Latin characters in names | Caps concurrent shortcut-based feature workspaces |
| Schema shortcuts require schema-enabled lakehouses; schema names allow letters, numbers and `_` | Prerequisite, and a naming constraint on the pattern |
| Lakehouse deploys as metadata only | A branched Store workspace is empty |
| Warehouse table clone cannot cross warehouses or workspaces | Rules out the warehouse route for cross-workspace isolation |

Sources: [delta-lake-clone](https://learn.microsoft.com/fabric/data-engineering/delta-lake-clone) ·
[clone-table](https://learn.microsoft.com/fabric/data-warehouse/clone-table) ·
[onelake-shortcuts](https://learn.microsoft.com/fabric/onelake/onelake-shortcuts) ·
[onelake-shortcut-security](https://learn.microsoft.com/fabric/onelake/onelake-shortcut-security) ·
[lakehouse-schemas](https://learn.microsoft.com/fabric/data-engineering/lakehouse-schemas) ·
[assign-variables-to-shortcuts](https://learn.microsoft.com/fabric/onelake/assign-variables-to-shortcuts)

## Materialising a fork cheaply: `SHALLOW CLONE`

The cost of "every write goes to the feature schema" is the first write: the feature schema
has no `orders`, so the notebook has to build one. For an incremental merge that means
reading the shared table and rewriting it whole.

Delta `SHALLOW CLONE` removes that cost, and **Fabric supports it** (verified on Learn,
2026-09-16):

```sql
CREATE TABLE dev_add_orders.orders SHALLOW CLONE dbo.orders
```

Metadata only — the clone references the source's existing OneLake files, so it is
near-instant whatever the table's size, and storage is paid only for files that diverge
afterwards. Two caveats that belong next to the feature, not in a footnote:

* **`VACUUM` on the source can break a clone** that still references the files it removes.
  Feature schemas must be short-lived; this is another argument for `drop_on_teardown`.
* **A clone is a snapshot.** It stops seeing rows landing in the shared table from the
  moment it is made. Right for a dimension being reshaped over two days, wrong for a fact
  table the feature needs current data in.

Only `SHALLOW` exists in Fabric and in open-source Delta; `DEEP CLONE` does not. Fabric
Warehouse has the equivalent as T-SQL `CREATE TABLE ... AS CLONE OF`.

Cloning is deliberately **not** a side effect of `write_schema()`. A function that reads as
a lookup must not create tables, and auto-cloning on every first write is wrong anyway: a
full overwrite does not need the old contents. `fork_table()` is the explicit call, and
`clone_on_first_write` decides whether it clones or leaves the write to start empty.

## How other people solve it

* **Microsoft's internal data engineering team** keeps lakehouses in a workspace separate
  from their dependent items specifically so feature workspaces do not need rehydration:
  *"the feature branch notebooks always point to the PPE Lakehouse."* Shared storage, no
  isolation, no rehydration — the same trade this spec takes as its default.
* **A community enterprise implementation** puts `lakehouses/` inside each project folder,
  so every environment and every feature workspace gets its own lakehouse. Full isolation,
  at the cost of rehydration and of storage sharing a deployment scope (and an orphan-delete
  blast radius) with the code that reads it.
* **Databricks** solves it a layer lower: Asset Bundles prefix resources per user, and Unity
  Catalog puts the user's name in the target schema (`${workspace.current_user.short_name}`).
  No built-in read overlay; where a table is too big to rebuild, the answer is a shallow
  clone.
* **dbt is the closest analogue, and it arrived at both halves of this contract
  independently.** Every developer builds into their own target schema (`dbt_peer`) and
  writes never fall back. Then `defer` with `--state` resolves `ref()` to the *production*
  relation for models the developer has not built themselves, so you build one model in your
  schema and read everything else from prod. That is precisely the read overlay. dbt also has
  the same ingestion boundary and does not solve it either: raw lands outside the model graph
  and is read through `source()`, unswapped.
* **Snowflake** sidesteps the problem with zero-copy clone — `CREATE DATABASE dev_peer CLONE
  prod` — so full isolation is cheap and no overlay is needed at all.

The spectrum is really *how cheap is a copy*. Snowflake: free, so clone everything.
Databricks: cheap, so clone the expensive tables. Fabric now has `SHALLOW CLONE`, which puts
it nearer Databricks than this spec assumed when it was first written — hence the overlay
**and** cloning, rather than the overlay alone.

## User stories

**E07-S1 — Schema-enabled lakehouses** *(prerequisite)*
AC: `creation_payload: { enableSchemas: true }` supported for new lakehouses in the recipe;
the existing Landing/Base/Curated migration is documented with its path-change consequences;
a validation warning fires when `feature_schema.enabled` is true for a solution whose
lakehouses are not schema-enabled.

**E07-S2 — Recipe surface**
AC: the `storage` block validates (`default_schema`, `feature_schema.{enabled,pattern,
drop_on_teardown}`); `pattern` is checked against Fabric's schema naming rules and must
contain `{feature}` or `{developer}`; defaults reproduce today's behaviour exactly when the
block is absent.

**E07-S3 — Resolver contract and delivery**
AC: `documentation/reference/feature-storage.md` states the contract table above as a
testable specification — mode detection, no-op outside feature workspaces, read overlay,
write without fallback, cache and invalidation, mode logging, naming, and how a notebook
names its target. The reference implementation ships as `fabricops_util.Notebook` in each
notebook-bearing layer, with a CI check asserting the copies are identical; the wheel and
Environment steps of the ladder are documented but not built.

**E07-S4 — Storage-definition flow, documented**
AC: `feature/store/<topic>` documented as supported behaviour of the existing
layer-from-branch filter, including that the branched lakehouses are empty by design and
that shortcut targets are parameterised at deploy time.

**E07-S5 — Teardown and orphan report**
AC: `drop_on_teardown` drops only pattern-matching schemas, only during feature teardown,
dry-run by default; `fabricops storage report` lists feature schemas in shared dev with age
and whether their branch still exists.

**E07-S6 — Coordination policy when the flag is off**
AC: the additive-change/announce/escalate policy written into the reference docs, so the
answer to "I need to reshape a shared table" is policy rather than folklore.

**E07-S7 — Blog assets**
AC: three spinoff posts drafted from this spec — why storage is the hard part; the resolver
pattern in practice; what shortcuts and clones actually isolate.

> Retired: the previous **S3 (shortcut workspace)** and **S4 (shallow clone)** stories.
> Shortcut isolation does not survive the write-through finding; shallow clone is possible
> but deferred. Both are recorded above under *Evaluated, not in scope*.

## Open questions

**Settled in review, 2026-09-16:**

* Detection is by **name pattern**, configurable as `workspace_pattern`. The
  `Lifecycle:Feature` tag stays available as later hardening if renames become a problem in
  practice; it costs an API call and read permission on workspace metadata, which is not
  worth paying before anyone has been bitten.
* Writes stay **always to the feature schema**, with `write_always_to_feature: false` for
  teams that prefer opting in per table, and `shared=True` as the explicit escape hatch.
* Pipelines are **out of scope by design** — land to `Files`, promote in Spark.
* `Curated` only is **forkable** by default.
* Forks are materialised with **`SHALLOW CLONE`**, through an explicit `fork_table()`.

**Still open:**

1. Is a shared **"golden subset"** lakehouse worth adding as the deterministic data source
   for PR validation, independent of dev? Still open from the first draft of this spec.
2. Should `fork_table()` be called automatically by the notebook templates before an
   incremental write, or stay something the notebook author opts into? Automatic is friendlier
   and hides a `CREATE TABLE`; explicit is honest and easy to forget. *Leaning explicit until
   somebody forgets it twice.*
3. Nothing enforces the `Files`-then-Spark rule. A lint over pipeline JSON that fails a Copy
   activity whose sink is a lakehouse **table** would make the boundary real rather than
   documented — worth doing only once the flag is actually in use somewhere.
