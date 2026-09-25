# Fabric notebook source

# METADATA ********************

# META {
# META   "kernel_info": {
# META     "name": "synapse_pyspark"
# META   },
# META   "dependencies": {}
# META }

# MARKDOWN ********************

# <center>
# 
# # **FabricOps - Utilities**
# 
# </center>
# 
# ### Purpose
# Shared helpers for notebooks in this solution. Include it in a cell of its own:
# 
# ```
# %run fabricops_util
# ```
# 
# ### Why a copy per layer
# `%run` only resolves notebooks **in the same workspace**, and each layer is its own
# workspace, so this notebook exists once per notebook-bearing layer. The copies are
# byte-identical and a test fails the build if they drift
# (`automation/tests/test_solution_layout.py`). When the logic settles it becomes a wheel
# in an Environment item and the copies collapse.

# MARKDOWN ********************

# ### Imports

# CELL ********************

import re
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import sempy.fabric as fabric
from pyspark.sql import DataFrame

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Addressing
# Lakehouses are addressed explicitly. Store is its own workspace, so a reference from
# here is cross-workspace and would never rebind on promotion if it relied on an attached
# default lakehouse.

# CELL ********************

# The shared schema every lakehouse in this solution uses. Defined here rather than in the
# feature-storage cell below because it is a default argument for helpers defined earlier.
DEFAULT_SCHEMA = "dbo"


def lakehouse_tables(name: str, workspace_id: str | None = None, schema: str | None = None) -> str:
    """The `Tables` path of a lakehouse, resolved by name.

    Falls back to the attached lakehouse when no workspace is given, which is what makes
    the notebooks usable interactively without changing any code.
    """
    if not workspace_id:
        return f"Tables/{schema}" if schema else "Tables"
    item_id = fabric.resolve_item_id(item=name, item_type="Lakehouse", workspace=workspace_id)
    root = f"abfss://{workspace_id}@onelake.dfs.fabric.microsoft.com/{item_id}/Tables"
    return f"{root}/{schema}" if schema else root


def layer_workspace_id(layer: str) -> str:
    """The id of another layer's workspace, worked out from this one's name.

    Storage lives in its own workspace, so a notebook in Ingest or Prepare has to address
    Store explicitly - `currentWorkspaceId` is its own workspace and has no lakehouses in
    it. Platform workspaces are named `<solution> - <layer> [<env>]`, so the sibling is the
    same name with the layer swapped. Convention-dependent by design, exactly like
    `get_environment()`: no configuration, no lookup table, no API call to discover.

    A feature workspace is named `*<feature> (<layer>)` and carries neither the solution
    nor the environment, so it cannot name its sibling this way. Feature branches read and
    write the base environment's shared storage instead, and the notebook has to be told
    which workspace that is - see documentation/reference/feature-storage.md.
    """
    name = notebookutils.runtime.context.get("currentWorkspaceName") or ""
    match = re.match(r"^(?P<prefix>.+) - (?P<layer>[^\[\]]+) \[(?P<env>[^\[\]]+)\]$", name)
    if match:
        return fabric.resolve_workspace_id(f"{match.group('prefix')} - {layer} [{match.group('env')}]")

    # A feature workspace. It reads the base environment's shared storage, and FabricOps
    # stamped that base's name into this workspace's description when it created it.
    description = None
    try:
        description = invoke_api(
            f"https://api.fabric.microsoft.com/v1/workspaces/{current_workspace_id()}"
        ).get("description")
    except Exception:                                  # noqa: BLE001 - fall through to the name
        pass
    sibling = _base_workspace_for(layer, description, name, FEATURE_STORAGE.get("base_environment", "dev"))
    if not sibling:
        raise ValueError(
            f"cannot work out the {layer} workspace from '{name}'. It is not named "
            f"'<solution> - <layer> [<environment>]', its description carries no 'base=' stamp, "
            f"and its name has no solution prefix to derive one from. Re-run the feature "
            f"pipeline so FabricOps stamps it, or set the workspace id explicitly at the top "
            f"of the notebook."
        )
    return fabric.resolve_workspace_id(sibling)


