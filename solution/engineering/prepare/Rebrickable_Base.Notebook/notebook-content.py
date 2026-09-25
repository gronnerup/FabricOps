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
# # **Rebrickable - Base**
# 
# </center>
# 
# ### Purpose
# Conforms the raw Landing tables into **Base**: typed, trimmed, de-duplicated, and with
# the two derived structures the catalogue needs before anyone can model it sensibly.
# 
# ### What actually happens here
# Landing keeps source fidelity, which means every column arrives as a string and the CSVs
# carry their own conventions. Base is where that gets resolved:
# 
# | Problem in the source | Handled here |
# |---|---|
# | Booleans arrive as `"t"` / `"f"` | Cast to real booleans |
# | Numbers arrive as strings, blanks as `""` | Cast, with empty strings becoming `NULL` |
# | Names carry stray whitespace | Trimmed |
# | A set has **several inventory versions** | `is_latest` flag on the highest version per set |
# | Themes are a **recursive parent/child list** | Flattened to root theme and depth |
# 
# The last two are the reason this layer exists. Everything downstream that counts parts in
# a set is wrong unless it filters to the latest inventory, and no report can group by theme
# family while the hierarchy is still a self-join.
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

source_lakehouse_name = "Landing"
target_lakehouse_name = "Base"
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

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Resolve lakehouses

# CELL ********************

from pyspark.sql import DataFrame, functions as F, Window

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


print(f"Source: {lakehouse_tables(source_lakehouse_name, source_workspace_id, source_schema or DEFAULT_SCHEMA)}"
      f"{'' if source_schema else '  (feature copies preferred where they exist)'}")
print(f"Target: {TARGET}")

# A subset makes a run touch only what it names. In a feature workspace that is the whole
# point: write the one table the feature changes, leave the rest to be read from dbo.
selected = {t.strip() for t in str(tables).split(",") if t.strip()} if tables else None


def wanted(*names: str) -> bool:
    return selected is None or any(n in selected for n in names)


print(f"Tables: {', '.join(sorted(selected)) if selected else 'all'}")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Conforming rules
# One declaration per table rather than fourteen hand-written blocks: what to cast, what is
# really a boolean, and which columns identify a row.

# CELL ********************

# table: (integer columns, boolean columns, natural key)
CONFORM = {
    "colors":              (["id"],                                   ["is_trans"], ["id"]),
    "themes":              (["id", "parent_id"],                      [],           ["id"]),
    "part_categories":     (["id"],                                   [],           ["id"]),
    "parts":               (["part_cat_id"],                          [],           ["part_num"]),
    "part_relationships":  ([],                                       [],           ["rel_type", "child_part_num", "parent_part_num"]),
    "elements":            (["color_id"],                             [],           ["element_id"]),
    "sets":                (["year", "theme_id", "num_parts"],        [],           ["set_num"]),
    "minifigs":            (["num_parts"],                            [],           ["fig_num"]),
    "inventories":         (["id", "version"],                        [],           ["id"]),
    "inventory_parts":     (["inventory_id", "color_id", "quantity"], ["is_spare"], ["inventory_id", "part_num", "color_id", "is_spare"]),
    "inventory_sets":      (["inventory_id", "quantity"],             [],           ["inventory_id", "set_num"]),
    "inventory_minifigs":  (["inventory_id", "quantity"],             [],           ["inventory_id", "fig_num"]),
    "members":             (["member_number"],                        [],           ["member_number"]),
    "member_sets":         (["member_number"],                        [],           ["member_number", "set_num"]),
}

TRUE_VALUES = ("t", "true", "1", "y", "yes")

def conform(df: DataFrame, integers: list[str], booleans: list[str], key: list[str]) -> DataFrame:
    """Trim, blank-to-null, cast, then de-duplicate on the natural key."""
    for column, dtype in df.dtypes:
        if dtype == "string":
            trimmed = F.trim(F.col(column))
            df = df.withColumn(column, F.when(trimmed == "", None).otherwise(trimmed))

    for column in integers:
        if column in df.columns:
            df = df.withColumn(column, F.col(column).cast("int"))

    for column in booleans:
        if column in df.columns:
            df = df.withColumn(column, F.lower(F.col(column)).isin(list(TRUE_VALUES)))

    present = [column for column in key if column in df.columns]
    return df.dropDuplicates(present) if present else df

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Conform every table

