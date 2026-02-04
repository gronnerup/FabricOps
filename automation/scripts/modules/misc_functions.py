import json, os, uuid, re, copy
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

yaml = YAML()
yaml.indent(mapping=4, sequence=4, offset=2)

# Color codes
cdefault = '\033[0m'
cdefault_bold = '\033[1m'
cred = '\033[91m'
cred_bold = '\033[1;91m'
cyellow = '\033[33m'
cyellow_bold = '\033[1;33m'
cgreen = '\033[32m'
cgreen_bold = '\033[1;32m'
cblue_bold = '\033[1;34m'


def print_error(value, bold:bool = False):
    if bold:
        print(f"{cred_bold}{value}{cdefault}")
    else:
        print(f"{cred}{value}{cdefault}")


def print_warning(value, bold:bool = False):
    if bold:
        print(f"{cyellow_bold}{value}{cdefault}")
    else:
        print(f"{cyellow}{value}{cdefault}")


def print_success(value, bold:bool = False):
    if bold:
        print(f"{cgreen_bold}{value}{cdefault}")
    else:
        print(f"{cgreen}{value}{cdefault}")

def print_info(value:str = "", bold:bool = False, end:str = "\n"):
    if bold:
        print(f"{cdefault_bold}{value}{cdefault}", end=end)
    else:
        print(f"{value}", end=end)
        
def print_header(value):
    print("")
    print(f"{cblue_bold}#################################################################################################################################{cdefault}")
    print(f"{cblue_bold}# {value.center(125)} #{cdefault}")
    print(f"{cblue_bold}#################################################################################################################################{cdefault}")
    

def print_subheader(value):
    print("")
    print(f"{cyellow_bold}##################################################################################################################{cdefault}")
    print(f"{cyellow_bold}# {value.center(110)} #{cdefault}")
    print(f"{cyellow_bold}##################################################################################################################{cdefault}")
    

def flatten_dict(d, parent_key=''):
    items = []
    for k, v in d.items():
        full_key = f"{parent_key}.{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_dict(v, full_key))
        else:
            items.append((full_key, v))
    return items

def load_json(file_path):
    if os.path.exists(file_path):
        with open(file_path, 'r') as file:
            return json.load(file)
    else:
        print(f"Json file not found: {file_path}")

def is_guid(value: str) -> bool:
    try:
        uuid_obj = uuid.UUID(value)
        return str(uuid_obj) == value.lower()
    except (ValueError, AttributeError, TypeError):
        return False

def get_private_endpoint_resource_type(private_link_resource_id):
    """
    Determines the resource type associated with a given private link resource ID.

    Args:
        private_link_resource_id (str): The Azure Resource ID for the private link resource.

    Returns:
        str or None: The resource type associated with the private link resource ID. Possible values include:
            - "vault" for Key Vault
            - "sqlServer" for SQL Server
            - "blob" for Blob storage
            - "databricks_ui_api" for Databricks
            - "SQL" for DocumentDB
            - "cluster" for Kusto clusters
            - "Sql" for Synapse workspaces
            - "sites" for Web Apps
            - "namespace" for Event Hubs
            - "iotHub" for IoT Hubs
            - "account" for Purview accounts
            - "amlworkspace" for Machine Learning workspaces
            - None if the resource type cannot be determined.
    """
    match private_link_resource_id:
        case _ if "Microsoft.KeyVault" in private_link_resource_id:
            return "vault"
        case _ if "Microsoft.Sql" in private_link_resource_id:
            return "sqlServer"
        case _ if "Microsoft.Storage/storageAccounts" in private_link_resource_id:
            return "blob"
        case _ if "Microsoft.Databricks" in private_link_resource_id:
            return "databricks_ui_api"
        case _ if "Microsoft.DocumentDB" in private_link_resource_id:
            return "SQL"
        case _ if "Microsoft.Kusto/clusters" in private_link_resource_id:
            return "cluster"
        case _ if "Microsoft.Synapse/workspaces" in private_link_resource_id:
            return "Sql"
        case _ if "Microsoft.Web/sites" in private_link_resource_id:
            return "sites"
        case _ if "Microsoft.EventHub/namespaces" in private_link_resource_id:
            return "namespace"
        case _ if "Microsoft.Devices/IotHubs" in private_link_resource_id:
            return "iotHub"
        case _ if "Microsoft.Purview/accounts" in private_link_resource_id:
            return "account"
        case _ if "Microsoft.MachineLearningServices/workspaces" in private_link_resource_id:
            return "amlworkspace"
        case _:
            return None