def _base_workspace_for(layer: str, description, workspace_name: str, base_environment: str):
    """The base environment's workspace for `layer`, from the stamp or, failing that, the name.

    Pure, so the decision is testable off-tenant. The stamp wins: it is written by the tool
    that knows the base pattern, and it survives any naming scheme. The name is the
    fallback for a workspace created before stamping existed, and only works when the
    feature pattern carries the solution as a prefix (`*Brickyard add-orders (Prepare)`).
    """
    if description and " base=" in description:
        template = description.rsplit(" base=", 1)[1].strip()
        if "{layer}" in template:
            return template.replace("{layer}", layer)
    match = re.match(r"^\*(?P<solution>\S+)\s+\S+\s+\([^)]+\)$", workspace_name or "")
    if match:
        return f"{match.group('solution')} - {layer} [{base_environment}]"
    return None


def current_workspace_id() -> str:
    return notebookutils.runtime.context.get("currentWorkspaceId")


def get_environment(default: str = "dev") -> str:
    """Environment inferred from the workspace name, e.g. `Sales - Store [tst]` -> `tst`.

    Naming-convention dependent by design: it needs no configuration and no API call. An
    unrecognised name yields the default rather than guessing.
    """
    name = notebookutils.runtime.context.get("currentWorkspaceName") or ""
    match = re.search(r"\[([A-Za-z0-9_-]+)\]", name)
    return match.group(1).lower() if match else default

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Fabric and Power BI API access
# One wrapper, with retries on transient failures and long-running operations followed to
# completion. Errors raise - a helper that returns `None` on failure turns a broken call
# into wrong data three cells later.

# CELL ********************

TRANSIENT_STATUS = [429, 500, 502, 503, 504]

# The only audience keys notebookutils accepts. Note there is no "fabric": `pbi` is the key
# for both Power BI *and* Fabric REST APIs. Passing anything else fails deep inside Py4J
# with `"fabric" is not a valid resource`, which does not read as an argument problem at all.
# https://learn.microsoft.com/fabric/data-engineering/notebookutils/notebookutils-credentials
AUDIENCES = ("storage", "pbi", "keyvault", "kusto")


def invoke_api(
    url: str,
    method: str = "GET",
    payload: object = None,
    audience: str = "pbi",
    timeout: int = 240,
    lro_timeout: int = 600,
) -> dict:
    """Call a Fabric or Power BI REST endpoint with the notebook's own identity.

    Under a service principal the `pbi` token carries a reduced scope set - Lakehouse,
    Notebook, Workspace, Dataset and the ML items - which covers everything here. Anything
    broader needs MSAL rather than `getToken`.
    """
    if audience not in AUDIENCES:
        raise ValueError(f"'{audience}' is not a valid audience key; use one of {', '.join(AUDIENCES)}")

    session = requests.Session()
    adapter = HTTPAdapter(max_retries=Retry(total=3, backoff_factor=5, status_forcelist=TRANSIENT_STATUS))
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    headers = {
        "Authorization": f"Bearer {notebookutils.credentials.getToken(audience)}",
        "Content-Type": "application/json",
    }
    response = session.request(method, url, headers=headers, json=payload, timeout=timeout)

    if response.status_code == 202 and response.headers.get("x-ms-operation-id"):
        response = _await_operation(session, headers, response.headers["x-ms-operation-id"], lro_timeout)

    return {
        "status_code": response.status_code,
        "response": response.json() if response.content else None,
        "headers": dict(response.headers),
    }


def _await_operation(session, headers, operation_id: str, timeout: int):
    """Poll a long-running operation until it settles, or give up loudly."""
    state_url = f"https://api.fabric.microsoft.com/v1/operations/{operation_id}"
    deadline = time.time() + timeout

    while True:
        state = session.request("GET", state_url, headers=headers).json()
        status = state.get("status")

        if status == "Succeeded":
            return session.request("GET", f"{state_url}/result", headers=headers)
        if status not in ("NotStarted", "Running"):
            raise RuntimeError(f"Operation {operation_id} ended as {status}: {state}")
        if time.time() > deadline:
            raise TimeoutError(f"Operation {operation_id} still {status} after {timeout}s")

        print(".", end="", flush=True)
        time.sleep(2)


def refresh_sql_endpoint(workspace_id: str, sql_endpoint_id: str) -> dict:
    """Force the SQL analytics endpoint to pick up new or changed Delta tables.

    Worth calling after a load: the endpoint's metadata lags behind OneLake, and a
    semantic model refreshed in that window sees the old shape.
    """
    endpoint = (
        f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}"
        f"/sqlEndpoints/{sql_endpoint_id}/refreshMetadata"
    )
    return invoke_api(endpoint, method="POST", payload={})

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Moving and referencing tables

# CELL ********************

