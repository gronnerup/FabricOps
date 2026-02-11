#---------------------------------------------------------
# Default values
#---------------------------------------------------------
default_solution_path = ""
default_item_types_in_scope = "Notebook,DataPipeline,Lakehouse,SQLDatabase,SemanticModel,Report"
default_stages_in_scope = "core,ingest,store,prepare,orchestrate,model,insight,present"
default_environment = "tst"

#---------------------------------------------------------
# Main script
#---------------------------------------------------------
import os, sys, argparse, json
from pathlib import Path
from fabric_cicd import FabricWorkspace, publish_all_items, unpublish_all_orphan_items, change_log_level
import modules.fabric_cli_functions as fabcli
import modules.misc_functions as misc
from azure.identity import ClientSecretCredential

# Ensure stdout and stderr are line-buffered
sys.stdout.reconfigure(line_buffering=True, write_through=True)
sys.stderr.reconfigure(line_buffering=True, write_through=True)

# Get arguments 
parser = argparse.ArgumentParser(description="Fabric release arguments")
parser.add_argument("--environment", required=True, default=default_environment, help="Name of environment to release.")
parser.add_argument("--layers", required=False, default=default_stages_in_scope, help="Comma seperated list of layers to deploy. Can also be single layer.")
parser.add_argument("--item_types", required=False, default=default_item_types_in_scope, help="Comma seperated list of item types in scope. Must match Fabric ItemTypes exactly.")
parser.add_argument("--repo_path", required=False, default=default_solution_path, help="Path the the solution repository where items are stored.")
parser.add_argument("--is_debug", required=False, default=False, type=lambda x: x.lower() in ['true', '1', 'yes'], help="Enable debug logging.")
parser.add_argument("--unpublish_items", required=False, default=True, type=lambda x: x.lower() in ['true', '1', 'yes'], help="Whether to unpublish orphan items that are no longer in the repository. Default is True.")
parser.add_argument("--tenant_id", required=False, default=os.environ.get('TENANT_ID'), help="Azure Active Directory (Microsoft Entra ID) tenant ID used for authenticating with Fabric APIs. Defaults to the TENANT_ID environment variable.")
parser.add_argument("--client_id", required=False, default=os.environ.get('CLIENT_ID'), help="Client ID of the Azure AD application registered for accessing Fabric APIs. Defaults to the CLIENT_ID environment variable.")
parser.add_argument("--client_secret", required=False, default=os.environ.get('CLIENT_SECRET'), help="Client secret of the Azure AD application registered for accessing Fabric APIs. Defaults to the CLIENT_SECRET environment variable.")

args = parser.parse_args()
tenant_id = args.tenant_id
client_id = args.client_id
client_secret = args.client_secret
environment = args.environment
layers_to_deploy = [layers.strip().lower() for layers in args.layers.split(",")]
item_type_list = args.item_types.split(",")
repo_path = args.repo_path
is_debug = args.is_debug
unpublish_items = args.unpublish_items

# Uncomment to enable debug logging
if is_debug:
    change_log_level("DEBUG")

# Authenticate
fabcli.run_command("config set encryption_fallback_enabled true")
fabcli.run_command(f"auth login -u {client_id} -p {client_secret} --tenant {tenant_id}")

token_credential = ClientSecretCredential(client_id=client_id, client_secret=client_secret, tenant_id=tenant_id)

# Load JSON environment files (main and environment specific) and merge
main_json = misc.load_json(os.path.join(os.path.dirname(__file__), f'../resources/environments/infrastructure.json'))
env_json = misc.load_json(os.path.join(os.path.dirname(__file__), f'../resources/environments/infrastructure.{environment}.json'))
env_definition = misc.merge_json(main_json, env_json)

if env_definition:
    misc.print_header(f"Releasing - {environment}")
    
    solution_name = env_definition.get("name")
    layers = env_definition.get("layers")
    
    environment_parameters = {}

    for layer, layer_definition in layers.items():
        if layer.lower() in layers_to_deploy:        
            workspace_name = solution_name.format(layer=layer, environment=environment)
            workspace_name_escaped = workspace_name.replace("/", "\\/")

            workspace_id = fabcli.run_command(f"get '{workspace_name_escaped}.Workspace' -q id -f").strip()

            misc.print_subheader(f"Running release to workspace {workspace_name}!")

            target_workspace = FabricWorkspace(
                workspace_id=workspace_id,
                environment=environment,
                repository_directory=os.path.join(repo_path, layer.lower()),
                item_type_in_scope=item_type_list,
                token_credential=token_credential,
            )

            environment_parameters = {**target_workspace.environment_parameter, **environment_parameters}
            target_workspace.environment_parameter = environment_parameters

            publish_all_items(target_workspace)

            ### Support deployment to multiple layers in the same environment.
            ### This is done by adding the guid mappings to the environment parameter dictionary.
            if environment_parameters:
                for item_name in target_workspace.repository_items.values():
                    for item_details in item_name.values():
                        environment_parameters["find_replace"].append({
                            "find_value": item_details.logical_id,
                            "replace_value": {environment: item_details.guid}
                        })

            if unpublish_items:
                unpublish_all_orphan_items(target_workspace)

            # Bind Semantic Models to SQL Endpoints (if configured)
            try:
                bindings_yml = os.path.join(os.path.dirname(__file__), f"../resources/parameters/sqlendpoint_model_binding.yml")
                bindings = misc.get_semantic_model_bindings(bindings_yml, layer)

                if bindings:
                    misc.print_subheader("Binding semantic models to SQL endpoints")

                    for binding in bindings:
                        lakehouse_name = binding.get("lakehouse_name")
                        lakehouse_ws_layer = binding.get("lakehouse_ws_layer")
                        semantic_models = binding.get("semantic_models", [])

                        # Resolve lakehouse connection and SQL endpoint information
                        connection_name_template = misc.get_lakehouse_connection_template(env_definition, lakehouse_ws_layer, lakehouse_name)
                        connection_identifier = connection_name_template.format(environment=environment) if connection_name_template else None

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
                            continue

                        # Now bind all semantic models to this lakehouse
                        for semantic_model_name in semantic_models:
                            semantic_model_id = fabcli.run_command(f"get '/{workspace_name}.Workspace/{semantic_model_name}.SemanticModel' -q id -f").strip()
                            if not semantic_model_id:
                                misc.print_warning(f"Semantic model '{semantic_model_name}' not found in workspace {workspace_name}. Skip binding.")
                                continue

                            resp = fabcli.bind_semanticmodel_sqlendpoint(
                                workspace_id=workspace_id,
                                item_id=semantic_model_id,
                                connection_id=connection_id,
                                sqlendpoint=sqlendpoint,
                                database_name=database_name,
                            )
                            status = (resp or {}).get("status_code")
                            if status == 200:
                                misc.print_success(f"Binding '{semantic_model_name}' to SQL endpoint for lakehouse '{lakehouse_name}' successfully done.")
                            else:
                                misc.print_warning(f"Binding call returned non-success (status code {status}) for '{semantic_model_name}': {resp}")
                else:
                    misc.print_info("No semantic model bindings configured for this layer.")
            except Exception as e:
                misc.print_warning(f"Semantic model binding step encountered an error: {e}")
else:
    misc.print_error(f"No environment definition found for environment {environment}! Release of {environment} has been skipped.", True)