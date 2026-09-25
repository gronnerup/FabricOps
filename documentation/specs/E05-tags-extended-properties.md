# E05 — Tags as extended/custom properties for automation

**Goal:** use Fabric tags as the framework's *extended property* store — the metadata
that automation, deployment and cleanup can read back from the platform itself — and
know exactly where that idea breaks.

## What tags actually are (verified, Aug 2026)

| Property | Reality | Source |
| --- | --- | --- |
| Definition scope | Tags are **defined** by a Fabric admin (tenant scope) or Fabric/domain admin (domain scope). Non-admins can only *apply* existing tags | [tags-define](https://learn.microsoft.com/fabric/governance/tags-define) |
| Applied to | Workspaces **and** items | [tags-overview](https://learn.microsoft.com/fabric/governance/tags-overview) |
| Shape | A tag is a **flat display name string** (≤40 chars). There is no key/value pair, no free-text value per object | [bulk-create-tags](https://learn.microsoft.com/en-us/rest/api/fabric/admin/tags/bulk-create-tags) |
| Limits | 10,000 tags per tenant; **max 10 tags per workspace and 10 per item** (counted independently); unlimited tagged objects | tags-overview |
| Apply API | `POST /v1/workspaces/{wsId}/applyTags` (workspace admin, **preview**) · `POST /v1/workspaces/{wsId}/items/{itemId}/applyTags` (contributor+) — both take **tag IDs (GUIDs)**, not names | [apply-workspace-tags](https://learn.microsoft.com/en-us/rest/api/fabric/core/workspaces/apply-workspace-tags), [apply-tags](https://learn.microsoft.com/en-us/rest/api/fabric/core/tags/apply-tags) |
| Create/list API | `POST /v1/admin/tags/bulkCreateTags`, `GET /v1/admin/tags` — **admin APIs**, `Tenant.ReadWrite.All` | bulk-create-tags |
| Rate limit | **25 requests per minute per principal** on the tag APIs | apply-tags / bulk-create-tags |
| Git / item definition | Tags are **not** part of `.platform` or any item definition → they are not source-controlled and do not travel with git integration or `fabric-cicd` | [source-code-format](https://learn.microsoft.com/fabric/cicd/git-integration/source-code-format) |
| Deployment pipelines | Not carried by deployment pipelines; must be re-applied per environment | tags-overview |
| Fabric CLI | **No tag support in `fab` 1.6.1** — tags must go through `fab api` | verified in the installed CLI (no tag command/paths) |
| Discovery | Tag icons/search can take *several hours* to appear in the portal; scanner API returns tag IDs, resolved via admin List Tags | tags-overview |

### Consequences for FabricOps

1. **Tags are labels, not properties.** With no value field, "extended properties" must
   be encoded in the *name*. **Decision 6:** FabricOps uses `Key:Value` names with a
   fixed managed-key list, so tags can be parsed back into properties:

   | Key | Applies to | Example | Used by |
   | --- | --- | --- | --- |
   | `ManagedBy` | workspace, item | `ManagedBy:FabricOps` | inventory, reconciliation scope |
   | `Solution` | workspace, item | `Solution:Confidence` | deployment scoping, reaping |
   | `Env` | workspace | `Env:dev` | inventory, cost attribution |
   | `Layer` | workspace, item | `Layer:Store` | inventory, deployment scoping |
   | `Lifecycle` | workspace | `Lifecycle:Feature` | TTL reaping (E08) |
   | `Owner` | workspace | `Owner:peer` | reaping, chargeback |
   | `Branch` | workspace | `Branch:feature-peer-add-orders` | reaping (truncated to fit 40 chars) |
   | `Retain` | workspace, item | `Retain:true` | teardown protection (E03) |

   Any other key is treated as a user tag: never applied, never removed by FabricOps.
   Keys are fixed because the 10-tag budget is small — never generate one tag per
   arbitrary value (e.g. never `Cost:1234`).
2. **Creation needs a tenant admin.** A deployment service principal will typically
   *not* have `Tenant.ReadWrite.All`. Therefore: tag *definition* is a separate,
   admin-run, rarely-executed operation; tag *application* is part of every run.
3. **Names must be resolved to GUIDs.** Every apply needs a name→ID lookup, which is an
   admin API. Solution: a **tag registry** artefact (see below) so the deployment
   identity never needs admin rights at apply time.
4. **Tags are not a config source of truth.** Because they are not in git and not in
   item definitions, tags must be treated as a *projection* of the recipe, reconciled
   on every run — never as input to the recipe.

## Design

### Tag registry

`automation/resources/tags/registry.yml` (committed, generated + hand-curated):

```yaml
apiVersion: fabricops/v1
kind: TagRegistry
scope: { type: Tenant }          # or { type: Domain, domain: Data Platform }
tags:
  - { name: "ManagedBy:FabricOps", id: 97dd1d38-… }
  - { name: "Solution:Confidence", id: 41d5b790-… }
  - { name: "Env:dev",             id: 7b8e5ada-… }
  - { name: "Layer:Store",         id: … }
  - { name: "Lifecycle:Feature",   id: … }
  - { name: "Retain:true",         id: … }
```

* `fabricops tags sync` (admin identity, run rarely): reads the recipes, computes the
  required tag set, creates missing tags via `bulkCreateTags`, and **writes back the
  IDs** into the registry for commit. Batched to stay inside 25 req/min.
* `fabricops tags apply` (deployment identity, every run): resolves names from the
  committed registry — no admin API call — and applies to workspaces/items.
  Unknown name → actionable error telling the operator to run `tags sync`.
* `--tags-mode off|apply|reconcile` where `reconcile` also *removes* FabricOps-managed
  tags that are no longer in the recipe (only tags present in the registry are ever
  removed; foreign tags are never touched).

### Recipe surface

```yaml
defaults:
  tags: ["ManagedBy:FabricOps", "Solution:{solution}"]
layers:
  Store:
    tags: ["Layer:Store", "Env:{environment}"]
    items:
      - { name: Curated, type: Lakehouse, tags: ["Zone:Curated"] }
```

Tags are unioned (defaults → layer → item), token-substituted, de-duplicated, and then
**budget-checked at validation time** (>10 for any object = validation error, with the
offending list printed). Feature workspaces additionally get
`Lifecycle:Feature`, `Owner:{developer}` and `Branch:<sanitised>` where the branch fits
in 40 chars (E08).

### What tags then unlock

| Scenario | How |
| --- | --- |
| Cleanup/TTL of feature workspaces | list workspaces, filter `Lifecycle:Feature` + `Owner:*`, delete those whose branch no longer exists (E08) — no naming-convention parsing |
| Protecting resources from teardown | `--keep tags=Retain:true` in E03 teardown |
| Deployment scoping | "deploy every workspace tagged `Solution:Confidence` + `Env:tst`" without re-deriving names |
| Cost/chargeback and governance reporting | scanner API returns tags per workspace/item; join to capacity metrics |
| Inventory/drift | `fabricops plan` compares tag projection vs. live, reports untagged or foreign-tagged objects |

### Where tags must **not** be used

* As a value store for anything that changes per run (ids, endpoints, timestamps,
  costs) — that is the run manifest (E03) and/or a variable library (E06).
* As a permission or security boundary — tags are metadata, not access control.
* As the mechanism carrying config into an item at runtime — items cannot read tags.

## User stories

**E05-S1 — Declare tags in the recipe**
AC: `tags:` supported at defaults/layer/item level with union + token substitution;
validation fails above 10 per object or on names >40 chars.

**E05-S2 — Admin-run tag sync**
AC: `fabricops tags sync` creates missing tenant/domain tags in batches, respects the
25 req/min limit, writes IDs back to the registry, and is a no-op when nothing changed.

**E05-S3 — Apply without admin rights**
AC: `fabricops tags apply` uses only the committed registry + `applyTags`; run fails
with a clear message if the registry is stale.

**E05-S4 — Feature workspace ownership tags**
AC: feature workspaces are tagged `Lifecycle:Feature`, `Owner:{developer}`,
`Solution:*`; cleanup uses tags instead of name parsing.

**E05-S5 — Tag-driven teardown protection**
AC: `Retain:true` on a workspace or item causes teardown to skip it and report it.

**E05-S6 — Tag reconciliation report**
AC: `fabricops tags report --environment dev` lists every managed object with its
current vs. desired tags and exits 3 on difference.

## Risks / open questions

* `applyTags` for workspaces is **preview** — pin behind a feature flag
  (`features.workspace_tags: true`) so a breaking change doesn't break setup.
* Domain-scoped tags disappear if a workspace is moved between domains; prefer
  **tenant-scoped** tags for anything automation depends on.
* Propagation delay (hours) means tags are unsuitable for a *synchronous* read-back
  check right after apply — verify via the API response, not via search.
