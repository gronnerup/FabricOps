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
# # **Rebrickable - Curated**
# 
# </center>
# 
# ### Purpose
# Turns the conformed Base tables into the star the semantic model reads: six dimensions,
# two facts, a bridge and a date dimension.
# 
# ### Modelling decisions worth knowing
# 
# **Natural keys, not surrogates.** `set_num`, `part_num`, `color_id` and friends are
# already stable identifiers, and generating surrogates with `monotonically_increasing_id`
# would break idempotency - the same input would produce different keys on every run, and
# a Direct Lake model would silently repoint. Natural keys keep re-runs deterministic and
# the tables readable.
# 
# **Latest inventory only.** `fact_set_part` joins through `inventories` where
# `is_latest`, so a set contributes its parts once rather than once per catalogue revision.
# 
# **A real date dimension.** Direct Lake supports no calculated tables, so `dim_year` is
# materialised here rather than written in DAX.
# 
# **Orphans are dropped, and counted.** A fact row pointing at a missing dimension member
# produces a blank row in the model and a support question later. They are removed here and
# the count is printed, so the loss is visible rather than mysterious.
# 
# ### Idempotency
# Full overwrite. Running it twice produces the same tables.


# MARKDOWN ********************

# ### Shared helpers
# First, so that nothing below can reference a function that is not loaded yet.
# The parameters cell underneath is deliberately literal for the same reason: a pipeline
# overrides it wholesale.

# CELL ********************

%run fabricops_util

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Parameters

# PARAMETERS CELL ********************

source_lakehouse_name = "Base"
target_lakehouse_name = "Curated"
# Both lakehouses live in Store, not in the Prepare workspace this notebook runs in.
# Set these explicitly when running from a feature workspace.
# Literals only. A pipeline overrides this whole cell, so anything resolved by a helper
# has to be resolved after `%run`, not here.
storage_layer = "Store"           # both lakehouses live in Store, not in Prepare
source_workspace_id = None        # set to pin a workspace, e.g. from a feature workspace
target_workspace_id = None
source_schema = None              # None means the resolver decides
target_schema = None
tables = None                     # comma-separated subset to write, e.g. "members"; None means every table
refresh_endpoint = True   # nudge the SQL endpoint so Direct Lake sees the new tables

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Resolve lakehouses

# CELL ********************

from pyspark.sql import DataFrame, functions as F

source_workspace_id = source_workspace_id or layer_workspace_id(storage_layer)
target_workspace_id = target_workspace_id or layer_workspace_id(storage_layer)
# Reads are resolved per table: a table this feature has written is read from the feature
# schema, everything else from the shared one. Writes always go where the resolver says.
target_schema = target_schema or write_schema(lakehouse=target_lakehouse_name, workspace_id=target_workspace_id)
TARGET = lakehouse_tables(target_lakehouse_name, target_workspace_id, target_schema)


def source_path(name: str) -> str:
    """Where to read `name` from - pinned by `source_schema`, else the resolver's overlay."""
    schema = source_schema or read_schema(name, lakehouse=source_lakehouse_name, workspace_id=source_workspace_id)
    return f"{lakehouse_tables(source_lakehouse_name, source_workspace_id, schema)}/{name}"


def base(name: str) -> DataFrame:
    return spark.read.format("delta").load(source_path(name))


print(f"Source: {lakehouse_tables(source_lakehouse_name, source_workspace_id, source_schema or DEFAULT_SCHEMA)}"
      f"{'' if source_schema else '  (feature copies preferred where they exist)'}")
print(f"Target: {TARGET}")

# A subset makes a run touch only what it names. In a feature workspace that is the whole
# point: write the one table the feature changes, leave the rest to be read from dbo.
selected = {t.strip() for t in str(tables).split(",") if t.strip()} if tables else None


KNOWN = {"dim_theme", "dim_set", "dim_part", "dim_colour", "dim_minifig", "dim_member",
         "dim_year", "fact_set_part", "fact_set_minifig", "bridge_member_set"}
