import os, argparse, json
import modules.fabric_cli_functions as fabcli
import modules.misc_functions as misc

default_environment = "dev"

# Get arguments 
parser = argparse.ArgumentParser(description="Fabric feature maintainance arguments")
parser.add_argument("--tenant_id", required=False, default=os.environ.get('TENANT_ID'), help="Azure Active Directory (Microsoft Entra ID) tenant ID used for authenticating with Fabric APIs. Defaults to the TENANT_ID environment variable.")
parser.add_argument("--client_id", required=False, default=os.environ.get('CLIENT_ID'), help="Client ID of the Azure AD application registered for accessing Fabric APIs. Defaults to the CLIENT_ID environment variable.")
parser.add_argument("--client_secret", required=False, default=os.environ.get('CLIENT_SECRET'), help="Client secret of the Azure AD application registered for accessing Fabric APIs. Defaults to the CLIENT_SECRET environment variable.")
parser.add_argument("--environment", required=False, default=default_environment, help="The environment to operate on. Defaults to a predefined variable `environment`.")

args = parser.parse_args()
tenant_id = args.tenant_id
client_id = args.client_id
client_secret = args.client_secret
environment = args.environment

# Load JSON environment files (main and environment specific) and merge
main_json = misc.load_json(os.path.join(os.path.dirname(__file__), f'../resources/environments/infrastructure.json'))
env_json = misc.load_json(os.path.join(os.path.dirname(__file__), f'../resources/environments/infrastructure.{environment}.json'))
env_definition = misc.merge_json(main_json, env_json)

if env_definition:
    solution_name = env_definition.get("name")
    layers = env_definition.get("layers")

    fabcli.run_command("config set encryption_fallback_enabled true")
    fabcli.run_command(f"auth login -u {client_id} -p {client_secret} --tenant {tenant_id}")

    # Perform workspace synchronization for all layers
    misc.print_header(f"Synchronizing environment workspaces")
    for layer, layer_definition in layers.items():
            workspace_name = solution_name.format(layer=layer, environment=environment)
            workspace_name_escaped = workspace_name.replace("/", "\\/")

            if layer_definition.get("git_synchronize_on_commit", True) and not layer_definition.get("git_disconnect_after_initialize", False):
                misc.print_info(f"Synchronizing workspace {workspace_name_escaped} with latest changes from Git repo...", bold=True, end="")
                workspace_id = fabcli.run_command(f"get '{workspace_name_escaped}.Workspace' -q id -f").strip()

                git_status = fabcli.get_git_status(workspace_id)
                if git_status is None:
                    misc.print_warning(" ⚠ Git synchronization not possible.")
                    continue
                elif git_status.get("workspaceHead") == git_status.get("remoteCommitHash"):
                    misc.print_warning(" ⚠ Already up to date.")
                    continue
                elif len(git_status.get("changes")) == 0:
                    misc.print_warning(" ⚠ No changes detected.")
                    continue
                else:
                    fabcli.update_workspace_from_git(workspace_id, git_status.get("remoteCommitHash"))
                    misc.print_success(" ✔")

    misc.print_success(f"Environment workspaces synchronized!",bold = True)