def merge_json(parent, child, inherited_merge_type=1):
    """Recursively merge child JSON into parent, respecting 'merge_type' at all levels."""

    if not isinstance(parent, dict) or not isinstance(child, dict):
        return parent if inherited_merge_type == 0 else child  # Respect override type 0

    merged = parent.copy()  # Start with parent values

    # Get the merge type for this level, inherited if not set
    current_merge_type = child.get("merge_type", inherited_merge_type)

    for key, child_value in child.items():
        if key == "merge_type":
            continue  # Skip processing merge_type itself as an attribute

        parent_value = parent.get(key)

        if isinstance(parent_value, dict) and isinstance(child_value, dict):
            # Recursively merge dictionaries, ensuring the correct merge_type is used at all levels
            merged[key] = merge_json(parent_value, child_value, current_merge_type)

        elif isinstance(parent_value, list) and isinstance(child_value, list):
            if current_merge_type == 0:
                merged[key] = parent_value  # Keep parent list (No Override)
            elif current_merge_type == 2:
                # Smart merge for lists containing objects with common identifiers
                merged_list = []
                parent_dict = {}
                
                # Index parent items by item_name if they're objects with item_name
                for item in parent_value:
                    if isinstance(item, dict) and 'item_name' in item:
                        parent_dict[item['item_name']] = item.copy()
                    else:
                        # For non-object items or objects without item_name, add directly
                        merged_list.append(item)
                
                # Process child items
                for item in child_value:
                    if isinstance(item, dict) and 'item_name' in item:
                        item_name = item['item_name']
                        if item_name in parent_dict:
                            # Merge with existing parent item
                            parent_dict[item_name].update(item)
                        else:
                            # New item not in parent
                            parent_dict[item_name] = item.copy()
                    else:
                        # For non-object items or objects without item_name, add directly
                        if item not in merged_list:
                            merged_list.append(item)
                
                # Add all merged objects with item_name to the final list
                merged_list.extend(parent_dict.values())
                merged[key] = merged_list
            else:
                merged[key] = child_value  # Replace list if merge_type is not 2

        else:
            # Apply merge rules for simple values
            if current_merge_type == 0:
                merged[key] = parent_value  # Keep parent value (No Override)
            else:
                merged[key] = child_value  # Replace with child

    return merged

def manage_find_replace(
    yml_path: str,
    action: str,
    find_value: str,
    replace_value: dict = None,
    comment: str = None,
    print_operations: bool = False
):

    if not os.path.isfile(yml_path):
        with open(yml_path, "w") as f:
            f.write("find_replace:\n")

    with open(yml_path, "r") as f:
        data = yaml.load(f)

    if 'find_replace' not in data or not isinstance(data['find_replace'], list):
        data['find_replace'] = CommentedSeq()

    entries: CommentedSeq = data['find_replace']
    entries[:] = [entry for entry in entries if entry is not None]

    def find_index(value):
        for idx, entry in enumerate(entries):
            if entry and entry.get('find_value') == value:
                return idx
        return None

    idx = find_index(find_value)

    if action == 'delete':
        if idx is not None:
            del entries[idx]
            print(f"✅ Deleted entry with find_value: {find_value}") if print_operations is True else None
        else:
            print(f"⚠️ No entry found to delete for find_value: {find_value}") if print_operations is True else None
    elif action == 'upsert':
        new_entry = CommentedMap()
        new_entry['find_value'] = find_value
        if comment:
            new_entry.yaml_add_eol_comment(comment, key='find_value')
        rv_map = CommentedMap()
        if replace_value:
            for k, v in replace_value.items():
                rv_map[k] = v
        new_entry['replace_value'] = rv_map

        if idx is not None:
            entries[idx] = new_entry
            print(f"🔁 Updated existing entry for find_value: {find_value}") if print_operations is True else None
        else:
            entries.append(new_entry)
            print(f"➕ Added new entry for find_value: {find_value}") if print_operations is True else None
    else:
        raise ValueError("Action must be 'upsert' or 'delete'")

    with open(yml_path, "w") as f:
        yaml.dump(data, f)