# The names this notebook can write. A selection matching none of them would otherwise
# finish green having written nothing, which reads as success and is the opposite.
unknown = sorted(selected - KNOWN) if selected else []
if unknown:
    raise ValueError(
        f"tables={tables!r} names {', '.join(unknown)}, which this notebook does not write. "
        f"It writes: {', '.join(sorted(KNOWN))}."
    )

def wanted(*names: str) -> bool:
    return selected is None or any(n in selected for n in names)


print(f"Tables: {', '.join(sorted(selected)) if selected else 'all'}")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Dimensions

# CELL ********************

dim_theme = base("themes").select(
    F.col("id").alias("theme_id"),
    F.col("name").alias("theme_name"),
    F.col("parent_name").alias("parent_theme_name"),
    F.col("root_name").alias("root_theme_name"),
    F.col("level").alias("theme_level"),
)

dim_set = (
    base("sets").alias("s")
    .join(base("themes").select(F.col("id").alias("t_id"), F.col("name").alias("t_name")),
          F.col("s.theme_id") == F.col("t_id"), "left")
    .select(
        F.col("s.set_num").alias("set_num"),
        F.col("s.name").alias("set_name"),
        F.col("s.year").alias("year"),
        F.col("s.theme_id").alias("theme_id"),
        F.col("t_name").alias("theme_name"),
        F.col("s.num_parts").alias("num_parts"),
    )
)

dim_part = (
    base("parts").alias("p")
    .join(base("part_categories").select(F.col("id").alias("c_id"), F.col("name").alias("c_name")),
          F.col("p.part_cat_id") == F.col("c_id"), "left")
    .select(
        F.col("p.part_num").alias("part_num"),
        F.col("p.name").alias("part_name"),
        F.coalesce(F.col("c_name"), F.lit("Unknown")).alias("part_category"),
        F.col("p.part_material").alias("part_material"),
    )
)

dim_colour = base("colors").select(
    F.col("id").alias("colour_id"),
    F.col("name").alias("colour_name"),
    # The report colours its bars with this, so make it a usable hex string.
    F.concat(F.lit("#"), F.upper(F.col("rgb"))).alias("colour_hex"),
    F.col("is_trans").alias("is_transparent"),
)

dim_minifig = base("minifigs").select(
    F.col("fig_num").alias("fig_num"),
    F.col("name").alias("minifig_name"),
    F.col("num_parts").alias("num_parts"),
)

dim_member = base("members").select(
    F.col("member_number").alias("member_number"),
    F.col("name").alias("member_name"),
)

for name, df in [
    ("dim_theme", dim_theme), ("dim_set", dim_set), ("dim_part", dim_part),
    ("dim_colour", dim_colour), ("dim_minifig", dim_minifig), ("dim_member", dim_member),
]:
    if not wanted(name):
        continue
    print(f"  {name:<18} {write_delta(df, TARGET, name):>9,} rows")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Date dimension
# One row per year the catalogue covers. Materialised because Direct Lake cannot calculate
# a table, and useful anyway - a decade column is one less thing for every report to invent.

# CELL ********************

if wanted("dim_year"):
    bounds = base("sets").agg(F.min("year").alias("lo"), F.max("year").alias("hi")).collect()[0]

    dim_year = (
        spark.range(int(bounds["lo"]), int(bounds["hi"]) + 1)
        .select(F.col("id").cast("int").alias("year"))
        .withColumn("decade", (F.col("year") / 10).cast("int") * 10)
        .withColumn("decade_name", F.concat(F.col("decade").cast("string"), F.lit("s")))
    )

    print(f"  dim_year           {write_delta(dim_year, TARGET, 'dim_year'):>9,} rows "
          f"({bounds['lo']}-{bounds['hi']})")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Facts
# `inventories` is the hinge: it carries the version history, so joining through it filtered
# to `is_latest` is what stops a set counting its parts several times over.

# CELL ********************

