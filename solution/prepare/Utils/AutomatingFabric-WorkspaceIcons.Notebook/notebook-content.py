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
# # **Utils:  Maintaining workspace icon images**
# 
# </center>  
# 
# ### Purpose
# This notebook demonstrates how to maintain workspace icon images programmatically.  
# The notebook uses the Fabric icons from Marc Lelijveld's blog post on [Designing Architectural Diagrams with the Latest Microsoft Fabric Icons](https://data-marc.com/2023/07/10/designing-architectural-diagrams-with-the-latest-microsoft-fabric-icons/). 
# 
# **_Prerequisites:_** The user executing the notebook must be workspace admin on the workspaces to be able to set the workspace icon
# 
# **_Disclaimer:_** This solution uses a non-documented and internal Microsoft endpoint for fetching and updating workspace metadata in Microsoft Fabric/Power BI. Since this is not an officially supported API, it may change without notice, which could impact the functionality of this approach. Use it with that in mind, and feel free to experiment!
# Also these APIs should be run in the context of a User Principal as the endpoints does not support Service Principal


# MARKDOWN ********************

# ### Configuration
# The following cells define how workspaces are selected and how their corresponding icons are configured and applied.
# - **is_dryrun**  
# Controls whether the notebook runs in dry-run mode. When set to True, no workspace icons are updated and changes are only simulated. Set to False to apply the updates.
# - **must_contain**  
#   Specifies a substring that must be present in the workspace name for it to be included.
# - **either_contain**  
#   Defines a list of substrings where at least one must be present in the workspace name in addition to the value defined in must_contain.
# - **workspace_icon_def**  
#   A JSON object that defines how workspace icons are assigned based on workspace name patterns.
#   - Keys represent substrings to match in workspace names.
#   - Values define the corresponding icon configuration.
#   - _color_overlays_ can be used to override icon colors.
#   - _text_overlay_ defines a short label displayed in the top-right corner of the icon (for example, an environment indicator).


# CELL ********************

is_dryrun = True

must_contain = "SpaceParts"
either_contain = ["dev","tst", "prd"]

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

workspace_icon_def = {
    "icons": {
        "prepare": "Notebook",
        "ingest": "Pipelines",
        "orchestrate": "Dataflow",
        "store": "Lakehouse",
        "model": "Dataset",
        "present": "Report",
        "core": "Links"
    },
    "color_overlays": {
        "dev": "#1E90FF",   # Blue
        "tst": "#FFA500",   # Orange
        "prd": "#008000"    # Green    
    },
    # "text_overlays": {
    #     "dev": "D",
    #     "tst": "T",
    #     "prd": "P"
    # }
}

### Reset workspace icons by setting the icon to "default". 
### Un-comment the lines below and run the notebook if you wish to reset workspace icons to the Power BI/Fabric default icon.
# workspace_icon_def = {
#     "icons": {
#         "orchestrate": "default",
#         "prepare": "default",
#         "ingest": "default",
#         "store": "default",
#         "serve": "default"
#     }
# }

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Notebook functionality
# Below cells define functions etc. for setting workspace icons.

# CELL ********************

%run "SpaceParts - Functions"

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

### Set cluster url based on response from Power BI API call. Overwrite with manual value if required.
CLUSTER_BASE_URL = get_cluster_url()
print(f"Using base URL: {CLUSTER_BASE_URL}")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# Print available icon names
for title in get_marcs_fabric_icons().keys():
    print(title)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark",
# META   "frozen": true,
# META   "editable": false
# META }

# CELL ********************

all_workspaces = get_workspaces()
workspaces = filter_items(all_workspaces, must_contain, either_contain)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

fabric_icons = get_marcs_fabric_icons()

for workspace in workspaces:
    display_name = workspace['displayName'].lower()

    # Check if any of the keys in 'icons' appear in displayName
    for icon_key, icon_value in workspace_icon_def['icons'].items():
        if icon_key in display_name:
            if icon_value == "":
                workspace["icon_base64img"] = None 
                break
            elif icon_value == "default":
                workspace["icon_base64img"] = "default" 
            else:
                workspace_icon = fabric_icons.get(icon_value)
                color_overlays = workspace_icon_def.get('color_overlays', {})

                if isinstance(color_overlays, dict) and color_overlays:
                    for color_key, color_value in color_overlays.items():
                        if color_key in display_name:
                            workspace_icon = fill_svg(workspace_icon, color_value)
                            break 
                
                workspace_icon = convert_svg_base64_to_png_base64(workspace_icon) if workspace_icon else None

                text_overlays = workspace_icon_def.get('text_overlays', {})
                if isinstance(text_overlays, dict) and text_overlays:
                    for overlay_key, overlay_value in text_overlays.items():
                        if overlay_key in display_name:
                            workspace_icon = add_letter_to_base64_png(workspace_icon, overlay_value, 15, "black", False)
                            break 

                workspace["icon_base64img"] = workspace_icon 
                break 

    

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ## Iterate workspaces and update icon
# Let us run through the workspaces which match our search pattern and update the icons as we have specified in the workspace icon definition json

# CELL ********************

if not is_dryrun:
    for workspace in workspaces:
        set_workspace_icon(
            workspace.get("id"),
            workspace.get("icon_base64img")
        )
    print("\033[1mIcons updated. Below is the result of the update:\033[0m")
else:
    print("\033[1mDry run mode\033[0m - Skipping actual icon update.")
    print("\033[1mBelow is the result if executed:\033[0m")

print("-" * 80 + "\n")
display_workspace_icons(workspaces)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark",
# META   "frozen": false,
# META   "editable": true
# META }
