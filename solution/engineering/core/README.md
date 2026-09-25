# Core

Control-plane data: the metadata store a metadata-driven ingestion framework reads from.

**Nothing is published from this layer at the moment.** The workspace is created by
`fabricops setup` with its permissions and git integration, but the recipe tells the release
to publish no items:

```yaml
Core:
  deploy:
    item_types_in_scope: []
```

The reason is that the demo does not use it yet. `Metadata.SQLDatabase` contains one table
called `dummy`, nothing reads it, and publishing it would leave an empty Fabric SQL database
consuming capacity - while the dacpac toolchain it needs (dotnet SDK, a sqlproj build,
sqlpackage) is the largest thing in the pipelines and the reason `conn.txt` exists. On the
small capacities this solution is meant to be runnable on, that is a poor trade for a table
called dummy.

Everything needed to bring it back is still here.

## Putting it back

1. Remove the `deploy` block from `Core` in the platform recipe
   (`automation/resources/solutions/demo/platform.yml`).

2. Restore the connection to its SQL endpoint, under `defaults.connections`:

```yaml
- name: Confidence-MetadataDB [{environment}]
  type: SQL
  auth: ServicePrincipal
  from_item:
    layer: Core
    name: Metadata
    type: SQLDatabase
```

3. Nothing else. The dacpac steps in both providers' build and release pipelines are
   conditional on a `.sqlproj` being present, so they start running again on their own.

`Metadata.sqlproj`, the `.platform`, `dbo/Tables/`, and
`automation/scripts/generate_connection_string.py` are all untouched.
