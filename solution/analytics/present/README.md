# Present

Reports. Deployed to the `Present` workspace.

`Rebrickable.Report` is a thin report: it carries no model, and connects live to
`Rebrickable.SemanticModel` in the **Model** workspace. That cross-workspace reference is
why `definition.pbir` uses `byConnection` rather than `byPath` - fabric-cicd can only
resolve a relative `byPath` to a model deployed in the *same* workspace, and ours is not.

The semantic model id in `definition.pbir` is a placeholder in Git. It is rewritten per
environment by `automation/resources/parameters/parameter.yml`.

## The shape git integration accepts

`definition.pbir` has two forms in the wild, and only one survives a git sync:

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
  "version": "4.0",
  "datasetReference": {
    "byConnection": {
      "connectionString": "Data Source=powerbi://api.powerbi.com/v1.0/myorg/<workspace>;initial catalog=Rebrickable;access mode=readonly;integrated security=ClaimsToken;semanticmodelid=<guid>"
    }
  }
}
```

That is what Power BI Desktop writes: schema **2.0.0**, one `connectionString`, with the
model id in a `semanticmodelid=` segment. The Fabric items API also documents a
`byConnection` with six separate keys (`connectionString`, `pbiModelDatabaseName`,
`pbiModelVirtualServerName`, and so on). Git integration rejects that one. If a report
will not sync, compare it against a `.pbip` that Desktop itself saved.

## Why the first deployment into a new tenant needs a second pass

The model id above is a real id, and item ids are per-tenant, so a committed one cannot
resolve anywhere else. The report is marked `blocks_sync` in the recipe, so a layer whose
references have not been resolved yet reports **waiting** instead of failing the run.
Deploy `Model` first, then `fabricops references sync --environment dev --apply` writes the
ids this tenant actually has, and `Present` syncs on the next run.
