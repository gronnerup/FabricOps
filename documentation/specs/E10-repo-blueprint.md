# E10 — Repository blueprint: folders, items, workspace folders, git providers

**Goal:** a repo layout that *is* the architecture blueprint — one that scales from one
workspace to 3 environments × 7 layers, and that the framework can read without
convention guessing. This spec is the engineering counterpart to blog post #3.

## Principles

1. **One workspace = one git directory.** Fabric git integration connects a workspace to
   *one* directory of a repo — a platform constraint, not a preference. So the tree is
   shaped like the **three-layer floor** (`store/`, `engineering/`, `analytics/`) with a
   responsibility folder for every leaf beneath. A tier is then only a choice of which
   folders get a workspace bound to them: Minimum binds the three, Enterprise binds every
   leaf, and the repo never changes between them. A flat directory per layer only worked at
   the Enterprise tier, which contradicted the "start small, scale up" promise (credit:
   Sebastian, in the comments on Episode 3).
2. **The repo mirrors the platform, the recipe names the mapping.** `git.directory` per
   layer in the recipe (today's `git_directoryName`) is the only binding between repo
   and workspace; no implicit "folder name == layer name" magic.
3. **Content and control plane are separated.** `solution/` is what gets deployed into
   workspaces; `automation/` is how it gets there. Nothing in `solution/` should need to
   know which environment it will land in.
4. **Generated artefacts are visibly generated.** `automation/generated/**` for
   parameter files, deploy configs, variable libraries — never mixed with hand-authored
   recipes.

## Proposed layout

```
├─ fabricops.yml                        # repo-level defaults (E02)
├─ solution/                            # workspace content, shaped like the 3-layer floor
│  ├─ store/
│  ├─ engineering/                      # a folder at Minimum/Basic; folder-only at Enterprise
│  │   ├─ ingest/  prepare/  orchestrate/  core/
│  ├─ analytics/                        # a folder at Minimum; folder-only above
│  │   ├─ model/  present/
│  │       └─ <Item Name>.<ItemType>/   # .platform + definition (git format v2)
│  └─ _shared/                          # templates, not deployed
├─ automation/
│  ├─ resources/
│  │  ├─ solutions/<solution>/          # recommended layout (E02 order 1)
│  │  │   ├─ platform.yml  platform.dev.yml  platform.tst.yml  platform.prd.yml
│  │  │   └─ feature.yml   feature.<developer>.yml
│  │  ├─ environments/                  # legacy/flat layout (E02 orders 2–3) — still supported
│  │  ├─ schemas/solution.schema.json   # E01
│  │  └─ tags/registry.yml              # E05
│  ├─ generated/<solution>/             # deploy.yml, parameter.yml, *.VariableLibrary (E06/E09)
│  ├─ src/fabricops/                    # the package (was scripts/modules)
│  │   ├─ recipe/ (loader, schema, merge, model)  engine/ (planner, executor, handlers)
│  │   ├─ fabric/ (cli runner, api, types)        obs/ (logging, redaction, manifest)
│  │   └─ cli.py                        # `fabricops <verb>`
│  ├─ scripts/                          # thin entry points kept for pipeline compatibility
│  └─ tests/                            # unit + recipe fixtures + golden plans (E11)
├─ .github/workflows/  .azure-pipelines/
└─ documentation/                       # specs, research, reference (generated docs)
```

Notes:
* `automation/scripts/locale/*` (local dev runners) become `fabricops` CLI invocations or
  documented VS Code launch configs; the folder can stay during transition.
* `automation/credentials/credentials.json` should not exist in a public repo —
  replace with env vars / `.env.example` + OIDC (E11).

## Workspace folders (item folders)

Fabric supports folders inside a workspace, mirrored in git (structure retained up to
**10 levels**; empty folders are not committed; empty git folders are deleted; workspace
folders are *not* auto-deleted when emptied)
([git-integration-process](https://learn.microsoft.com/fabric/cicd/git-integration/source-code-format), [basic concepts](https://learn.microsoft.com/fabric/cicd/git-integration/git-integration-process)).

Therefore:
* The recipe can declare folders (`folder: Zones/Curated` on an item, E03), and the
  engine creates them (`fab mkdir <ws>.Workspace/<folder>`, and `folderId` is accepted by
  item creation).
* Because folder structure participates in git sync, **folder changes are content
  changes** — call this out in the blog: connecting a workspace that already has folders
  to an empty git folder produces uncommitted changes, and updating first overwrites the
  workspace structure. Recommended flow: commit folder structure first, or check out a
  new branch.
* **Placement is enforced in CI.** Fabric drops a new item at the workspace root, which on
  a workspace bound to `engineering/` lands it in no responsibility at all.
  `test_solution_layout.py` fails the build on any item directly under `engineering/` or
  `analytics/`, so the convention is a guarantee and scaling a tier stays a rebind.
* Decision to make: use **layers (workspaces)** for isolation and security boundaries,
  **folders** for readability inside a layer. Do not use folders as a substitute for
  layers when the boundary needs different permissions, capacity or git branch.

## Git provider considerations

| | GitHub | Azure DevOps |
| --- | --- | --- |
| Connection credential | PAT-based connection (`credentialDetails.type=Key`) | SPN-capable connection (`AzureDevOpsSourceControl`) |
| Repo URL shape | `https://github.com/{owner}/{repo}` | `https://dev.azure.com/{org}/{project}/_git/{repo}` |
| Automation identity | PAT must belong to an identity with repo write | SPN works end-to-end (preferred for unattended) |
| Feature-branch triggers | `feature_fabric_*` workflows | `feature_fabric_*` pipelines |
| Practical guidance | fine for demos/OSS; PAT rotation is the weak spot | preferred for enterprise: SPN + branch policies + environments |

Both are already supported by FabricOps; the spec change is to make the provider a
**recipe-level choice with validated shapes** (no URL string building in flow code) and
to document the credential model per provider.

## Naming conventions (documented, validated)

| Object | Pattern | Notes |
| --- | --- | --- |
| Workspace | `{solution} - {layer} [{environment}]` | recipe token pattern; validated for Fabric workspace naming rules |
| Feature workspace | `*{topic} ({layer})` | leading `*` sorts feature workspaces together in the UI |
| Connection | `{solution}-{purpose} [{environment}]` | |
| Item | `PascalCase` / domain-specific, no environment suffix | environment lives in the workspace, never in item names |
| Tag | `Key:Value` (≤40 chars) | E05 |
| Branch | `feature/{developer}/{topic}` | E08 |

## User stories

**E10-S1 — Blueprint documented** — AC: `documentation/reference/repo-blueprint.md` with
the layout, the one-workspace-one-directory constraint, folders vs layers guidance, and
naming table (blog post #3 asset).
**E10-S2 — Folder support in the recipe** — AC: `folder:` on items creates workspace
folders and sets `folderId` at creation; 10-level limit validated; git-sync caveat
documented.
**E10-S3 — Repo restructure** — AC: package moved to `automation/src/fabricops`, thin
entry points retained, pipelines updated, no import breakage.
**E10-S4 — Provider validation** — AC: provider-specific required fields validated in the
schema; URL construction centralised and unit-tested for both providers.
**E10-S5 — Credentials out of the repo** — AC: `credentials.json` removed from the
repo/history plan documented; `.env.example` + OIDC-based pipeline auth documented.