def find_item(data, layer_name, unique_name):
    # Find the layer object in the layers list where name matches layer_name
    layers = data["layers"]
    layer = next((l for l in layers if l.get("name") == layer_name), None)
    if not layer:
        return None

    for item in layer.get("items", []):
        if item.get("unique_name") == unique_name:
            return item

    return None  # Not found

def build_parameter_yml(yaml_file, all_environments):
    """
    Creates and returns a dictionary structure for a YAML file.

    Args:
        all_environments (dict): Dict of all environments and their properties to be used for deriving the parameter yml file.

    Returns:
        dict: The parameter dictionary with predefined sections ('find_replace' and 'spark_pool') and their mapped items
    """
    
    # Use dev as primary
    primary_env = "dev"

    item_props_in_scope = {
        "id": {"comment": "Item Guids"},
        "connectionId": {"comment": "Connection Guids"},
        "sqlEndpointId": {"comment": "SQL Endpoint Guids"},
        "serverFqdn": {"comment": "Fabric SQL Database addresses"},
        "databaseName": {"comment": "Fabric SQL Database names"},
        "connectionString": {"comment": "Fabric SQL Endpoint connection strings"},    
    }

    # Skip processing if no environments are provided
    if not all_environments.get("environments"):
        with open(yaml_file, 'r', encoding='utf-8') as file:
            return yaml.load(file)

    # Find the environment dict where name == primary_env
    primary_env_obj = next(env for env in all_environments["environments"] if env["name"] == primary_env)
    primary_layers = primary_env_obj["layers"]
 
    for layer in primary_layers: 
        primary_id = layer.get("workspace_id")
        layer_name = layer.get("name")

        # Map workspaces across environments
        replace_value = {}
        for env in all_environments.get("environments"):
            if env.get("name") == primary_env:
                continue  # Skip primary environment

            # Find the layer object where name == layer_name
            env_layer = next((l for l in env.get("layers", []) if l.get("name") == layer_name), None)

            if env_layer:
                replace_value[env.get("name")] = env_layer["workspace_id"]
                
        manage_find_replace(
            yml_path = yaml_file,
            action = "upsert",
            find_value = primary_id,
            replace_value = replace_value,
            comment = f"Workspace - {layer_name}"
        )

        print(f"Added replacement value for Workspace - {layer_name}")
    
        if layer.get("items"):
            for item in layer.get("items"):
                unique_name = item.get("unique_name")

                for item_prop_name, item_props in item_props_in_scope.items():
                    replace_value = {}
                    item.get(item_prop_name)
                    for env in all_environments.get("environments"):
                        if env.get("name") == primary_env:
                            continue # Skip primary environment
                        
                        env_item = find_item(env, layer_name, unique_name)
                        
                        if (env_item):
                            if env_item.get(item_prop_name):
                                replace_value[env.get("name")] = env_item.get(item_prop_name)

                    if replace_value:
                        manage_find_replace(
                            yml_path = yaml_file,
                            action = "upsert",
                            find_value = item.get(item_prop_name),
                            replace_value = replace_value,
                            comment = f"{item.get("type")}: {unique_name} - {item_props.get('comment')}"
                        )    

                        print(f"Added replacement value for {item.get("type")}: {unique_name} - {item_props.get('comment')}")

    print(f"Parameter file succesfully created in path {yaml_file}")


def save_json_to_file(data, filepath):
    """
    Save JSON data to a file with indentation and sorted keys.
    
    Args:
        data (dict): The JSON data to write.
        filepath (str): The path to the output file.
    """
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, sort_keys=True, ensure_ascii=False)


def read_json_from_file(filepath):
    """
    Read JSON data from a file.
    
    Args:
        filepath (str): The path to the JSON file to read.
        
    Returns:
        dict: The parsed JSON data.
    """
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)
    