def create_table_shortcuts(
    table_names: list[str],
    source_workspace_id: str,
    source_lakehouse_id: str,
    target_workspace_id: str,
    target_lakehouse_id: str,
    conflict_policy: str = "CreateOrOverwrite",
    source_schema: str | None = DEFAULT_SCHEMA,
    target_schema: str | None = DEFAULT_SCHEMA,
) -> dict:
    """Point a lakehouse at tables that live in another one, without copying them.

    Both lakehouses in this solution are schema-enabled, so the default is `dbo` on each
    side. Pass None for either to address a flat lakehouse - the two ends are separate
    arguments because a shortcut can span one of each.
    """
    endpoint = (
        f"https://api.fabric.microsoft.com/v1/workspaces/{target_workspace_id}"
        f"/items/{target_lakehouse_id}/shortcuts/bulkCreate?shortcutConflictPolicy={conflict_policy}"
    )
    source_root = f"Tables/{source_schema}" if source_schema else "Tables"
    target_root = f"Tables/{target_schema}" if target_schema else "Tables"
    requests_payload = [
        {
            "path": target_root,
            "name": table_name,
            "target": {
                "oneLake": {
                    "workspaceId": source_workspace_id,
                    "itemId": source_lakehouse_id,
                    "path": f"{source_root}/{table_name}",
                }
            },
        }
        for table_name in table_names
    ]
    return invoke_api(endpoint, method="POST", payload={"createShortcutRequests": requests_payload})


def copy_tables(source_tables_path: str, target_tables_path: str) -> list[str]:
    """Copy every Delta table from one lakehouse to another. Overwrites the target."""
    copied = []
    for table in notebookutils.fs.ls(source_tables_path):
        name = table.path.rstrip("/").split("/")[-1]
        spark.read.format("delta").load(table.path).write.format("delta").mode("overwrite").save(
            f"{target_tables_path}/{name}"
        )
        copied.append(name)
        print(f"  copied {name}")
    return copied


def write_delta(df: DataFrame, tables_path: str, name: str) -> int:
    """Overwrite one Delta table and return its row count."""
    df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").save(f"{tables_path}/{name}")
    return df.count()

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Feature storage
# Where a notebook reads from and writes to while someone works on a feature branch.
# 
# **This cell is the platform decision.** Everything here is meant to be edited once, when
# the solution is set up, and then left alone. The default is `enabled: False` - every read
# and write uses the shared schema, no metadata call is made, and nothing below has any
# effect. That is the right setting for a small team working additively, and turning it on
# buys isolation at the cost of storage, staleness and a boundary that stops at Spark.
# 
# See `documentation/reference/feature-storage.md` for the reasoning.

# CELL ********************

FEATURE_STORAGE = {
    # The master switch. Off: the resolver is a no-op that returns DEFAULT_SCHEMA without
    # asking the workspace anything. Nothing else in this dictionary matters.
    "enabled": False,

    # The shared schema. Everything outside a feature workspace uses this, and so does a
    # feature workspace for anything it has not forked.
    "default_schema": DEFAULT_SCHEMA,

    # The schema a feature's own tables live in. Must contain {feature}. Per feature, not
    # per table: one schema holds everything that feature forks.
    "pattern": "dev_{feature}",

    # How a feature workspace is recognised, and where the feature name is inside its name.
    # Must be a regular expression with a (?P<feature>...) group. The default matches the
    # feature naming pattern in feature.json - a leading asterisk, an optional solution
    # prefix, the feature, then the layer in brackets - so "*Demo add-orders (Prepare)"
    # gives "add-orders".
    #
    # Derived from the workspace and never from anything committed, which is what stops a
    # feature schema reaching test or production: no file in the repository names one. A
    # workspace that does not match is not a feature workspace, so detection fails toward
    # shared.
    "workspace_pattern": r"^\*(?:.*\s)?(?P<feature>\S+)\s+\([^)]+\)$",

    # Writes.
    #   True  - every write in a feature workspace goes to the feature schema. Predictable,
    #           and it cannot silently mutate a table other people read.
    #   False - a write goes to the feature schema only for tables already forked there,
    #           so you fork the one table you are reshaping and everything else stays
    #           shared. Needs the table name: write_schema() with no table cannot know a
    #           table is opted in, so it returns the shared schema.
    "write_always_to_feature": True,

    # Reads. True: a table the feature has forked is read from the feature schema, and
    # everything else from the shared one. False: always read the shared schema, which is
    # what you want if a feature writes tables nothing downstream should pick up yet.
    "read_overlay": True,

    # Fork a table by SHALLOW CLONE of the shared one rather than starting empty. The clone
    # is metadata only - it shares the source's OneLake files - so a first write costs
    # almost nothing instead of rebuilding the table.
    #
    # Two things to know. VACUUM on the *source* can delete files a clone still points at,
    # which breaks the clone: keep feature schemas short-lived. And a clone is a snapshot,
    # so it stops seeing new rows landing in the shared table the moment it is made.
    "clone_on_first_write": True,

    # Which lakehouses may hold feature schemas, by name. None means all of them.
    # Curated only, by default: reshaping something in Base means everything downstream
    # should run off the fork too, which is a much larger change than it looks. Base stays
    # shared, and a Base change follows the additive policy instead.
    "forkable_lakehouses": ["Curated"],

    # The environment a feature workspace reads shared storage from, when its description
    # carries no `base=` stamp and the base has to be derived from the workspace name.
    "base_environment": "dev",
}

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# #### The resolver
# `_decide_*` are pure: given the configuration, the feature name and the set of tables
# already forked, they return a schema and touch nothing. Everything that talks to the
# workspace is in the thin wrappers below them, which is what makes the decision table
# testable off-tenant.

