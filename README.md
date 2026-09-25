<p align="center">
  <img src="FabricOps_250.png" alt="FabricOps">
</p>

# FabricOps

Infrastructure as code for Microsoft Fabric. A recipe describes your platform: the
layers, the environments, the permissions, the git wiring, the connections. FabricOps
provisions it, keeps it in step with its branch, gives every feature branch its own
workspaces and tears them down again, and deploys content through environments with
`fabric-cicd`. It runs locally, from GitHub Actions and from Azure DevOps.

It is the companion repository to the blog series
[Fabric Automation at Scale: From Chaos to Confidence](https://peerinsights.emono.dk/series/fabric-automation-at-scale),
and it ships a small working solution so every command here can be run against a tenant
of your own.

## What it does

* **Provisions from a recipe.** One file per solution, an overlay per environment,
  JSON or YAML. Workspaces, capacity, roles, workspace identities, git integration,
  connections. Run it twice and the second run changes nothing.
* **Plans before it acts.** `plan` prints the ordered actions offline. `--dry-run`
  performs every read and no write, so the plan reflects the tenant, not an assumption.
* **Keeps dev in step with git.** A merge to `main` syncs the layers the merge touched.
  A nightly run syncs everything. Fabric never pulls on its own, so something has to.
* **Gives a branch its workspaces.** `feature/<group>/<topic>` creates a workspace per
  layer the group selects, connected to that branch. The pull request closing removes
  them. A daily reaper catches the ones nobody came back for, and only deletes when the
  git provider confirms the branch is gone.
* **Resolves references that can only be ids.** A report needs its model's id, a
  pipeline needs a notebook's id, and both are per tenant. `references sync` rewrites
  them from the manifest of what was actually created.
* **Deploys with `fabric-cicd`,** layer by layer, in dependency order, with a
  `parameter.yml` the recipe helps fill in.
* **Isolates feature data when asked.** Off by default. Opt in, and notebooks in a
  feature workspace write to a schema of their own in the shared lakehouse and read
  through to the base tables. Teardown drops the schema.
* **Tests itself offline.** Over 700 tests run against a fake `fab` binary. They prove
  the arguments, the parsing and the control flow. Nothing in the suite reaches a tenant.

## Quick start

You need Python 3.12 or newer, a Fabric capacity, and a service principal that is allowed
to use Fabric APIs and create workspaces. The full list, including the two tenant settings
and the Azure DevOps step people miss, is in
[documentation/reference/getting-started.md](documentation/reference/getting-started.md).

```bash
git clone https://github.com/gronnerup/FabricOps.git
cd FabricOps
pip install -r automation/resources/requirements.txt
export PYTHONPATH=automation/src

# Point the demo at your tenant: capacity name, admin group, git provider and repository.
#   automation/resources/solutions/demo/platform.yml
#   automation/resources/solutions/demo/platform.dev.yml

python -m fabricops recipe validate                # every recipe, every environment
python -m fabricops plan --environment dev         # what a setup would do, offline
python -m fabricops setup --environment dev --dry-run
python -m fabricops setup --environment dev        # workspaces, roles, git, connections
```

In a new tenant, run `references sync --environment dev --apply` after the first setup and
commit the result. The report and the pipeline in the demo point at items by id, and those
ids belong to whoever created them last.

The pipelines call the same code through the scripts in `automation/scripts/`. The
variables they expect are listed in the getting-started page.

## Repository layout

```
├── .azure-pipelines/           Azure DevOps pipelines
├── .github/workflows/          the same pipelines for GitHub Actions
├── automation/
│   ├── src/fabricops/          the package: recipe, engine, release, cli
│   ├── scripts/                entry points the pipelines call
│   ├── tests/                  the offline suite and the fake fab
│   └── resources/
│       ├── solutions/demo/     the demo recipe: platform.yml + one overlay per environment
│       ├── parameters/         parameter.yml for fabric-cicd
│       └── BPARules.json       Best Practice Analyzer rules for semantic models
├── documentation/
│   ├── reference/              getting started, the recipe format, feature storage, developing
│   ├── specs/                  one file per epic: the design and the decisions
│   └── research/               platform findings verified against Microsoft Learn
└── solution/                   what Fabric syncs, one directory per workspace
    ├── store/                  lakehouses
    ├── engineering/            ingest, prepare, orchestrate, core
    └── analytics/              model, present
```

`solution/` mirrors the architecture: one workspace, one directory, and the layer
structure on disk is the layer structure in the tenant. The recipe names the directory
each layer syncs from, so a layer can move without anything else changing.

## Pipelines

| Pipeline | Runs when | Does |
| --- | --- | --- |
| `solution_setup` | manual | provisions an environment from the recipe |
| `solution_cleanup` | manual | tears an environment down, with confirmation |
| `solution_release_single_stage` | manual | deploys one environment |
| `solution_release_multistages` | manual | dev, then test, then prod, with gates between |
| `solution_release_octopus` | manual | selective promotion by branch |
| `feature_fabric_branch` | a `feature/*` branch is pushed | creates that branch's workspaces |
| `feature_fabric_cleanup` | a pull request to `main` closes, and nightly | removes the feature workspaces, syncs dev |
| `feature_fabric_reap` | daily | deletes feature workspaces whose branch is gone, reports the rest |
| `pr-validation` | a pull request to `main` | tests, recipes, sanitise scan and naming rules on Linux, BPA on Windows |

Register `pr-validation` as build validation on the `main` branch policy and it becomes
one required check with two jobs. On GitHub the same two jobs are two workflow files,
`pr-validation.yml` and `pr-automation-validation.yml`.

## Feature branches

```bash
git checkout -b feature/engineering/new-support-table
git push -u origin feature/engineering/new-support-table
```

The push creates the workspaces the `engineering` group selects in
`solutions/demo/feature.engineering.yml`, each connected to the branch. Every feature
workspace carries its branch, owner and base workspace in its description, so cleanup
never has to parse a display name. Merge the pull request and the workspaces go, dev is
synced with what changed, and any feature schemas are dropped.

## Documentation

* [Getting started](documentation/reference/getting-started.md): from an empty tenant to
  a working environment, every command, exit codes, and what to check when it breaks.
* [The recipe](documentation/reference/recipes.md): tokens, merge rules, connections,
  items, deployment policy, references, feature recipes.
* [Feature storage](documentation/reference/feature-storage.md): shared versus isolated
  data on a feature branch, and the resolver notebooks use.
* [Developing FabricOps](documentation/reference/development.md): layout, the fake `fab`
  harness, and the conventions an action follows.
* [Specs](documentation/specs/): the design, one epic per file, including the decisions
  that turned out to be wrong.

## Coming from the first version

The rewrite keeps the entry points and the recipe you already have.

* `infrastructure.json` and its environment overlays still resolve, unchanged. The new
  `solutions/<name>/` layout is where the docs and the demo lead, and a repository with
  exactly one solution needs no `--solution` flag.
* `fabric_setup.py`, `fabric_release.py`, `fabric_feature_maintainance.py` and
  `fabric_gitsync_env.py` are still the scripts the pipelines call. They now delegate to
  the `fabricops` package, which is also a command line: `python -m fabricops --help`.
* `solution/` moved from one directory per layer to three: `store/`, `engineering/` and
  `analytics/`. The recipe's `git.directory` per layer is what changed, not the items.
* `credentials.json` is a local fallback only. A flag or an environment variable always
  wins, and pipelines should use their secret store.
* Version 1 is tagged `v1` if you need it.

## Disclaimer

Use of this code is at your own risk. It is a reference and a boilerplate, tested against
one tenant, and Fabric changes under it. Run `--dry-run` first, read the plan, and test in
your own environment before you point it at anything that matters.

## Contributing

Issues and pull requests are welcome. The offline suite has to stay green
(`python -m unittest discover -s automation/tests -t automation`), and anything touching
the tenant should come with a fake `fab` test that proves the commands it sends.

## More

* [Session Archive](https://github.com/gronnerup/SessionArchive): slides and demo
  material from conferences and community events.
* [Peer insights](https://peerinsights.emono.dk): the blog.

## License

MIT. See [LICENSE](LICENSE). The demo data and examples are for learning and development
only.