def load_environments_as_dict(env_file_map):
    """
    Load multiple JSON files into a dictionary keyed by environment name.
    
    Args:
        env_file_map (dict): A mapping of environment name to file path.
    
    Returns:
        dict: A dictionary where each key is the environment name and value is its JSON data.
    """
    all_environments = {}

    for env_name, file_path in env_file_map.items():
        with open(file_path, 'r', encoding='utf-8') as f:
            all_environments[env_name] = json.load(f)

    return all_environments


def get_semantic_model_bindings(yml_path: str, target_layer: str) -> list:
    """
    Parse sqlendpoint_model_binding.yml and return binding entries for the given semantic model layer.
    Returns structure grouped by lakehouse to avoid redundant lookups.

    YAML structure example:
        semantic_model_sqlendpoint_binding:
          - lakehouse_name: Curated
            lakehouse_ws_layer: Store
            semantic_model_layer: Model
            semantic_models: [SpaceParts, SpacePartsSaveToFolder, SpacePartsBim]

    Args:
        yml_path (str): Path to the YAML file containing semantic model bindings.
        target_layer (str): The semantic model layer to filter by (case-insensitive), e.g., "Model".

    Returns:
        list[dict]: One entry per lakehouse, containing:
            {
                "semantic_model_layer": str,
                "lakehouse_ws_layer": str,
                "lakehouse_name": str,
                "semantic_models": list[str]
            }
    """

    try:
        with open(yml_path, 'r', encoding='utf-8') as f:
            data = yaml.load(f)
    except Exception as e:
        print_warning(f"Failed to load binding YAML from {yml_path}: {e}")
        return []

    entries = []
    target_layer_norm = (target_layer or "").strip().lower()

    bindings = (data or {}).get("semantic_model_sqlendpoint_binding", [])
    for b in bindings:
        semantic_model_layer = (b or {}).get("semantic_model_layer")
        lakehouse_ws_layer = (b or {}).get("lakehouse_ws_layer")
        lakehouse_name = (b or {}).get("lakehouse_name")
        sm_names = (b or {}).get("semantic_models")

        if not semantic_model_layer or not lakehouse_ws_layer or not lakehouse_name or not sm_names:
            continue

        if (semantic_model_layer or "").strip().lower() != target_layer_norm:
            continue

        # Normalize semantic_models to a list
        if isinstance(sm_names, (list, tuple)):
            semantic_models = [str(name) for name in sm_names if name]
        else:
            semantic_models = [str(sm_names)]

        entries.append({
            "semantic_model_layer": semantic_model_layer,
            "lakehouse_ws_layer": lakehouse_ws_layer,
            "lakehouse_name": lakehouse_name,
            "semantic_models": semantic_models
        })

    return entries


