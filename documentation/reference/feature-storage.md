# Storage in feature development

How data is shared, isolated and cleaned up while people work on feature branches.
The design and the reasoning behind it are in [E07](../specs/E07-feature-dev-storage.md);
this page is the working reference.

## The default: shared

Storage is a shared layer per environment. Feature workspaces contain notebooks and
pipelines; they read and write the **shared dev lakehouse**. Nothing to configure.

That means two people changing the same table in dev interfere with each other. The
policy for living with it is at the end of this page; the alternative is the opt-in
feature schema below.

## The option: a schema per feature

```yaml
storage:
  default_schema: dbo
  feature_schema:
    enabled: false                 # today's behaviour is the default
    pattern: "dev_{feature}"       # must contain {feature} or {developer}
    drop_on_teardown: false
    lakehouses: ["Store/Curated"]  # where feature schemas live, for cleanup
```

The switch is a **solution authoring decision**: `enabled` says whether the solution's
notebooks call the resolver at all. Nothing plumbs it at run time, nothing is written into
a feature workspace, and no developer-specific value exists in the repo — which is why a
feature schema cannot leak into test or prod.

## Turning it on, and tuning it

Two places, and they hold different kinds of thing. The **recipe** carries the names
FabricOps itself needs, so teardown and `storage report` agree with the resolver. The
**notebook** carries what the resolver does, in one dictionary at the top of the
feature-storage cell of `fabricops_util`:

```python
FEATURE_STORAGE = {
    "enabled": False,                      # master switch; off is a no-op, no metadata calls
    "default_schema": DEFAULT_SCHEMA,
    "pattern": "dev_{feature}",
    "workspace_pattern": r"^\*(?:.*\s)?(?P<feature>\S+)\s+\([^)]+\)$",
    "write_always_to_feature": True,
    "read_overlay": True,
    "clone_on_first_write": True,
    "forkable_lakehouses": ["Curated"],
}
```

| Setting | Off / alternative | When you want that |
| --- | --- | --- |
| `enabled` | `False` (shipped default) | A small team working additively. Everything reads and writes `dbo`, and nothing below costs anything. |
| `write_always_to_feature` | `False` | Opt in per table: only tables already forked resolve to the feature schema, so you isolate the one table you are reshaping and leave the rest shared. Needs the table name — `write_schema()` with no table returns the shared schema, because it cannot know. |
| `read_overlay` | `False` | Always read shared, even for tables this feature has forked. For a feature writing tables nothing downstream should pick up yet. |
| `clone_on_first_write` | `False` | Forked tables start empty instead of as a `SHALLOW CLONE` of the shared one. Right when a feature rebuilds a table from scratch anyway. |
| `forkable_lakehouses` | `None` | Allow feature schemas in every lakehouse, not just `Curated`. |
| `workspace_pattern` | your own regex | Must carry a `(?P<feature>...)` group. A workspace that does not match is treated as shared. |

The rule for where a new setting belongs: **if it changes what the CLI does, it goes in the
recipe; if it changes what a notebook does, it goes here.**

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


## Ingestion: land to `Files`, write tables in Spark

The resolver only exists inside Spark, so a Data Pipeline Copy activity writing straight
into a lakehouse **table** is outside it — there is no Python to call, and the schema is
baked into the pipeline JSON.

**So pipelines land raw data into `Files`, and a notebook promotes it to a Delta table.**
`Files` has no schema, so there is nothing to isolate, and the promotion happens where the
resolver applies. Copy-activity-direct-to-table is unsupported with the flag on.

This is not a Fabric quirk. dbt has the same boundary: ingestion lands outside the model
graph and is read through `source()`, which is not environment-swapped. Raw is shared
everywhere; isolation starts at transformation.

## A new table does not need isolation

The instinct is to put anything new in a feature schema. Resist it. **Isolation protects
existing consumers, not namespace tidiness** — a table nothing reads yet cannot break
anyone, and writing it to `dbo` in dev is exactly the additive case the policy blesses.

What needs a feature schema is changing the shape of a table other people read.

## Forking a table cheaply

The first write to a forked table has to get the existing rows from somewhere. `fork_table()`
does it with a Delta `SHALLOW CLONE`, which copies metadata only and shares the source's
OneLake files:

```python
fork_table("orders", lakehouse="Curated")        # near-instant at any size
target = write_schema("orders", lakehouse="Curated")
```

Call it before an **incremental** write — a merge or an append. A full overwrite does not
need it, because the write creates the table itself. It is deliberately not something
`write_schema()` does for you: a function that reads as a lookup should not create tables.

Two things to know:

* **`VACUUM` on the shared table can break a clone** that still points at the files it
  removes. Keep feature schemas short-lived.
* **A clone is a snapshot** — it stops seeing new rows in the shared table from the moment
  it is made.

## The resolver contract

Notebooks stop naming schemas and ask a resolver. Any implementation must satisfy this:

