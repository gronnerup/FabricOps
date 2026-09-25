# E13 — The reference (demo) solution

**Goal:** a minimal, end-to-end solution that proves the framework does what it claims —
and doubles as the contract test and the material for the getting-started guide.

## Why an empty reference implementation is not enough

Provisioning seven empty workspaces demonstrates nothing about cross-layer dependency
ordering, semantic model binding, the schema resolver, orphan control or environment
parameterisation. One thin slice through every layer exercises all of them, and gives the
nightly contract test (E11-S3) something to assert beyond "the workspace exists".

## Shape

One item per layer, following the architecture the series argues for — **Model and Present
stay separate**, because a reference implementation that quietly merges them undercuts
Episode 2.

| Layer | Item | Does |
| --- | --- | --- |
| Ingest | `FabricOps_ingest.Notebook` | Generates the source data and lands it raw |
| Store | `Landing` / `Base` / `Curated` lakehouses, schema-enabled | Holds it |
| Prepare | `FabricOps_base.Notebook`, `FabricOps_curate.Notebook` | raw → base → curated (star schema) |
| Model | `FabricOps.SemanticModel` | Direct Lake over Curated, a handful of measures |
| Present | `FabricOps.Report` | One page, enough to prove the binding works |
| Orchestrate | `Load FabricOps.DataPipeline` | Chains ingest → base → curate |
| Core | `Metadata.SQLDatabase` | Framework metadata |

## The data: LEGO-shaped, and generated

The domain is bricks — sets, parts, colours, themes, minifigs — which fits Peer's visual
motif and happens to be a naturally good star schema (`dim_theme`, `dim_part`,
`dim_colour`, `fact_set_part`).

**Generated with a fixed seed, not vendored.** Redistributing a real catalogue extract in
a public repository raises a licensing question the demo does not need, real extracts are
large enough to make the first run slow, and a generated set is deterministic — which is
what makes it usable as a test fixture. The domain language survives; only the provenance
changes.

Rules the sample holds to:

1. **Seconds, not minutes.** It runs on a trial capacity, in CI, repeatedly, and it is the
   first thing a reader executes.
2. **No run-time external dependency.** No API, no download, no key — it must work under
   outbound access protection.
3. **Idempotent.** Overwrite semantics throughout; a second run produces the same result.
4. **Schema-enabled lakehouses** from the start, so the resolver works without migration.
5. **Seed data is never a Fabric item** in the solution tree, or orphan control and git
   sync will both have opinions about it.

## What it proves

* dependency ordering — the pipeline cannot run before the notebooks exist, the semantic
  model cannot bind before Curated has a SQL endpoint;
* `semantic_model_binding` at deploy time (E09), otherwise untestable without a tenant;
* the schema resolver (E07) in a real notebook rather than in a docstring;
* a variable library consumed by a pipeline and a notebook (E06);
* orphan control, since the demo is small enough to reason about what should be deleted.

## User stories

**E13-S1 — Generated brick dataset** — AC: a seeded generator producing 4–6 tables in
seconds, deterministic across runs, no external calls.
**E13-S2 — The three notebooks** — AC: ingest, base and curate, each idempotent, each
addressing storage through the resolver rather than a hardcoded path or an attached
default lakehouse.
**E13-S3 — Semantic model and report** — AC: Direct Lake over Curated with a few measures;
BPA-clean so `pr-validation` keeps its purpose; one report page.
**E13-S4 — Orchestration** — AC: a pipeline chaining the three notebooks, deployable and
runnable end to end.
**E13-S5 — Contract test** — AC: the nightly disposable environment provisions the demo,
runs the pipeline, asserts row counts, and tears everything down.
