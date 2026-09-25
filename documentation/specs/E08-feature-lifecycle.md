# E08 — Feature workspace lifecycle hardening

**Goal:** feature environments that are correct, discoverable, cheap and self-cleaning.

## Problem (today)

`fabric_feature_maintainance.py` works but:

* workspace identity is a **name pattern** (`*{feature_name} ({layer_name})`) and
  cleanup parses names — fragile once a branch contains `/` or non-Latin characters;
* the created workspace has **no relation** to its base workspace, so Fabric's UI does
  not show it as a branched workspace and "related branches" navigation is empty;
* branch→layer filtering is a string convention (`filter_layers_by_branch`) with no
  validation and no way to request extra layers;
* git connect/initialize/update is duplicated from `fabric_setup.py`;
* no TTL: a merged/deleted branch leaves its workspaces (and now, storage, E07) behind
  until someone runs the cleanup workflow;
* permissions are workspace-wide from `feature.json`; the *developer who created the
  branch* is not automatically an admin of their own feature workspace.

## Design

### 1. Register the branch relation

After creating the feature workspace and connecting it to the branch, call the preview
**Create Workspace Relation** API so Fabric treats it as a branched workspace:

```
POST /v1/workspaces/{featureWorkspaceId}/git/workspaceRelations
{ "relatedWorkspaceId": "<dev layer workspace id>", "relationType": "Base" }
```

Verified constraints ([create-workspace-relation](https://learn.microsoft.com/en-us/rest/api/fabric/core/git/create-workspace-relation)):
admin on the branch workspace + contributor on the base; SPN supported; both workspaces
must share the same git **root directory**; the base cannot itself be a branch; a
workspace can't already have branches. Errors are well-defined
(`WorkspaceRelationRootDirectoryMismatch`, `WorkspaceRelationBaseIsBranch`, …) → map
them to actionable messages and treat "already exists" as success.

Payoff: the branched-workspace UI (workspace tree, breadcrumbs, related branches) works
for automation-created feature workspaces, i.e. the automated flow finally looks like
the native "Branch out" flow.

### 2. Identity by tag, not by name

Feature workspaces are tagged (E05): `Lifecycle:Feature`, `Owner:{developer}`,
`Solution:{solution}`, `Layer:{layer}` and, where it fits in 40 chars, `Branch:<sanitised>`.
Cleanup and inventory query tags; naming stays cosmetic.

The branch↔workspace mapping is additionally recorded in the run manifest and in the
workspace **description** (`FabricOps: solution=… layer=… branch=… developer=… created=…`),
which is API-readable and survives renames.

### 3. Declarative layer selection

```yaml
kind: Feature
branch:
  pattern: "feature/{developer}/{topic}"      # documented, validated
  layer_from_branch: true                     # feature/prepare/x → layer Prepare
layers:
  Prepare:  { always: false }
  Ingest:   { always: false }
  Model:    { always: true }                  # was: always_provision
  Present:  { always: true }
```
Plus `--layers Prepare,Ingest` to override per run, and validation that every branch
pattern segment resolves. Unmatched layer name in a branch = warning + provision all
(current behaviour), but logged explicitly.

### 4. TTL and orphan cleanup

`fabricops feature reap` (scheduled workflow, dry-run by default):

* lists workspaces tagged `Lifecycle:Feature` for the solution;
* resolves each one's branch from tag/description/manifest;
* deletes when: branch no longer exists on the remote, **or** last git sync older than
  `ttl_days` (default 14), **or** the PR is merged/closed (GitHub/ADO API);
  *(implemented: branch-gone and TTL. The branch probe is GitHub-only; on Azure DevOps the
  reap falls back to age alone and says so.)*
* skips `Retain:true`; reports what it would delete; `--apply` to execute;
* also reaps E07 storage artefacts owned by the same feature.

### 5. Shared git plumbing

Git connect / initialize / update-from-git / disconnect / status becomes one module used
by both platform and feature paths (E03 resource types), including the current niceties:
`git_synchronize_on_commit`, `git_disconnect_after_initialize`, and the
"already up to date" short-circuit.

### 6. Developer as admin of their own feature workspace

Resolve the initiating identity (`GITHUB_ACTOR` / `BUILD_REQUESTEDFOR*`, or
`--developer-object-id`) and assign it the Admin role on the created feature workspaces,
so the developer can branch-switch and manage their own environment without a platform
admin. Falls back to the recipe's group permissions when the identity can't be resolved
(with a warning).

## User stories

**E08-S1 — Branched workspace relation** — AC: relation created after git connect;
"already exists" tolerated; documented failure mapping; visible in the Fabric UI.

**E08-S2 — Tag-based feature inventory** — AC: `fabricops feature list` shows
workspace, layer, branch, developer, last sync, age — sourced from tags/description, no
name parsing.
*Done, but description-first rather than tag-first. Tags need a tenant admin to create and
a recipe may declare none at all — `_feature_tags` returns nothing when the recipe has no
base tags — so tags cannot be the mechanism. The workspace description is stamped at
creation and is always there.*

**E08-S3 — TTL reap** — AC: `fabricops feature reap` dry-run report + `--apply`; deletes
only tagged, non-retained, branch-gone/expired workspaces; runs on a schedule.
*Done. Two safety rules that are not in the AC but should be: a branch probe returning
"cannot tell" (offline, no token, rate limited) never counts as "branch is gone", and a
workspace with no created or last-sync time to age against is kept rather than reaped. The
scheduled workflow reports only; deleting takes a manual dispatch with `apply: true`,
because a cron job that silently deletes workspaces is one you learn about the hard way.
PR-merged detection is not implemented — branch-gone covers the same ground for a repo
that deletes branches on merge, and needs no PR API.*

**E08-S4 — Declarative layer selection** — AC: `always:`/`--layers` implemented;
branch pattern validated; layer resolution logged.

**E08-S5 — Developer is admin of their feature workspace** — AC: initiating identity
gets Admin; fallback documented.

**E08-S6 — Shared git module** — AC: one implementation used by platform + feature
paths; feature sync-on-commit behaviour unchanged.

**E08-S7 — Feature storage lifecycle** — AC: E07 strategy resources created with the
workspace and reaped with it.

> **Decision, 2026-09-23.** A group recipe's `layers` is a *selection*, not an overlay
> union. The natural way to write a group - list the layers you want - previously yielded
> every layer in `feature.json`, because mapping merges are unions. A group now names its
> layers and inherits their definitions; `always: true` applies only on the layer-name
> path; developer overlays keep merging and never select. Two groups ship by default:
> `engineering` (Ingest, Prepare) and `analytics` (Model, Present).

> **Decision, 2026-09-25.** Reaping runs daily and deletes unattended only on the *branch
> gone* signal; age-based candidates are reported for a human unless `--apply-when both`.
> Azure DevOps has a branch probe now (refs API, exact ref match, pipeline token), so it
> no longer falls back to age alone. A reaped feature drops its schemas through the same
> teardown a `feature delete` runs.
