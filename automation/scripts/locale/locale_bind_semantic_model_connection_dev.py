#---------------------------------------------------------
# This script binds the SpaceParts semantic model to 
# the appropriate Lakehouse connection in development
# 
#---------------------------------------------------------
# Advanced settings
# Only change if you know what you are doing
#---------------------------------------------------------
lakehouse_name          = "Curated"
semantic_model_name     = "YOUR_MODEL_NAME_HERE"
store_layer             = "Store"
model_layer             = "Model"
dev_environment         = "dev"  # Environment to use for credentials

#---------------------------------------------------------
# Main script
#---------------------------------------------------------
import os, sys, json
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.append(os.getcwd())

import modules.auth_functions as authfunc
import modules.misc_functions as misc
import modules.fabric_cli_functions as fabcli

credentials = authfunc.get_environment_credentials(None, os.path.join(os.path.dirname(__file__), f'../../credentials/'))

# Authenticate
fabcli.run_command("config set encryption_fallback_enabled true")
fabcli.run_command(f"auth login -u {credentials.get('client_id')} -p {credentials.get('client_secret')} --tenant {credentials.get('tenant_id')}")

# Load JSON environment files (main and development environment) and merge
main_json = misc.load_json(os.path.join(os.path.dirname(__file__), f'../../resources/environments/infrastructure.json'))
env_json = misc.load_json(os.path.join(os.path.dirname(__file__), f'../../resources/environments/infrastructure.{dev_environment}.json'))
env_definition = misc.merge_json(main_json, env_json)

if env_definition:    
    misc.print_header(f"Bind Semantic model connection for xSpaceParts Semantic Model in {dev_environment} environment")
    solution_name = env_definition.get("name")
    
    # Resolve lakehouse connection and SQL endpoint information
    workspace_name = solution_name.format(layer=model_layer, environment=dev_environment).replace("/", "\\/")
    workspace_id = fabcli.run_command(f"get '{workspace_name}.Workspace' -q id").strip()

    semantic_model_id = fabcli.get_item_id(f"/{workspace_name}.Workspace/{semantic_model_name}.SemanticModel", retry_count=2)

    connection_name_template = misc.get_lakehouse_connection_template(env_definition, store_layer, lakehouse_name)
    connection_identifier = connection_name_template.format(environment=dev_environment) if connection_name_template else None

    connection_id = None
    database_name = None
    sqlendpoint = None
    if connection_identifier:
        conn_obj = fabcli.get_connection(connection_identifier)
        if conn_obj:
            conn_details = misc.parse_fabric_connection(conn_obj)
            connection_id = conn_details.get("connection_id")
            sqlendpoint = conn_details.get("sqlendpoint")
            database_name = conn_details.get("database_name")

    # Check if connection information was resolved successfully
    if not (connection_id and sqlendpoint and database_name):
        misc.print_warning(f"Connection information for {lakehouse_name} is incomplete. Skipping all models for this lakehouse.")

    # Takeover semantic model if needed
    misc.print_info(f"Service Principal taking over semantic model '{semantic_model_name}' in workspace '{workspace_name}...", end="")
    takeover_response = fabcli.takeover_semantic_model(workspace_id, semantic_model_id)
    if (takeover_response or {}).get("status_code") == 200:
        misc.print_success(" ✔ Done")
    else:
        misc.print_error(f" ✖ Failed!")

    misc.print_info(f"Binding semantic model '{semantic_model_name}' in workspace '{workspace_name}' to connection {connection_identifier}...", end="")
    
    resp = fabcli.bind_semanticmodel_sqlendpoint(
        workspace_id=workspace_id,
        item_id=semantic_model_id,
        connection_id=connection_id,
        sqlendpoint=sqlendpoint,
        database_name=database_name
    )

    if (takeover_response or {}).get("status_code") == 200:
        misc.print_success(" ✔ Done")
    else:
        misc.print_error(f" ✖ Failed!")