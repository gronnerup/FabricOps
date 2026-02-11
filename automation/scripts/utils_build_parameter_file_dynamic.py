#---------------------------------------------------------
# Dynamic Parameter File Builder - Dev-Only Scan
# This script generates/updates parameter.yml using dynamic values
# based on a scan of the dev environment only.
# Dynamic values like $workspace.SpaceParts - Core [tst] are used
# to automatically resolve IDs in target environments.
#---------------------------------------------------------
import os, sys, io, argparse
import modules.fabric_cli_functions as fabcli
import modules.misc_functions as misc
import shutil

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stdout.reconfigure(line_buffering=True)

# Get arguments
parser = argparse.ArgumentParser(description="Fabric IaC dynamic parameter file builder - dev environment only")
parser.add_argument("--tenant_id", required=False, default=os.environ.get('TENANT_ID'), help="Azure Active Directory (Microsoft Entra ID) tenant ID used for authenticating with Fabric APIs. Defaults to the TENANT_ID environment variable.")
parser.add_argument("--client_id", required=False, default=os.environ.get('CLIENT_ID'), help="Client ID of the Azure AD application registered for accessing Fabric APIs. Defaults to the CLIENT_ID environment variable.")
parser.add_argument("--client_secret", required=False, default=os.environ.get('CLIENT_SECRET'), help="Client secret of the Azure AD application registered for accessing Fabric APIs. Defaults to the CLIENT_SECRET environment variable.")
parser.add_argument("--target_environments", required=False, default="tst,prd", help="Comma separated list of target environments for parameter mapping (e.g., 'tst,prd'). Defaults to 'tst,prd'.")
parser.add_argument("--build_parameter_file", required=False, default=True, type=lambda x: x.lower() in ['true', '1', 'yes'], help="Build parameter file for Fabric deployments using dynamic values.")

args = parser.parse_args()

tenant_id = args.tenant_id
client_id = args.client_id
client_secret = args.client_secret
target_environments = [env.strip() for env in args.target_environments.split(",")]
build_parameter_file = args.build_parameter_file

# Authenticate
fabcli.run_command("config set encryption_fallback_enabled true")
fabcli.run_command(f"auth login -u {client_id} -p {client_secret} --tenant {tenant_id}")

dev_environment_data = {
    "name": "dev",
    "layers": []
}

if build_parameter_file:
    misc.print_header(f"Fetching dev environment details for dynamic parameter generation")

    environment = "dev"

    # Load JSON environment files (main and environment specific) and merge
    main_json = misc.load_json(os.path.join(os.path.dirname(__file__), f'../resources/environments/infrastructure.json'))
    env_json = misc.load_json(os.path.join(os.path.dirname(__file__), f'../resources/environments/infrastructure.{environment}.json'))
    env_definition = misc.merge_json(main_json, env_json)

    if env_definition:
        misc.print_info(f"Fetching details for {environment}...", bold=True)
        
        solution_name = env_definition.get("name")
        layers = env_definition.get("layers")
        default_capacity_name = env_definition.get("generic").get("capacity_name")

        for layer_name, layer_definition in layers.items():
            workspace_name = solution_name.format(layer=layer_name, environment=environment)
            workspace_name_escaped = workspace_name.replace("/", "\\/")
            
            misc.print_info(f"  Scanning workspace: {workspace_name}...", bold=False, end="")
            workspace_id = fabcli.run_command(f"get '{workspace_name_escaped}.Workspace' -q id -f").strip()
            
            if misc.is_guid(workspace_id):
                print(" ✔")
                workspace_items = fabcli.list_all_workspace_items(workspace_id)

                layer = {
                    "name": layer_name,
                    "workspace_name": workspace_name,
                    "workspace_id": workspace_id,
                    "items": []
                }
    
                # Get all items in the workspace
                for item in workspace_items:
                    fabric_item = {
                        "unique_name": f"{item.get('displayName')}.{item.get('type')}",
                        "name": item.get("displayName"),
                        "id": item.get("id"),
                        "type": item.get("type")
                    }

                    if item.get("type") in {"Lakehouse", "SQLDatabase"}:
                        item_details = fabcli.get_item(f"/{workspace_name_escaped}.Workspace/{item.get('displayName')}.{item.get('type')}", retry_count=1)
                        if item_details:
                            fabric_item.update({
                                "connectionString": item_details.get("properties").get("connectionString") if item.get("type") == "SQLDatabase" else item_details.get("properties").get("sqlEndpointProperties").get("connectionString"),
                                "databaseName": item_details.get("properties").get("databaseName") if item.get("type") == "SQLDatabase" else None,
                                "serverFqdn": item_details.get("properties").get("serverFqdn") if item.get("type") == "SQLDatabase" else None,
                                "sqlEndpointId": item_details.get("properties").get("sqlEndpointProperties").get("id") if item.get("type") == "Lakehouse" else None,
                            })
                            misc.print_info(f"    ✔ Fetched details for item: {item.get('displayName')} ({item.get('type')})", bold=False)
                        else:
                            misc.print_warning(f"    ⚠ Unable to fetch details for item: {item.get('displayName')} ({item.get('type')})", bold=False)

                    layer["items"].append(fabric_item)

                # Get all layer connections
                if layer_definition.get("items"):
                    for item_type, items in layer_definition.get("items").items():
                        for item in items: 
                            if item.get("connection_name") and item_type in {"Lakehouse", "SQLDatabase"}:
                                connection_name = item.get("connection_name").format(layer=layer_name, environment=environment)
                                if fabcli.connection_exists(connection_name):
                                    connection = fabcli.get_item(f".connections/{connection_name}.Connection")
                                    upd_item = next((i for i in layer["items"] if i.get('unique_name') == f"{item.get('item_name')}.{item_type}"), None)
                                    if upd_item:
                                        upd_item['connectionId'] = connection.get("id")

                dev_environment_data["layers"].append(layer)
            else:
                print(" ⚠ (workspace not found)")
        
        print("")
        
        # Build dynamic parameter file
        parameter_file_src = os.path.join(os.path.dirname(__file__), "../resources/parameters/parameter.yml")
        misc.build_parameter_yml_dynamic(parameter_file_src, dev_environment_data, target_environments)

        # Copy the updated parameter file to each solution folder
        misc.print_info(f"Copying parameter file to solution folders...", bold=True)
        solution_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../solution'))
        for folder in os.listdir(solution_root):
            folder_path = os.path.join(solution_root, folder)
            if os.path.isdir(folder_path):
                dest_path = os.path.join(folder_path, 'parameter.yml')
                shutil.copyfile(parameter_file_src, dest_path)
                misc.print_info(f"  Copied to {folder}/parameter.yml", bold=False)
        
        print("")
        misc.print_success("Parameter file generation completed successfully!")
    else:
        misc.print_error(f"No environment definition found for {environment}... Exiting!")
