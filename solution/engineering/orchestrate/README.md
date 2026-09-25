# Orchestrate

Pipelines. Deployed to the `Orchestrate` workspace.

`Load Rebrickable` chains the three notebooks in order: Ingest, then Base, then Curated.
There is no semantic model refresh at the end - the model is Direct Lake, so it picks up
the new Delta files without one.

The notebooks live in the Ingest and Prepare workspaces, so the pipeline references them
across workspace boundaries:

* `notebookId` holds each notebook's `logicalId` from its `.platform`. fabric-cicd swaps
  those for the deployed item ids, which is why Ingest and Prepare must deploy before
  Orchestrate.
* `workspaceId` holds a placeholder resolved per environment by
  `automation/resources/parameters/parameter.yml`.

## Why the activities ship inactive

All three activities carry `"state": "Inactive"`.

A notebook activity's `notebookId` and `workspaceId` are dependencies Fabric resolves when
git syncs the item, and item ids are per-tenant - so the committed ones cannot resolve in a
tenant that has not deployed this solution before. An active activity therefore fails the
whole sync with `MissingDependency`.

An inactive one validates, so the pipeline lands. `fabricops references sync --environment
dev --apply` then writes the ids this tenant actually has, and you activate the activities
once - in the portal, or by removing `state` here and committing.

`onInactiveMarkAs: Succeeded` means a run of the pipeline before that point reports success
rather than skipping into a failure.

## The session tag

All three activities carry the same `sessionTag`:

```json
"typeProperties": { "notebookId": "...", "workspaceId": "...", "sessionTag": "FabricOps_Demo" }
```

Notebook activities with a matching tag reuse one Spark session instead of starting three.
On a small capacity - F2, where the demo is meant to be runnable - starting a session per
activity exhausts the available cores and the pipeline fails or queues. Keep the tag
identical across the three; the value itself does not matter.