# CELL ********************

def _sanitise_feature(name: str) -> str:
    """Letters, numbers and underscore, lowercased. Schema names have no room for the rest."""
    return re.sub(r"[^0-9a-zA-Z_]+", "_", str(name)).strip("_").lower()


def _feature_schema_for(feature: str, config: dict) -> str:
    return str(config["pattern"]).format(feature=_sanitise_feature(feature))


def _is_forkable(lakehouse, config: dict) -> bool:
    """Whether feature schemas are allowed in this lakehouse.

    An unnamed lakehouse is treated as forkable: the caller did not say, and refusing to
    isolate is the more surprising of the two answers once the switch is on.
    """
    allowed = config.get("forkable_lakehouses")
    if allowed is None or lakehouse is None:
        return True
    return str(lakehouse) in {str(item) for item in allowed}


def _decide_write(table, forked, config: dict, feature, lakehouse=None) -> str:
    """The schema a write goes to. Pure."""
    default = config.get("default_schema", DEFAULT_SCHEMA)
    if not config.get("enabled") or not feature or not _is_forkable(lakehouse, config):
        return default
    if config.get("write_always_to_feature"):
        return _feature_schema_for(feature, config)
    # Opt-in per table: only somewhere already forked.
    if table is not None and str(table) in forked:
        return _feature_schema_for(feature, config)
    return default


def _decide_read(table, forked, config: dict, feature, lakehouse=None) -> str:
    """The schema a read comes from. Pure."""
    default = config.get("default_schema", DEFAULT_SCHEMA)
    if not config.get("enabled") or not feature or not config.get("read_overlay"):
        return default
    if not _is_forkable(lakehouse, config):
        return default
    return _feature_schema_for(feature, config) if str(table) in forked else default

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# #### Talking to the workspace
# Detection is derived from the workspace name and never from anything committed, so a
# feature schema cannot reach test or production: there is nothing in the repository that
# names one. Detection fails toward shared.

# CELL ********************

_FEATURE_STATE = {"feature": None, "resolved": False, "tables": {}, "announced": False}


def feature_name() -> "str | None":
    """The feature this workspace belongs to, or None if it is not a feature workspace.

    Read once per session. A workspace that does not match the pattern - every environment
    workspace, and anything renamed by hand - is not a feature workspace, which is the safe
    answer to fail toward.
    """
    if _FEATURE_STATE["resolved"]:
        return _FEATURE_STATE["feature"]
    _FEATURE_STATE["resolved"] = True
    if not FEATURE_STORAGE.get("enabled"):
        return None
    try:
        name = fabric.resolve_workspace_name(current_workspace_id())
        match = re.match(str(FEATURE_STORAGE["workspace_pattern"]), str(name))
        _FEATURE_STATE["feature"] = match.group("feature") if match else None
    except Exception as error:                       # noqa: BLE001 - never fail a notebook over this
        print(f"feature storage: could not read the workspace name ({error}); using shared")
        _FEATURE_STATE["feature"] = None
    return _FEATURE_STATE["feature"]


def feature_schema() -> "str | None":
    """This session's feature schema, or None outside a feature workspace."""
    feature = feature_name()
    return _feature_schema_for(feature, FEATURE_STORAGE) if feature else None