# CELL ********************

KNOWN = set(CONFORM)
# The names this notebook can write. A selection matching none of them would otherwise
# finish green having written nothing, which reads as success and is the opposite.
unknown = sorted(selected - KNOWN) if selected else []
if unknown:
    raise ValueError(
        f"tables={tables!r} names {', '.join(unknown)}, which this notebook does not write. "
        f"It writes: {', '.join(sorted(KNOWN))}."
    )

conformed = {}
for name, (integers, booleans, key) in CONFORM.items():
    if not wanted(name):
        continue
    df = conform(spark.read.format("delta").load(source_path(name)), integers, booleans, key)
    conformed[name] = df
    print(f"  {name:<22} {write_delta(df, TARGET, name):>9,} rows")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Latest inventory per set
# A set accumulates inventory versions as the catalogue is corrected. Counting parts across
# all versions double-counts, so mark the highest version per set and let downstream filter
# on one flag rather than rediscovering this every time.

# CELL ********************

if wanted("inventories"):
    inventories = conformed["inventories"]
    latest = Window.partitionBy("set_num").orderBy(F.col("version").desc())

    inventories_flagged = (
        inventories
        .withColumn("_rank", F.row_number().over(latest))
        .withColumn("is_latest", F.col("_rank") == 1)
        .drop("_rank")
    )

    write_delta(inventories_flagged, TARGET, "inventories")
    versions = inventories_flagged.filter("is_latest").count()
    print(f"  inventories            {versions:>9,} sets at their latest version")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Flatten the theme hierarchy
# `themes.parent_id` is recursive and arbitrarily deep. Walking it iteratively - joining the
# frontier to its parents until nothing new resolves - gives every theme its root and its
# depth, which is what a report actually groups by.

# CELL ********************

if wanted("themes"):
    themes = conformed["themes"].select("id", "name", "parent_id")

    # Themes is a few hundred rows, so climbing the parent pointer a fixed number of times is
    # cheaper to run - and far easier to read - than a convergence check on every pass.
    MAX_DEPTH = 10

    climb = themes.select(
        F.col("id"),
        F.col("id").alias("ancestor_id"),
        F.col("parent_id").alias("ancestor_parent_id"),
        F.lit(0).alias("level"),
    )
    parents = themes.select(F.col("id").alias("p_id"), F.col("parent_id").alias("p_parent_id"))

    for _ in range(MAX_DEPTH):
        climb = (
            climb.join(parents, climb["ancestor_parent_id"] == parents["p_id"], "left")
            .select(
                climb["id"],
                # step up when there is a parent, otherwise stay put: the root is a fixed point
                F.coalesce(parents["p_id"], climb["ancestor_id"]).alias("ancestor_id"),
                parents["p_parent_id"].alias("ancestor_parent_id"),
                F.when(parents["p_id"].isNotNull(), climb["level"] + 1).otherwise(climb["level"]).alias("level"),
            )
        )

    names = themes.select(F.col("id").alias("n_id"), F.col("name").alias("n_name"))

    themes_flat = (
        themes.alias("t")
        .join(climb.select("id", F.col("ancestor_id").alias("root_id"), "level"), on="id", how="left")
        .join(names.alias("root"), F.col("root_id") == F.col("root.n_id"), "left")
        .join(names.alias("parent"), F.col("t.parent_id") == F.col("parent.n_id"), "left")
        .select(
            F.col("t.id").alias("id"),
            F.col("t.name").alias("name"),
            F.col("t.parent_id").alias("parent_id"),
            F.col("parent.n_name").alias("parent_name"),
            F.col("root_id"),
            F.coalesce(F.col("root.n_name"), F.col("t.name")).alias("root_name"),
            F.col("level"),
        )
    )

    depth = themes_flat.agg(F.max("level")).collect()[0][0]
    print(f"  themes                 {write_delta(themes_flat, TARGET, 'themes'):>9,} rows, max depth {depth}")
    print("\nBase complete.")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