| Behaviour | Rule |
| --- | --- |
| Mode detection | Derived from the **workspace**, never from committed configuration. A workspace matching the feature naming pattern is a feature workspace; everything else is not. Detection fails *toward shared* |
| Outside a feature workspace | A no-op: returns the default schema with **no metadata calls, no cache** |
| Read, in a feature workspace | Overlay — the feature schema if the table exists there, otherwise the default schema |
| Write, in a feature workspace | The feature schema. **Never falls back** — a write that quietly landed in shared is the one thing this exists to prevent. `write_always_to_feature: false` makes it opt-in per table instead |
| A deliberate shared write | `write_schema(shared=True)`. Explicit, never inferred |
| Forkable lakehouses | Only those in `forkable_lakehouses` (`Curated` by default); elsewhere the default schema |
| Table list | Fetched **once** per session, cached as a set; an absent schema is an empty set, not an error |
| Cache invalidation | A write registers the table; `refresh()` picks up tables created elsewhere |
| Transparency | The mode is logged on first use: `mode=feature · write=dev_add_orders · read=[dev_add_orders→dbo]` |
| Naming | `dev_{feature}` — per feature, because workspaces are per feature. Sanitised to letters, numbers and underscore |

**The boundary:** the resolver only applies where it runs — Spark. Semantic models in
Direct Lake, the SQL analytics endpoint and Power BI all read the default schema. So
feature work in Model and Present has no data isolation, and a feature spanning
engineering *and* reporting must be sequenced: merge the engineering change and run it in
dev first, so the table exists in `dbo` with its new shape.

### Where the resolver lives

`%run` only references notebooks **in the same workspace**, so a single shared helper
cannot be included from another layer. The resolver therefore ships as
`fabricops_util.Notebook` in each notebook-bearing layer:

```
solution/engineering/ingest/Utils/fabricops_util.Notebook/
solution/engineering/prepare/Utils/fabricops_util.Notebook/
```

included with `%run fabricops_util` in a cell of its own. The copies must stay identical —
`automation/tests/test_solution_layout.py` fails the build if they drift. A wheel in an
Environment item is the end state, but a full-mode environment adds 1–3 minutes to every
session start, which is not worth paying for one module.

## Addressing data

Because Store is its own workspace, every lakehouse reference from Ingest or Prepare is
**cross-workspace** — and cross-workspace references never auto-bind. The attached default
lakehouse therefore offers none of its usual promotion benefit here.

* **In a feature workspace, the helper reads the base from the description.** FabricOps
  stamps `base=Brickyard - {layer} [dev]` into every feature workspace it creates, so
  `layer_workspace_id("Store")` resolves to dev's Store without guessing from the
  workspace's own name. A workspace created before the stamp existed falls back to the
  solution prefix in its name (`*Brickyard add-orders (Prepare)`); re-running the feature
  pipeline stamps it.
* **Resolve the target** through the helper. Nothing to rewrite at deploy time, and a
  `parameter.yml` entry you can forget is a `parameter.yml` entry that eventually points
  production at dev.
* **Prefer four-part names** (`workspace.lakehouse.schema.table`) where lakehouses are
  schema-enabled.
* **`abfss://` for `Files`** and anything the SQL surface cannot express.
* Keep a default lakehouse attached for interactive convenience — just never depend on it
  in code.

Related: notebook→lakehouse auto-binding in Git is **off by default** and must be enabled
per notebook, so a default lakehouse otherwise stays pinned to dev's object id across
promotion.

## Prerequisite: schema-enabled lakehouses

**Done for this solution.** Landing, Base and Curated each carry `defaultSchema: dbo` in
their `lakehouse.metadata.json`, from which `fabric-cicd` derives
`creationPayload: {enableSchemas: true}`.

Two things follow from that, and they are the reason it had to be decided early:

* **Only `fabricops release` may create these lakehouses.** Schema enablement is a
  creation-only payload. If anything else creates the lakehouse first, the `defaultSchema`
  in the repository definition is ignored from then on, silently. This is why the recipe
  declares no lakehouses at all and the SQL connection references one with `from_item`
  instead.
* **There is no migration.** Microsoft Learn is explicit that tools to convert an existing
  non-schema lakehouse do not exist yet. Enabling schemas moves tables from
  `Tables/<table>` to `Tables/<schema>/<table>`, which breaks ABFS paths, shortcuts into
  `Tables/`, and semantic model bindings. If you have a flat lakehouse from an earlier run,
  delete it and let `release` recreate it.

The notebooks are written for this: they take their target from `write_schema()` and their
source from `read_schema()`, so turning the flag on changes where data goes without touching
a notebook. `create_table_shortcuts` takes a schema at each end, because a shortcut can span
one schema-enabled and one flat lakehouse.

The demo notebooks currently read `DEFAULT_SCHEMA` directly for their source rather than
calling `read_schema()`. That is correct while `enabled` is `False` and is the one edit
needed in each notebook before turning it on.

## Cleanup

A feature's data lives in `dev_<feature>` **inside the shared dev lakehouse**, so deleting
the feature workspace leaves it behind.

```bash
# what has accumulated?
fabricops storage report --environment dev

# drop with the feature, when the recipe says so
fabricops feature delete --branch feature/prepare/add-orders --dry-run
```

`drop_on_teardown` is the only destructive operation in this design: off by default, only
schemas matching `pattern`, only during feature teardown, dry-run first.

## When the flag is off

Everyone writes `dbo` in shared dev, so:

> Developer A adds a column to `curated.orders` and iterates. For an hour that table has a
> half-finished shape. Developer B, on an unrelated feature, reads it and either fails or —
> worse — succeeds with wrong numbers, debugging someone else's change.

The policy:

1. **Keep changes additive** while others are working. A new nullable column is usually
   safe; renaming, dropping or retyping is not.
2. **Announce it, and keep the window short.**
3. **Escalate** — turn the flag on for that piece of work, accepting the Spark boundary.
