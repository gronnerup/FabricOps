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
# # **Rebrickable - Ingest**
# 
# </center>
# 
# ### Purpose
# Lands the [Rebrickable](https://rebrickable.com/downloads/) LEGO catalogue into the
# **Landing** lakehouse, one Delta table per source file, plus two small demo tables
# (`members`, `member_sets`) that give the model a many-to-many to work with.
# 
# ### How it works
# Each `.csv.gz` is downloaded to the driver, staged into `Files/_staging/rebrickable/`,
# and then read **by Spark directly from OneLake**. The parse is the expensive part, and
# doing it in Spark keeps it parallel - a million-row file through pandas on the driver is
# minutes and a memory risk.
# 
# Columns land as **strings, unmodified**. Landing keeps source fidelity; typing, boolean
# conversion and de-duplication happen in the Base notebook, where they can be reviewed.
# 
# ### Idempotency
# Full overwrite with schema merge. Running it twice produces the same tables.

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
# Overridable from a pipeline. `sample_rows` caps each table for a fast smoke run;
# leave it at `0` for the full catalogue.

# PARAMETERS CELL ********************

# Literals only. A pipeline overrides this whole cell, so anything resolved by a helper
# has to be resolved after `%run`, not here.
landing_lakehouse_name = "Landing"
landing_workspace_layer = "Store"   # storage is its own layer, not the one this runs in
landing_workspace_id = None         # set to pin a workspace, e.g. from a feature workspace
landing_schema = None               # None means the resolver decides
sample_rows = 0

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Configuration

# CELL ********************

REBRICKABLE_BASE_URL = "https://cdn.rebrickable.com/media/downloads"
STAGING_FOLDER = "_staging/rebrickable"

REBRICKABLE_FILES = [
    "colors",
    "themes",
    "part_categories",
    "parts",
    "part_relationships",
    "elements",
    "sets",
    "minifigs",
    "inventories",
    "inventory_parts",
    "inventory_sets",
    "inventory_minifigs",
]

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Resolve the Landing lakehouse

# CELL ********************

landing_workspace_id = landing_workspace_id or layer_workspace_id(landing_workspace_layer)
landing_schema = landing_schema or write_schema(lakehouse=landing_lakehouse_name, workspace_id=landing_workspace_id)

tables_path = lakehouse_tables(landing_lakehouse_name, landing_workspace_id, landing_schema)
files_path = tables_path.rsplit("/Tables", 1)[0] + "/Files" if landing_workspace_id else "Files"

staging_path = f"{files_path}/{STAGING_FOLDER}"
notebookutils.fs.mkdirs(staging_path)

print(f"Tables:  {tables_path}")
print(f"Staging: {staging_path}")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Download and stage
# Streamed to the driver in chunks, then copied into OneLake. Spark reads `.csv.gz`
# natively, so the archive is staged as-is.

# CELL ********************

import os
import requests

