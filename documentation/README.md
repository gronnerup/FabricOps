# FabricOps documentation

## Using it

* **[reference/getting-started.md](reference/getting-started.md)** — from an empty tenant
  to a working environment: prerequisites, credentials, the order the first run has to go
  in, every command, exit codes, and what to check when something breaks.
* **[reference/recipes.md](reference/recipes.md)** — the recipe format: tokens, merge
  rules, connections, items, properties, deployment policy, parameters, feature recipes.
* **[reference/feature-storage.md](reference/feature-storage.md)** — how a feature branch
  shares or isolates data, and the resolver contract notebooks use.

## Building it

* **[reference/development.md](reference/development.md)** — layout, the fake `fab` test
  harness, and the conventions an action has to follow.

## Why it is the way it is

* **[specs/](specs/)** — one file per epic (E01–E13), each with the problem, the design,
  the user stories, and the decisions made along the way including the ones that turned
  out to be wrong. `specs/README.md` is the decision log.
* **[research/fabric-platform-findings.md](research/fabric-platform-findings.md)** —
  platform constraints verified against Microsoft Learn, with links: tag limits, variable
  library scoping, shortcut behaviour, schema enablement.
* **[backlog.md](backlog.md)** — what is done, what is next, and how it maps to the blog
  series.

`blog/` is drafting material and is excluded from the public export.