def build_parameter_yml_dynamic(yaml_file, dev_environment_data, target_environments=None):
    """
    Creates and updates parameter.yml with dynamic values based on dev environment scan.
    Uses dynamic notation like $workspace.SpaceParts - Core [tst] to reference items by name.
    Preserves existing content in the parameter file.

    Args:
        yaml_file (str): Path to the parameter.yml file.
        dev_environment_data (dict): Dev environment data with layers and items from Fabric scan.
                                     Expected structure:
                                     {
                                         "name": "dev",
                                         "layers": [
                                             {
                                                 "name": "Core",
                                                 "workspace_name": "SpaceParts - Core [dev]",
                                                 "workspace_id": "...",
                                                 "items": [...]
                                             },
                                             ...
                                         ]
                                     }
        target_environments (list, optional): List of target environment names (e.g., ['tst', 'prd']).
                                              Defaults to ['tst', 'prd'] if not provided.

    Returns:
        None: Updates the parameter.yml file in-place
    """
    
    if target_environments is None:
        target_environments = ["tst", "prd"]
    
    if not dev_environment_data or not dev_environment_data.get("layers"):
        print_warning(f"No dev environment data provided. Skipping parameter file update.")
        return

    # Only valid generic item property we map for all types
    item_props_in_scope = {
        "id": {"comment": "Item Guids"}
    }

    dev_layers = dev_environment_data.get("layers", [])
    
    for layer in dev_layers:
        primary_id = layer.get("workspace_id")
        layer_name = layer.get("name")
        workspace_name = layer.get("workspace_name")

        if not primary_id or not layer_name:
            continue

        # Workspace name mapping
        # Example: "SpaceParts - Model [dev]" -> tst: SpaceParts - Model [tst]
        if workspace_name:
            replace_value = {}
            for env in target_environments:
                replace_value[env] = workspace_name.replace("[dev]", f"[{env}]")

            manage_find_replace(
                yml_path=yaml_file,
                action="upsert",
                find_value=workspace_name,
                replace_value=replace_value,
                comment=f"Workspace - {layer_name} (name)"
            )

            print_info(f"✔ Added dynamic reference for Workspace name - {layer_name}", bold=False)

        # Workspace ID mapping
        # For workspace IDs, create dynamic references for other environments
        # Example: workspace_id (dev) -> $workspace.SpaceParts - Core [tst]
        replace_value = {}
        for env in target_environments:
            dynamic_ref = workspace_name.replace("[dev]", f"[{env}]")
            replace_value[env] = f"$workspace.{dynamic_ref}"

        manage_find_replace(
            yml_path=yaml_file,
            action="upsert",
            find_value=primary_id,
            replace_value=replace_value,
            comment=f"Workspace - {layer_name}"
        )

        print_info(f"✔ Added dynamic reference for Workspace - {layer_name}", bold=False)

        # Process items in the layer
        if layer.get("items"):
            for item in layer.get("items"):
                unique_name = item.get("unique_name")
                item_type = item.get("type")
                item_name = item.get("name")

                # Generic: map item id for all types
                for item_prop_name, item_props in item_props_in_scope.items():
                    primary_value = item.get(item_prop_name)
                    
                    if primary_value:
                        replace_value = {}
                        for env in target_environments:
                            dynamic_ref = f"$workspace.{workspace_name.replace('[dev]', f'[{env}]')}.$items.{item_type}.{item_name}.${item_prop_name}"
                            replace_value[env] = dynamic_ref

                        manage_find_replace(
                            yml_path=yaml_file,
                            action="upsert",
                            find_value=primary_value,
                            replace_value=replace_value,
                            comment=f"{item_type}: {unique_name} - {item_props.get('comment')}"
                        )

                        print_info(f"✔ Added dynamic reference for {item_type}: {unique_name} - {item_props.get('comment')}", bold=False)

                # Lakehouse-specific: add sqlendpoint and sqlendpointid
                if item_type == "Lakehouse":
                    # Derive dev sqlendpoint id from available properties, and address from connection string
                    dev_sqlendpointid = item.get("sqlendpointid") or item.get("sqlEndpointId")
                    # For Lakehouse, the 'connectionString' field already contains the FQDN endpoint.
                    dev_sqlendpoint_addr = item.get("connectionString")

                    # Map endpoint address (FQDN, already provided by Lakehouse properties)
                    if dev_sqlendpoint_addr:
                        replace_value = {}
                        for env in target_environments:
                            dynamic_ref = f"$workspace.{workspace_name.replace('[dev]', f'[{env}]')}.$items.{item_type}.{item_name}.$sqlendpoint"
                            replace_value[env] = dynamic_ref

                        manage_find_replace(
                            yml_path=yaml_file,
                            action="upsert",
                            find_value=dev_sqlendpoint_addr,
                            replace_value=replace_value,
                            comment=f"{item_type}: {unique_name} - SQL Endpoint address"
                        )

                        print_info(f"✔ Added dynamic reference for {item_type}: {unique_name} - SQL Endpoint address", bold=False)

                    # Map endpoint id
                    if dev_sqlendpointid:
                        replace_value = {}
                        for env in target_environments:
                            dynamic_ref = f"$workspace.{workspace_name.replace('[dev]', f'[{env}]')}.$items.{item_type}.{item_name}.$sqlendpointid"
                            replace_value[env] = dynamic_ref

                        manage_find_replace(
                            yml_path=yaml_file,
                            action="upsert",
                            find_value=dev_sqlendpointid,
                            replace_value=replace_value,
                            comment=f"{item_type}: {unique_name} - SQL Endpoint Guids"
                        )

                        print_info(f"✔ Added dynamic reference for {item_type}: {unique_name} - SQL Endpoint Guids", bold=False)

    print_success(f"Parameter file successfully updated in path {yaml_file}")