def forked_tables(lakehouse: str, workspace_id: "str | None" = None) -> set:
    """Tables this feature has already forked in `lakehouse`. Cached per session.

    An absent schema is an empty set, not an error: not having forked anything yet is the
    normal state, not a failure.
    """
    schema = feature_schema()
    if not schema:
        return set()
    key = f"{workspace_id or ''}/{lakehouse}"
    if key in _FEATURE_STATE["tables"]:
        return _FEATURE_STATE["tables"][key]
    found = set()
    try:
        path = lakehouse_tables(lakehouse, workspace_id=workspace_id, schema=schema)
        found = {entry.name.rstrip("/") for entry in notebookutils.fs.ls(path)}
    except Exception:                                # noqa: BLE001 - absent schema, mostly
        found = set()
    _FEATURE_STATE["tables"][key] = found
    return found


def _announce(write_to: str) -> None:
    if _FEATURE_STATE["announced"]:
        return
    _FEATURE_STATE["announced"] = True
    feature = feature_name()
    if not feature:
        print(f"feature storage: mode=shared · schema={DEFAULT_SCHEMA}")
        return
    overlay = "on" if FEATURE_STORAGE.get("read_overlay") else "off"
    print(
        f"feature storage: mode=feature · feature={feature} · write={write_to} "
        f"· read overlay={overlay} → {DEFAULT_SCHEMA}"
    )


def write_schema(
    table: "str | None" = None,
    lakehouse: "str | None" = None,
    shared: bool = False,
    workspace_id: "str | None" = None,
) -> str:
    """The schema this session should write to.

    A lookup, and only a lookup - it creates nothing. `shared=True` is the deliberate
    escape hatch for a write that is meant to land in the shared schema from a feature
    branch; it is never inferred, because a write that silently falls back to shared is the
    one thing isolation exists to prevent.
    """
    if shared:
        return DEFAULT_SCHEMA
    feature = feature_name()
    # `workspace_id` is the workspace the lakehouse lives in - Store, not the one this
    # notebook runs in. Without it the lookup searched the feature workspace for a lakehouse
    # that is not there, found nothing, and the overlay never fired.
    forked = forked_tables(lakehouse, workspace_id) if (feature and table and lakehouse) else set()
    resolved = _decide_write(table, forked, FEATURE_STORAGE, feature, lakehouse)
    _announce(resolved)
    return resolved


def read_schema(table: str, lakehouse: "str | None" = None, workspace_id: "str | None" = None) -> str:
    """The schema to read `table` from: the feature's copy if it has one, else the shared one."""
    feature = feature_name()
    forked = forked_tables(lakehouse, workspace_id) if (feature and lakehouse) else set()
    return _decide_read(table, forked, FEATURE_STORAGE, feature, lakehouse)


def fork_table(table: str, lakehouse: str, workspace_id: "str | None" = None) -> "str | None":
    """Give this feature its own copy of `table`, and return the schema it now lives in.

    Call this before an **incremental** write - a merge or an append - where the feature
    copy has to start from the shared table's current contents. A full overwrite does not
    need it: the write creates the table itself.

    With `clone_on_first_write` the copy is a Delta SHALLOW CLONE, which is metadata only
    and shares the source's files, so it is near-instant whatever the table's size. Without
    it, nothing is created and the write starts empty.

    Returns None outside a feature workspace, or where the lakehouse is not forkable, so
    the caller writes shared without special-casing anything.
    """
    schema = feature_schema()
    if not schema or not _is_forkable(lakehouse, FEATURE_STORAGE):
        return None
    if table in forked_tables(lakehouse, workspace_id):
        return schema
    if not FEATURE_STORAGE.get("clone_on_first_write"):
        return schema
    source = f"{lakehouse_tables(lakehouse, workspace_id=workspace_id, schema=DEFAULT_SCHEMA)}/{table}"
    target = f"{lakehouse_tables(lakehouse, workspace_id=workspace_id, schema=schema)}/{table}"
    spark.sql(f"CREATE TABLE IF NOT EXISTS delta.`{target}` SHALLOW CLONE delta.`{source}`")
    _FEATURE_STATE["tables"].pop(f"{workspace_id or ''}/{lakehouse}", None)
    print(f"feature storage: forked {DEFAULT_SCHEMA}.{table} → {schema}.{table} (shallow clone)")
    return schema


def refresh() -> None:
    """Forget the cached table lists, for tables created elsewhere mid-session."""
    _FEATURE_STATE["tables"].clear()

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