if wanted("fact_set_part", "fact_set_minifig"):
    latest_inventory = base("inventories").filter("is_latest").select("id", "set_num")

    def drop_orphans(fact: DataFrame, dimension: DataFrame, on: str, label: str) -> DataFrame:
        """Remove fact rows with no matching dimension member, and say how many went."""
        keys = dimension.select(on).distinct()
        kept = fact.join(keys, on=on, how="inner")
        before, after = fact.count(), kept.count()
        if before != after:
            print(f"      dropped {before - after:,} row(s) with no matching {label}")
        return kept

    fact_set_part = (
        base("inventory_parts").alias("ip")
        .join(latest_inventory.alias("inv"), F.col("ip.inventory_id") == F.col("inv.id"), "inner")
        .select(
            F.col("inv.set_num").alias("set_num"),
            F.col("ip.part_num").alias("part_num"),
            F.col("ip.color_id").alias("colour_id"),
            F.col("ip.quantity").alias("quantity"),
            F.col("ip.is_spare").alias("is_spare"),
        )
    )
    fact_set_part = drop_orphans(fact_set_part, dim_set, "set_num", "set")
    fact_set_part = drop_orphans(fact_set_part, dim_part, "part_num", "part")
    fact_set_part = drop_orphans(fact_set_part, dim_colour, "colour_id", "colour")
    print(f"  fact_set_part      {write_delta(fact_set_part, TARGET, 'fact_set_part'):>9,} rows")

    fact_set_minifig = (
        base("inventory_minifigs").alias("im")
        .join(latest_inventory.alias("inv"), F.col("im.inventory_id") == F.col("inv.id"), "inner")
        .select(
            F.col("inv.set_num").alias("set_num"),
            F.col("im.fig_num").alias("fig_num"),
            F.col("im.quantity").alias("quantity"),
        )
    )
    fact_set_minifig = drop_orphans(fact_set_minifig, dim_set, "set_num", "set")
    fact_set_minifig = drop_orphans(fact_set_minifig, dim_minifig, "fig_num", "minifig")
    print(f"  fact_set_minifig   {write_delta(fact_set_minifig, TARGET, 'fact_set_minifig'):>9,} rows")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Bridge
# Members own sets, and a set is owned by several members: a genuine many-to-many. Keeping
# it as a bridge rather than flattening it into a fact is what lets the model answer
# "which parts does Emmet own?" by traversing member → set → parts.

# CELL ********************

if wanted("bridge_member_set"):
    bridge_member_set = base("member_sets").select(
        F.col("member_number").alias("member_number"),
        F.col("set_num").alias("set_num"),
    ).distinct()

    bridge_member_set = drop_orphans(bridge_member_set, dim_member, "member_number", "member")
    bridge_member_set = drop_orphans(bridge_member_set, dim_set, "set_num", "set")
    print(f"  bridge_member_set  {write_delta(bridge_member_set, TARGET, 'bridge_member_set'):>9,} rows")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Refresh the SQL endpoint
# The endpoint's metadata lags behind OneLake, and a Direct Lake model reading it in that
# window sees the previous shape. Nudging it here means the report is correct the moment
# the pipeline finishes.

# CELL ********************

if refresh_endpoint and target_workspace_id:
    lakehouse_id = fabric.resolve_item_id(
        item=target_lakehouse_name, item_type="Lakehouse", workspace=target_workspace_id
    )
    details = invoke_api(
        f"https://api.fabric.microsoft.com/v1/workspaces/{target_workspace_id}/lakehouses/{lakehouse_id}",
    )
    endpoint_id = ((details.get("response") or {}).get("properties") or {}).get(
        "sqlEndpointProperties", {}
    ).get("id")

    if endpoint_id:
        refresh_sql_endpoint(target_workspace_id, endpoint_id)
        print(f"  SQL endpoint {endpoint_id} refreshed")
    else:
        print("  ⚠ no SQL endpoint found on the Curated lakehouse; skipped")

print("\nCurated complete.")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