def get_lakehouse_connection_template(env_definition: dict, lakehouse_ws_layer: str, lakehouse_name: str) -> str:
    """
    Retrieve the connection name template for a lakehouse from the environment definition.

    Args:
        env_definition (dict): The environment definition containing layers and items.
        lakehouse_ws_layer (str): The layer name where the lakehouse is located (e.g., "Store").
        lakehouse_name (str): The name of the lakehouse to find (e.g., "Curated").

    Returns:
        str or None: The connection_name template for the lakehouse, or None if not found.
    """
    try:
        layer_def = env_definition.get("layers", {}).get(lakehouse_ws_layer, {})
        lakehouse_items = (layer_def.get("items", {}) or {}).get("Lakehouse", [])
        for itm in lakehouse_items:
            if str(itm.get("item_name")).strip() == lakehouse_name:
                return itm.get("connection_name")
    except Exception:
        pass
    
    return None


def parse_fabric_connection(conn_obj: dict) -> dict:
    """
    Parse a Fabric connection object and extract connection details.

    Expected structure:
    {
        'id': 'guid',
        'connectionDetails': {
            'path': 'endpoint.fabric.microsoft.com;DatabaseName',
            'type': 'SQL'
        }
    }

    Args:
        conn_obj (dict): The connection object from Fabric.

    Returns:
        dict: Contains 'connection_id', 'sqlendpoint', and 'database_name', or empty dict if parsing fails.
    """
    if not conn_obj or not isinstance(conn_obj, dict):
        return {}
    
    try:
        connection_id = conn_obj.get("id")
        path = (conn_obj.get("connectionDetails", {}) or {}).get("path", "")
        
        if not path:
            return {}
        
        # Parse path: "endpoint.fabric.microsoft.com;DatabaseName"
        parts = path.split(";")
        if len(parts) != 2:
            return {}
        
        sqlendpoint = parts[0].strip()
        database_name = parts[1].strip()
        
        return {
            "connection_id": connection_id,
            "sqlendpoint": sqlendpoint,
            "database_name": database_name
        }
    except Exception:
        return {}
    

def merge_permissions(layer_permissions, generic_permissions):
    """
    Merge layer and generic permissions, avoiding duplicates for the same identity.
    Returns a dict with unique permissions based on identity.
    """
    merged = {}
    
    # Process generic permissions first
    if generic_permissions:
        for permission, definitions in generic_permissions.items():
            if permission not in merged:
                merged[permission] = []
            merged[permission].extend(definitions)
    
    # Add layer permissions, skipping duplicates
    if layer_permissions:
        for permission, definitions in layer_permissions.items():
            if permission not in merged:
                merged[permission] = []
            
            for definition in definitions:
                # Check if this identity already exists in this permission level
                if not any(d.get('id') == definition.get('id') for d in merged[permission]):
                    merged[permission].append(definition)
    
    return merged


def update_expression_tmsl(
    expression: str,
    config: dict,
    new_value: str
) -> dict:
    """
    Returns a new config with the model expression updated.
    """
    updated = copy.deepcopy(config)

    def update_expression(expr: str) -> str:
        return re.sub(
            r'^"[^"]+"(?=\s+meta\s+\[)',
            f'"{new_value}"',
            expr
        )

    def walk(node):
        if isinstance(node, dict):
            if node.get("name") == expression and "expression" in node:
                node["expression"] = update_expression(node["expression"])

            for value in node.values():
                walk(value)

        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(updated)
    return updated


def update_expression_tmdl(
    expression: str,
    content: str,
    new_value: str
) -> str:
    pattern = (
        rf'(expression\s+{re.escape(expression)}\s*=\s*")'  # prefix
        r'([^"]+)'                             # value
        r'(".*meta\s*\[.*\])'                  # suffix
    )

    def replacer(match):
        return f"{match.group(1)}{new_value}{match.group(3)}"

    updated, count = re.subn(pattern, replacer, content)

    if count == 0:
        raise ValueError(f"{expression} expression not found")

    return updated