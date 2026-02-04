#---------------------------------------------------------
# This script updates the connection string in a Power BI report definition file
# to point to the specified SQL Analytics endpoint, database, and semantic model ID.
# It also updates relevant model files in the specified model folder.
# Set the parameters below before running the script.
#
# Go to https://learn.microsoft.com/en-us/fabric/database/sql/connect 
# for more informantion on how to find the SQL Analytics endpoint.
#---------------------------------------------------------

sql_analytics_endpoint  = None # Only set this if you manually want to override the sql_analytics_endpoint variable. e.g. "te3-training-eu.database.windows.net"
semantic_model_id       = None # Only set this if you manually want to override the semantic_model_id variable. e.g. "d1b869ef-7890-45ad-95a1-c679e3ad675a"

#--------------------------------------------------------
# Advanced settings
# Only change if you know what you are doing
#---------------------------------------------------------
lakehouse_name          = "Curated"
semantic_model_name     = "YOUR_MODEL_NAME_HERE"
report_folder           = "solution/present/YOUR_REPORT_NAME_HERE.Report"
model_root_folder       = "solution/model"
store_layer             = "Store"
model_layer             = "Model"
dev_environment         = "dev"  # Environment to use for credentials

target_files = {
    "expressions.tmdl",
    "model.bim",
    "database.json",
    "sqlendpoint.json"
}

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
    misc.print_header(f"Update connections to semantic model in report and model files for environment")

    solution_name = env_definition.get("name")
    workspace_name = solution_name.format(layer=store_layer, environment=dev_environment)
    workspace_name_escaped = workspace_name.replace("/", "\\/")

    lakehouse = fabcli.get_item(f"/{workspace_name_escaped}.Workspace/{lakehouse_name}.Lakehouse", retry_count=2)
    if sql_analytics_endpoint is None:
        sql_analytics_endpoint = lakehouse.get("properties").get("sqlEndpointProperties").get("connectionString") 

    workspace_name = solution_name.format(layer=model_layer, environment=dev_environment)
    workspace_name_escaped = workspace_name.replace("/", "\\/")

    if semantic_model_id is None:
        semantic_model_id = fabcli.get_item_id(f"/{workspace_name_escaped}.Workspace/{semantic_model_name}.SemanticModel", retry_count=2)

    ### Replace in report definition file
    connection_string = (
        f'Data Source="powerbi://api.powerbi.com/v1.0/myorg/{workspace_name}";'
        f'initial catalog={semantic_model_name};'
        'access mode=readonly;'
        'integrated security=ClaimsToken;'
        f'semanticmodelid={semantic_model_id};'
    )

    file_path = os.path.join(os.path.dirname(__file__), f'../../../{report_folder}/definition.pbir')

    with open(file_path, "r", encoding="utf-8") as f:
        content = json.load(f)

    content["datasetReference"]["byConnection"]["connectionString"] = connection_string

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(content, f, indent=2)

    print("\033[1mUpdated semantic model reference!\033[0m")

    ### Replace in model files 
    model_root_folder = os.path.join(os.path.dirname(__file__), f'../../../{model_root_folder}')
    target_files_lower = {name.lower() for name in target_files}

    for root, _, files in os.walk(model_root_folder):
        for filename in files:
            if filename.lower() in target_files_lower:
                file_path = os.path.join(root, filename)
                if filename.lower().endswith((".bim", ".json")):
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = json.load(f)
                    
                    new_content = misc.update_expression_tmsl("SqlEndpoint", content, sql_analytics_endpoint)
                    new_content = misc.update_expression_tmsl("Database", new_content, lakehouse_name)
                    
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(json.dumps(new_content, indent=2, ensure_ascii=False))

                if filename.lower().endswith((".tmdl")):
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = f.read()

                    new_content = misc.update_expression_tmdl("SqlEndpoint", content, sql_analytics_endpoint)
                    new_content = misc.update_expression_tmdl("Database", new_content, lakehouse_name)

                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(new_content)

print("\033[1mUpdated model files!\033[0m")