#---------------------------------------------------------
# This script creates or deletes GitHub Repository secrets
#---------------------------------------------------------
# Parameters
#---------------------------------------------------------
action = "setup"  # Action to perform: "setup" or "cleanup"
dev_environment = "dev"  # Environment to use for credentials

#---------------------------------------------------------
# Main script
#---------------------------------------------------------
import os, sys
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.append(os.getcwd())

import modules.github_functions as ghfunc, modules.auth_functions as authfunc, modules.misc_functions as misc

credentials = authfunc.get_environment_credentials(None, os.path.join(os.path.dirname(__file__), f'../../credentials/'))
github_pat = credentials.get("github_pat")
tenant_id = credentials.get("tenant_id")
client_id = credentials.get("client_id")
client_secret = credentials.get("client_secret")

# Load JSON environment files (main and development environment) and merge
main_json = misc.load_json(os.path.join(os.path.dirname(__file__), f'../../resources/environments/infrastructure.json'))
env_json = misc.load_json(os.path.join(os.path.dirname(__file__), f'../../resources/environments/infrastructure.{dev_environment}.json'))
env_definition = misc.merge_json(main_json, env_json)

git_settings = env_definition.get("generic").get("git_settings").get("gitProviderDetails")
owner = git_settings.get("ownerName") # GitHub owner/organization name
repository = git_settings.get("repositoryName") # GitHub repository name

if action.lower() == "cleanup":
    misc.print_header(f"Deleting GitHub Repository Secrets")

    secrets_to_delete = ["SPN_CLIENT_ID", "SPN_CLIENT_SECRET", "SPN_TENANT_ID", "GIT_PAT"]
    
    for secret_name in secrets_to_delete:
        misc.print_info(f"Deleting secret '{secret_name}'...", bold=True, end="")
        try:
            ghfunc.delete_secret(owner, repository, secret_name, github_pat)
            misc.print_success(" ✔ Done")
        except Exception as e:
            error_msg = str(e)
            if "404" in error_msg or "not found" in error_msg.lower():
                misc.print_warning(" ⚠ Not found")
            else:
                misc.print_error(f" ✖ Failed!")

elif action.lower() == "setup":
    ### Setup Repository Secrets
    misc.print_header(f"Setting up GitHub Repository Secrets")
    secrets = {
        "SPN_CLIENT_ID": client_id,
        "SPN_CLIENT_SECRET": client_secret,
        "SPN_TENANT_ID": tenant_id,
        "GIT_PAT": github_pat
    }

    for secret_name, secret_value in secrets.items():
        misc.print_info(f"Creating or updating secret '{secret_name}'...", bold=True, end="")
        try:
            if ghfunc.create_or_update_secret(owner, repository, secret_name, secret_value, github_pat):
                misc.print_success(" ✔ Done")
            else:
                misc.print_error(" ✖ Failed!")
        except Exception as e:
            error_msg = str(e)
            misc.print_error(f" ✖ Failed! Error: {error_msg}")