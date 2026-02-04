#---------------------------------------------------------
# This script creates or deletes Azure DevOps pipelines and variable groups.
# Set the parameters below before running the script.
#---------------------------------------------------------
# Parameters
#---------------------------------------------------------
action = "setup"  # Action to perform: "setup" or "cleanup"

variable_group_name = "Fabric_Automation"
dev_environment = "dev"  # Environment to use for credentials

#---------------------------------------------------------
# Advanced Parameters - usually do not change
#---------------------------------------------------------
pipelines = [
    {   
        "name": "PR - BPA Validation",
        "pipeline_path": ".azure-pipelines/pr-validation.yml",
        "folder": "Build Validation"
    },
    {   
        "name": "Cleanup feature workspaces",
        "pipeline_path": ".azure-pipelines/feature_fabric_cleanup.yml",
        "folder": "CICD"
    },
    {   
        "name": "Create feature workspaces",
        "pipeline_path": ".azure-pipelines/feature_fabric_branch.yml",
        "folder": "CICD"
    },
    {   
        "name": "Solution - Release (Octopus Deploy)",
        "pipeline_path": ".azure-pipelines/solution_release_octopus.yml",
        "folder": "CICD"
    },
    {   
        "name": "Solution - Release (Multi-stage)",
        "pipeline_path": ".azure-pipelines/solution_release_multistages.yml",
        "folder": "CICD"
    },
    {   
        "name": "Solution - Release (Single stage)",
        "pipeline_path": ".azure-pipelines/solution_release_single_stage.yml",
        "folder": "CICD",
        "set_queue_permission": True
    },
    {   
        "name": "Solution IaC - Setup",
        "pipeline_path": ".azure-pipelines/solution_setup.yml",
        "folder": "IaC"
    },
    {   
        "name": "Solution IaC - Teardown",
        "pipeline_path": ".azure-pipelines/solution_cleanup.yml",
        "folder": "IaC"
    },
]

#---------------------------------------------------------
# Main script
#---------------------------------------------------------
import os, sys
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.append(os.getcwd())

import modules.ado_functions as adofunc, modules.auth_functions as authfunc, modules.misc_functions as misc

credentials = authfunc.get_environment_credentials(None, os.path.join(os.path.dirname(__file__), f'../../credentials/'))
pat = credentials.get("ado_pat")
tenant_id = credentials.get("tenant_id")
client_id = credentials.get("client_id")
client_secret = credentials.get("client_secret")

# Load JSON environment files (main and development environment) and merge
main_json = misc.load_json(os.path.join(os.path.dirname(__file__), f'../../resources/environments/infrastructure.json'))
env_json = misc.load_json(os.path.join(os.path.dirname(__file__), f'../../resources/environments/infrastructure.{dev_environment}.json'))
env_definition = misc.merge_json(main_json, env_json)

git_settings = env_definition.get("generic").get("git_settings").get("gitProviderDetails")
organization = git_settings.get("organizationName") # Name of Azure DevOps organization
project = git_settings.get("projectName") # Name of Azure DevOps project
repository = git_settings.get("repositoryName") # Name of Azure DevOps repository

if action.lower() == "cleanup":
    misc.print_header(f"Deleting Azure DevOps Variable Groups and Pipelines")

    misc.print_info(f"Deleting Variable Group '{variable_group_name}'...", bold=True, end="")
    try:
        adofunc.delete_variable_group(variable_group_name, organization, project, pat, tenant_id, client_id, client_secret)
        misc.print_success(" ✔ Done")
    except Exception as e:
        error_msg = str(e)
        if "404" in error_msg or "not found" in error_msg.lower():
            misc.print_warning(" ⚠ Not found")
        else:
            misc.print_error(f" ✖ Failed!")

    # Delete all pipelines by distinct folders using adofunc.delete_definition_folder
    folders = sorted({p.get('folder') for p in pipelines if p.get('folder')})
    for folder in folders:
        misc.print_info(f"Deleting pipeline folder '{folder}'...", bold=True, end="")
        response = adofunc.delete_definition_folder(
                folder_path=folder,
                organization=organization,
                project=project,
                pat=pat,
                tenant_id=tenant_id,
                client_id=client_id,
                client_secret=client_secret
            )
        misc.print_success(" ✔ Done")

