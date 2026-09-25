# Getting started with FabricOps

FabricOps provisions Microsoft Fabric from a recipe and deploys solution content with
`fabric-cicd`. This page is the path from an empty tenant to a working environment. For
the recipe format see [recipes.md](recipes.md); for the internals see
[development.md](development.md).

> This has been run end to end against a live tenant — locally, from GitHub Actions and
> from Azure DevOps — across dev, test and production. It is also covered by 517 offline
> tests against a fake `fab` binary, which prove the argv, parsing and control flow. Use
> `--dry-run` first anyway: it costs one command and it prints the whole plan.

## What you need

| | |
| --- | --- |
| Python | 3.12 or newer |
| Dependencies | `pip install -r automation/resources/requirements.txt` (pinned) |
| Fabric CLI | installed by the requirements as `ms-fabric-cli` |
| A capacity | named in the recipe's `defaults.capacity` |
| An identity | a service principal, or an existing `fab auth login` session |
| Repo access | a GitHub PAT, **or** the service principal added to your Azure DevOps organization — see [Git provider access](#git-provider-access) |

Fabric tenant settings the service principal needs: **Service principals can use Fabric
APIs**, and **Service principals can create workspaces, connections and deployment
pipelines**. Creating *tags* additionally needs a tenant admin — see
[Tags](#tags-are-an-admin-job) below.

## Credentials

Three ways, in the order FabricOps prefers them:

```bash
# 1. FAB_SPN_* - the Fabric CLI's own variables. Preferred in pipelines: FabricOps
#    never sees the secret, so it cannot log it.
export FAB_SPN_CLIENT_ID=... FAB_SPN_CLIENT_SECRET=... FAB_TENANT_ID=...

# 2. TENANT_ID / CLIENT_ID / CLIENT_SECRET, which the entry-point scripts read
export TENANT_ID=... CLIENT_ID=... CLIENT_SECRET=...

# 3. an existing session
fab auth login
```

`--client-secret` on the command line also works and is masked in every log sink, but a
shell history is a poor place for a secret.

**Locally**, a credentials file fills whatever the above did not supply:

```
automation/credentials/credentials.<environment>.json    tried first
automation/credentials/credentials.json                  fallback
```

with `tenant_id`, `client_id`, `client_secret`, `github_pat` — the same shape the
`locale/` scripts have always read. It is **only ever a fallback**: a flag or an
environment variable is a deliberate act for this run and always wins. `--no-credentials-file`
ignores it; `--credentials-dir` points elsewhere.

Two warnings it will print, and both are worth acting on: if git tracks the file, treat
everything in it as public and rotate; if it is readable by anyone but you, `chmod 600`.
Pipelines should not use this path at all — they have a secret store.

## Git provider access

The two providers authenticate as **different identities**, and this is the single most
common reason a first `setup` gets all the way to git integration and then stops.

| | GitHub | Azure DevOps |
| --- | --- | --- |
| Connection credential | a **PAT** | the **service principal** |
| Whose access is used | the person who issued the PAT | the service principal's own |
| Set up in | GitHub | Azure DevOps, *before* the first run |
| Recipe | `provider: GitHub`, `owner`, `repository` | `provider: AzureDevOps`, `organization`, `project`, `repository` |

With GitHub you hand Fabric a token that already carries a human's repo access, so if you
can see the repo, the connection works. Azure DevOps has no token in the middle: Fabric
connects **as the service principal**, so the service principal has to be a member of the
Azure DevOps organization in its own right. Nothing in Fabric or in your Entra app
registration grants that — it is a step in Azure DevOps.

### Azure DevOps: add the service principal to the organization

Do this once per organization, before the first `setup`:

1. **Organization settings → Users → Add users**, and add the service principal (search
   for its app registration name). Give it **Basic** access.
2. **Project settings → Teams**, and add the service principal to the relevant team.

Then the connection can be created. FabricOps does that for you from the recipe's
`credentials: { connection: … }` block; you never create it in the portal.

> **Different tenants?** If the Azure DevOps organization lives in a different tenant from
> the app registration, the service principal does not exist in the DevOps tenant and
> cannot be added to it. Register the app as **multitenant**, then create the service
> principal in the DevOps tenant:
> ```bash
> az login --tenant <devops-tenant-id>
> az ad sp create --id <app-client-id>
> ```

### GitHub: the PAT

A classic PAT with `repo`, or a fine-grained PAT with **Contents: read and write** on the
repository. Supply it as `github_pat` in the credentials file or as `GITHUB_PAT`.

A PAT expires, and when it does, every run fails at git integration with
`the git provider rejected the credentials stored in connection '…'`. That is not a
problem with your service principal. Issue a new token, put it where FabricOps reads it,
and run `fabricops connection refresh` — which rewrites the stored credential in place, so
the workspaces stay connected.

## The first run, in order

The order matters on a fresh environment, and it is not the order you might guess.

Every command takes `--solution <name>` (or `FABOPS_SOLUTION`). When the repository defines
exactly one solution, as this one does with `solutions/demo/`, it is optional: the run header
prints which recipe files were read either way.

```bash
export PYTHONPATH=automation/src

# 1. What would happen? Reads only; no credentials needed for the plan itself.
python -m fabricops plan --environment dev

# 2. Workspaces, permissions, git integration, and the connections it can already make.
python -m fabricops setup --environment dev --dry-run
python -m fabricops setup --environment dev

# 3. Items: lakehouses, notebooks, models, reports. This is fabric-cicd.
python -m fabricops release --environment dev --dry-run
python -m fabricops release --environment dev
```

**In a new tenant, run `references sync` after the first release.** The repository ships
working example content, and some Fabric references can only be item ids - which are
per-tenant, so the committed ones cannot resolve for you:

```bash
python -m fabricops references sync --environment dev            # what differs
python -m fabricops references sync --environment dev --apply    # fix it, then commit
```

Without this you get `DiscoverDependenciesFailed` on Present and `MissingDependency` on
Orchestrate, because the report and the pipeline point at items in somebody else's tenant.

**Where the connections come from depends on the environment**, because dev and the
promoted environments get their items by different routes.

* **dev is git-connected.** Items arrive when `setup` syncs the workspace from git, so the
  lakehouse exists by the end of the same run. A `from_item` connection is planned *after*
  its layer's git sync for exactly this reason, and it waits for the SQL endpoint to
  finish provisioning before reading it. One `setup` is enough — `release` is not part of
  the dev loop at all.
* **tst and prd are deployed.** `setup` runs before the items exist, so the connection is
  reported as *waiting* rather than failed, and `release` creates it once publishing is
  done. Re-running `setup` afterwards would also do it; both are idempotent.

If a connection still reports *waiting* after a dev setup, the git sync did not bring the
item in — check the git step above it in the log rather than the connection.

This also matters for a reason that is easy to miss: schema enablement on a lakehouse is
**creation-only**. If something other than `release` creates the lakehouse first, the
`defaultSchema` in its repository definition is silently ignored forever. That is why the
recipe no longer declares lakehouses at all.

## What each command is for

| Command | Does |
| --- | --- |
| `recipe validate [--all]` | Parse and validate. Use in PR validation. |
| `recipe render --environment dev` | The merged, token-substituted recipe the engine will read. |
| `plan --environment dev` | The ordered actions a setup would run. Offline. |
| `plan --environment dev --check` | Also probe the tenant. **Exit 3** when it differs from the recipe. |
| `setup --environment dev` | Provision. `--action delete` tears down (needs `--confirm`). |
| `release --environment tst` | Deploy items with fabric-cicd, layer by layer. |
| `feature create --branch ...` | A workspace per participating layer for a branch. |
| `feature list` | Every feature workspace, with branch, owner and age. |
| `feature reap` | Report stale feature workspaces. `--apply` to delete. |
| `manifest show` | What the last run produced: ids, endpoints, connection ids. |
| `varlib render` / `varlib activate` | Generate variable libraries; set the active value set. |
| `tags sync` / `tags list` | Register declared tags (admin); check registration. |
| `storage report` | Feature schemas left behind in shared lakehouses. |
| `sanitise` / `export` | Check and copy the publishable tree. |
| `solution list` | What this repo defines. |

Global flags work on either side of the subcommand: `fabricops --dry-run setup …` and
`fabricops setup … --dry-run` are the same.

## Dry run means dry run

`--dry-run` performs every **read** and no **write**. That is what makes the plan
accurate: it reads the tenant to decide what it *would* change, rather than assuming.
A dry run therefore still needs to reach Fabric, and will report "needs credentials" for
anything it cannot resolve rather than failing.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success, or a plan was produced |
| 1 | One or more actions failed |
| 2 | Recipe or resolution error — nothing was executed |
| 3 | Drift detected (`plan --check`) |
| 4 | Authentication or permission error |

## Seeing what it actually did

Three levels, and they answer different questions:

| | What it adds |
| --- | --- |
| `--log-level debug` | every `fab` command as it runs, redacted, with exit code and duration |
| `--log-level trace` | the **API response bodies** too, plus the Fabric CLI's own HTTP log |
| `--trace-file <path>` | a JSONL record per command, for a pipeline artefact |

Reach for `trace` when a failure's explanation is only in the response — an error code with
no message, or a generic `BadRequest`. `debug` shows what was asked; only `trace` shows what
came back.

In a pipeline, set the **Log level** parameter when you queue it (Azure DevOps) or
`FABOPS_LOG_LEVEL` (either). An automatically triggered run uses the default, `info`, so
merges and branch pushes stay readable — pick `debug` or `trace` when you re-run one by hand
to investigate.


```bash
# every fab command, redacted, as it runs
python -m fabricops setup --environment dev --log-level debug

# a JSONL trace, one record per command, for a pipeline artefact
python -m fabricops setup --environment dev --trace-file .fabricops/run.jsonl

# what the last run produced
python -m fabricops manifest show
```

Secrets are masked in every sink by default. `--no-redact` exists for local debugging and
should never appear in CI.

## Keeping an environment in step with its branch

Fabric does not pull on its own. Git integration needs an explicit `updateFromGit`, so a
branch moving forward leaves the workspaces behind until something asks them to catch up —
merging a pull request into `main` changes nothing in dev by itself.

That something is:

```bash
python -m fabricops setup --environment dev --only git
# or, the entry point the pipelines call
python automation/scripts/fabric_gitsync_env.py --environment dev
```

`--only git` converges the git state of every layer and nothing else: no roles, no
properties, no connections. Workspaces are included because a git action reads the
workspace id from them, and that is a read when the workspace already exists.

Both CI systems run it on a merge into `main`, in the cleanup workflow, after the feature
workspaces are torn down. It is deliberately **not** conditional on a pull request having
closed — a direct push to `main` moves the branch too, so it syncs dev as well.

Which layers actually pull is still the recipe's decision: `sync_on_commit: false` reports
status without writing, and a layer with `git_disconnect_after_initialize` has nothing to
pull.

### Only the layers the merge touched

Checking is the cost, not pulling: a `git/status` call is a server-side diff and takes
seconds, times every layer, for a merge that touched one folder. So on a merge the sync
takes `--changed-since HEAD~1` and asks only about layers whose git directory changed:

```
Sync default [dev]
only     git
skipped  Core, Store, Ingest, Prepare, Model (unchanged since HEAD~1)
Present     · Git integration... ✔ Updated (pulled from git)
Orchestrate · Git integration... ✔ Updated (pulled from git)
```

A merge that changes nothing under any layer directory — automation, pipelines, docs —
syncs nothing and says so. If git cannot diff against the ref (shallow clone, force push,
first run) it checks every layer rather than none, and says that too.

**The gap, and what closes it.** A layer can need a sync without its folder changing: a
report deferred *waiting for its model* becomes syncable when the model arrives in a
commit that touched only the model's folder. The merge-time sync would not retry it. So the
same pipeline also runs a **full sync nightly**, and a manual run always checks everything.
The gap is at most a day, and never silent.

## Drift, on a schedule

```bash
python -m fabricops plan --environment prd --check   # exit 3 when the tenant has moved
```

A drift check is a dry run read differently, so it cannot disagree with what a real run
would do. Bare `plan` stays offline, so reviewing a recipe change in a PR needs no
credentials.

## Feature branches

```bash
python -m fabricops feature create --branch feature/peer/add-orders
python -m fabricops feature list
python -m fabricops feature reap --ttl-days 14           # report
python -m fabricops feature reap --ttl-days 14 --apply   # delete
```

Feature workspaces identify themselves in their description, so cleanup never parses a
display name. `reap` refuses to delete when it cannot tell: an unreachable git provider is
"do not know", not "the branch is gone", and a workspace with no timestamps to age against
is kept. The scheduled workflow reports only; deleting takes a manual dispatch.

Tag a workspace `Retain:true` to make it un-reapable.

## Feature workspaces nobody came back for

The cleanup on a merged pull request handles the tidy path. `feature reap` catches the
rest: a branch deleted without a PR, an experiment abandoned, a PR closed while the agent
was down. It reads the same inventory every feature workspace carries in its description,
and decides per workspace:

| Signal | Means | Deleted unattended? |
| --- | --- | --- |
| `Retain:true` tag | keep, whatever else is true | never |
| **branch gone** | the git provider says the branch no longer exists | **yes** — a deleted branch is a fact |
| **ttl** | older than `--ttl-days` (14) since creation or last sync, branch still there | only with `--apply-when both` |
| unreachable provider | "do not know", never "gone" | never |

Both pipelines run it **daily** at 03:30 UTC, deleting on *branch gone* and reporting the
age-based candidates for a human. `--apply-when both` deletes those too. A reaped feature
takes its schemas with it: the same `drop_on_teardown` a `feature delete` performs.

The branch probe needs a token: `GITHUB_TOKEN` on GitHub, and the pipeline's own
`System.AccessToken` on Azure DevOps (or `AZURE_DEVOPS_TOKEN` as a PAT). Without one the
reap falls back to age alone and says so.

## Tags are an admin job

Fabric tags can only be *created* by a tenant admin, are limited to 40 characters and 10
per object, and are not captured in git. FabricOps therefore keeps a committed tag
registry and treats tags as a query convenience, never as the mechanism anything depends
on. `tags sync` needs an admin identity; `tags list` does not.

## When something goes wrong

| Symptom | Likely cause |
| --- | --- |
| `workspace '…' does not exist - run \`fabricops setup\` first` | `release` before `setup`. |
| A connection reports *waiting for …* | Expected before the first `release`. |
| Tables are not found after enabling schemas | The lakehouse pre-dates the change. Schemas are creation-only; delete it and let `release` recreate it. |
| `plan --check` always reports drift on one item | A property whose live value is formatted differently from the recipe's. Compare with `--log-level debug`. |
| `fabric-cicd is not installed` | `pip install -r automation/resources/requirements.txt`. |
| `The Fabric artifact '...' is not found` during a git sync | The **deployed** item still references an id that no longer resolves, and Fabric cannot update an item whose current state is broken. Git being correct is not enough. Delete the item from the workspace and sync again to recreate it. |
| An item shows as *Modified* in a workspace on every run, and you never touched it | Its `.platform` ends with a trailing newline. Fabric writes these with none, and git integration compares bytes, so the item never looks synced. Strip it — `.platform` files must have no final newline and LF endings. A test enforces this in this repo. |
| A branch relation reports *the workspace already existed, so its relation was left as it is* | Expected on any re-run. Fabric creates this relation as part of branching out — a create-time operation — and offers no API to read the relations a workspace already has, so a re-run cannot tell an existing relation from a missing one. Re-creating an existing one returns `BadRequest: An error occurred in the Entity Framework`, which is indistinguishable from a real fault, so FabricOps attempts it once, when it creates the workspace. The relation only drives the portal's workspace tree and breadcrumbs. |
| `[WorkspaceMigrationOperationInProgress]` | Fabric is moving the workspace (capacity or backend) and refuses writes while it does. Nothing to do with your deployment. It clears on its own — wait and run again. Not retried automatically: the service reports `isRetriable: false` and a migration outlasts any sensible backoff. |
| `[MissingInitializationStrategy]` on `initializeConnection` | Both the workspace and the branch have content, and Fabric will not choose. Normal on the **second** run of a layer that disconnects after initialising: the first run left the workspace populated. FabricOps sends the strategy from the recipe's `conflict_resolution` (`prefer_remote` → `PreferRemote`), so this only surfaces where that is `stop`. |
| `WorkspaceAlreadyConnectedToGit` | Something tried to connect a workspace that is already connected. `gitConnectionState` has **three** values — `NotConnected`, `Connected`, `ConnectedAndInitialized` — and code that tests for `Connected` alone misses the third. FabricOps handles all three; if you have extended it, check yours does too. |
| `Py4JJavaError: "fabric" is not a valid resource` in a notebook | `notebookutils.credentials.getToken` accepts `storage`, `pbi`, `keyvault`, `kusto`. Use `pbi` — it covers the Fabric REST API. |
| A pipeline queues or fails on an F2 | Give every notebook activity the same `sessionTag` so they share one Spark session instead of starting one each. |
| `Unexpected line type: ReferenceObject` opening the semantic model | `ref table` and model-level `annotation` lines belong at depth 0 in `model.tmdl`, not indented under `model`. |
| `[Unauthorized]` or `[InvalidInput]` creating an **AzureDevOps** source control connection | The service principal is not a member of the Azure DevOps organization. Fabric connects as the service principal itself, so this is a step in Azure DevOps, not in Fabric — see [Git provider access](#git-provider-access). |
| An Azure DevOps connection fails and the app registration is in another tenant | The service principal does not exist in the DevOps tenant. Register the app as multitenant and `az ad sp create --id <client-id>` against that tenant. |
| A failure whose only message is a progress line, like `Creating a new Connection...` | The Fabric CLI writes errors to **stdout**, interleaved with progress output, and the real reason is the line beginning `x `. Re-run with `--log-level debug` to see the command's full output. |
| `the git provider rejected the credentials stored in connection '...'` | The PAT in the connection has expired or lost repo access — not a problem with your service principal. Issue a new token, put it in the credentials file or `GITHUB_PAT`, then `fabricops connection refresh`. |
| The developer was not made admin of their own feature workspace | Expected, unless you asked for it. **Neither** CI system has an Entra object id to offer: GitHub's `GITHUB_ACTOR_ID` is GitHub's own numeric user id, and Azure DevOps' `BUILD_REQUESTEDFORID` is an *Azure DevOps* identity id — GUID-shaped, so it looks plausible, but not what `fab acl set -I` wants (*Entra identity objectId*). So FabricOps does not guess: without `FABOPS_DEVELOPER_OBJECT_ID` or `--developer-object-id`, only the recipe's own `permissions` are applied. Usually that is enough — the developer is in the admin group the recipe already grants. |
| `capacity '...' does not exist, or this identity cannot see it` | The hint lists what the identity *can* see. Usually the recipe names a capacity that was renamed or whose trial expired. |
| `[AuthenticationFailed] Failed to get access token` | The `fab auth login` session has expired. `fab auth logout && fab auth login`, or set the `FAB_SPN_*` variables. The run stops at the first action rather than repeating this 27 times. |
| Exit 4 | Authentication, or the identity lacks a Fabric tenant setting or workspace Admin. |