def stage_file(file_name: str) -> str:
    """Download one .csv.gz and place it in Files/. Returns the OneLake path."""
    url = f"{REBRICKABLE_BASE_URL}/{file_name}.csv.gz"
    local_path = f"/tmp/{file_name}.csv.gz"
    target_path = f"{staging_path}/{file_name}.csv.gz"

    with requests.get(url, timeout=300, stream=True) as response:
        response.raise_for_status()
        with open(local_path, "wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 20):
                handle.write(chunk)

    notebookutils.fs.cp(f"file://{local_path}", target_path, True)
    os.remove(local_path)
    return target_path

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Ingest
# Read with Spark, land as Delta. One count per table, reported at the end - counting
# inside the loop and again in a verification pass doubles the work for no new information.

# CELL ********************

row_counts = {}

for file_name in REBRICKABLE_FILES:
    source = stage_file(file_name)

    # Everything as strings: landing preserves what the source said, including "t"/"f"
    # booleans and empty strings. The Base notebook decides what they mean.
    df = (
        spark.read
        .option("header", "true")
        .option("inferSchema", "false")
        .option("multiLine", "true")
        .option("escape", '"')
        .csv(source)
    )
    if sample_rows:
        df = df.limit(sample_rows)

    row_counts[file_name] = write_delta(df, tables_path, file_name)
    print(f"  {file_name:<22} {row_counts[file_name]:>9,} rows")

print(f"\n{len(REBRICKABLE_FILES)} tables landed.")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark",
# META   "frozen": true,
# META   "editable": false
# META }

# MARKDOWN ********************

# ### Demo members
# Not from Rebrickable. A small cast of owners and the sets they own, so the curated layer
# has a genuine many-to-many to bridge.

# CELL ********************

from pyspark.sql.types import IntegerType, StringType, StructField, StructType

MEMBERS = [
    (1001, "Emmet Brickowski"), (1002, "Wyldstyle Lucy"), (1003, "Benny Spaceman"),
    (1004, "Lord Business"), (1005, "Kai Firefang"), (1006, "Jay Walker"),
    (1007, "Lloyd Garmadon"), (1008, "Nya Waterfall"), (1009, "Cole Brookstone"),
    (1010, "Zane Julien"), (1011, "Sensei Wu"), (1012, "Laval Lionheart"),
    (1013, "Eris Eagleton"), (1014, "Chase McCain"), (1015, "Rex Dangervest"),
    (1016, "Unikitty Cloudcuckoo"), (1017, "Metalbeard Pirate"), (1018, "Clutch Powers"),
]

members_schema = StructType([
    StructField("member_number", IntegerType(), False),
    StructField("name", StringType(), False),
])

write_delta(spark.createDataFrame(MEMBERS, members_schema), tables_path, "members")
print(f"  members                {len(MEMBERS):>9,} rows")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Member set ownership
# Which member owns which set. Deliberately uneven - a few large collectors and a few
# casual owners - so the leaderboard in the report has something to say.

# CELL ********************

MEMBER_SETS = [
    # Emmet Brickowski - the completionist
    (1001, "75192-1"), (1001, "75290-1"), (1001, "10294-1"), (1001, "71043-1"),
    (1001, "10276-1"), (1001, "75313-1"), (1001, "21054-1"), (1001, "42115-1"),
    (1001, "10297-1"), (1001, "75309-1"), (1001, "10300-1"), (1001, "21330-1"),
    # Wyldstyle - modern modular builds
    (1002, "10297-1"), (1002, "10312-1"), (1002, "10255-1"), (1002, "10278-1"),
    (1002, "21325-1"),
    # Benny - space, obviously
    (1003, "75192-1"), (1003, "10283-1"), (1003, "21309-1"), (1003, "75313-1"),
    # Lord Business - office and city
    (1004, "76178-1"), (1004, "10278-1"), (1004, "60380-1"),
    # The ninja - vehicles and castles
    (1005, "42115-1"), (1005, "42143-1"),
    (1006, "42115-1"), (1006, "10312-1"),
    (1007, "71741-1"), (1007, "71043-1"),
    (1008, "71741-1"),
    (1009, "42143-1"), (1009, "10276-1"),
    (1010, "71741-1"), (1010, "21330-1"),
    (1011, "71741-1"),
    # Chima and the rest - a long tail of one or two sets each
    (1012, "70010-1"), (1013, "70003-1"), (1014, "60380-1"), (1015, "10300-1"),
    (1016, "41455-1"), (1017, "10255-1"), (1017, "21325-1"), (1017, "76178-1"),
    (1017, "42143-1"), (1017, "10312-1"), (1017, "43222-1"), (1018, "60380-1"),
]

member_sets_schema = StructType([
    StructField("member_number", IntegerType(), False),
    StructField("set_num", StringType(), False),
])

write_delta(spark.createDataFrame(MEMBER_SETS, member_sets_schema), tables_path, "member_sets")
print(f"  member_sets            {len(MEMBER_SETS):>9,} rows")
print("\nLanding complete.")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