elif action.lower() == "setup":
    ### Setup Variable Groups
    misc.print_header(f"Setting up Variable Group in Azure DevOps")
    variables = {
        "SPN_CLIENT_ID": {
        "value": client_id,
        "isSecret": True
        },
        "SPN_CLIENT_SECRET": {
        "value": client_secret,
        "isSecret": True
        },
        "SPN_TENANT_ID": {
        "value": tenant_id,
        "isSecret": True
        }
    }

    misc.print_info(f"Creating variable group '{variable_group_name}' and setting variables...", bold=True, end="")
    variable_group = None
    try:
        variable_group = adofunc.create_variable_group(variable_group_name, variables, organization, project, pat, tenant_id, client_id, client_secret)
        misc.print_success(" ✔ Done")
    except Exception as e:
        error_msg = str(e)
        if "409" in error_msg or "already exists" in error_msg.lower():
            misc.print_warning(" ⚠ Already exists")
            variable_group = adofunc.get_variable_group(variable_group_name, organization, project, pat, tenant_id, client_id, client_secret)
        else:
            misc.print_error(f" ✖ Failed!")

    ### Setup Pipelines
    misc.print_header(f"Setting up Azure DevOps Pipelines")

    for pipeline in pipelines:
        misc.print_info(f"Creating pipeline '{pipeline.get('name')}'...", bold=True, end="")
        pipeline_def = None
        try:
            pipeline_response = adofunc.create_azure_pipeline(
                name=pipeline.get('name'),
                folder=pipeline.get('folder'),
                pipeline_path=pipeline.get('pipeline_path'),
                organization=organization,
                project=project,
                repository=repository,
                pat=pat,
                tenant_id=tenant_id,
                client_id=client_id,    
                client_secret=client_secret
            )
            
            status_code = pipeline_response.get("status_code") if isinstance(pipeline_response, dict) else getattr(pipeline_response, 'status_code', None)
                
            misc.print_success(" ✔ Done")

        except Exception as e:
            error_msg = str(e)
            if "409" in error_msg or "already exists" in error_msg.lower():
                pipeline_response = adofunc.get_definition(pipeline.get('name'), organization, project, pat, tenant_id, client_id, client_secret)
                misc.print_warning(" ⚠ Already exists")
            else:
                misc.print_error(f" ✖ Failed!")


        # Assign variable group permissions to the pipeline
        if variable_group and pipeline_response:
            try:
                misc.print_info(f" - Setting variable group permissions for pipeline '{pipeline.get('name')}'...", bold=False, end="")
                permission_repsonse = adofunc.set_variable_group_permissions(
                    organization=organization, 
                    project=project,
                    variable_group_id=variable_group.get("id"),
                    pipeline_id=pipeline_response.get("id"),
                    pat=pat,
                    tenant_id=tenant_id,
                    client_id=client_id,
                    client_secret=client_secret
                )
                misc.print_success(" ✔ Done")
            except Exception as e:
                misc.print_error(f" ✖ Failed!")

            if pipeline.get("set_queue_permission", False):
                misc.print_info(f" - Setting queue permission for pipeline '{pipeline.get('name')}'...", bold=False, end="")
                try:
                    adofunc.set_queue_build_permission(
                        organization,
                        project,
                        pipeline.get('folder'),
                        pipeline.get('name'),
                        pat=pat,
                        tenant_id=tenant_id,
                        client_id=client_id,    
                        client_secret=client_secret
                        )
                    misc.print_success(" ✔ Done")
                except Exception as e:
                    misc.print_error(f" ✖ Failed!")