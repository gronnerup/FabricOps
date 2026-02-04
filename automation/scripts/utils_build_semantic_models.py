import os, sys, argparse, shutil, subprocess, json
from datetime import datetime
from pathlib import Path
import uuid

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '../..'))
sys.path.append(os.getcwd())

start_time = datetime.now()

# Template for definition.pbism file
DEFINITION_PBISM_TEMPLATE = {
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
    "version": "4.2",
    "settings": {}
}

# Template for .platform file
PLATFORM_TEMPLATE = {
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
    "metadata": {
        "type": "SemanticModel",
        "displayName": ""
    },
    "config": {
        "version": "2.0",
        "logicalId": ""
    }
}

default_model_dir= f"{os.getenv('BUILD_SOURCEDIRECTORY')}\\solution\\model"
parser = argparse.ArgumentParser(description="Semantic model build script arguments")
parser.add_argument("--model_dir", required=False, default=default_model_dir, help="Repository containing semantic models.")
parser.add_argument("--tabulareditor_dir", required=False, default=None, help="Directory where Tabular Editor 2.x executable file is stored.")

args = parser.parse_args()
model_dir = Path(args.model_dir)
tabulareditor_directory = Path(args.tabulareditor_dir)

print("Building Semantic models")

if model_dir and os.path.exists(model_dir):
    with os.scandir(model_dir) as models:
        models_list = list(models)
        if models_list:
            for model in models_list:
                if model.is_dir():
                    model_name = model.name
                    model_source_path = model.path
                    
                    # Check if this folder contains a semantic model (database.json or model.bim)
                    database_json_path = os.path.join(model_source_path, "database.json")
                    model_bim_path = os.path.join(model_source_path, "model.bim")
                    
                    source_file = None
                    if os.path.exists(database_json_path):
                        source_file = database_json_path
                        print(f"Found database.json in {model_name}")
                    elif os.path.exists(model_bim_path):
                        source_file = model_bim_path
                        print(f"Found model.bim in {model_name}")
                    
                    if source_file:
                        print(f"Converting model {model_name}...")
                        
                        # Create output path: <model_name>.SemanticModel/definition
                        output_folder_name = f"{model_name}.SemanticModel"
                        output_base_path = os.path.join(model_dir, output_folder_name)
                        output_definition_path = os.path.join(output_base_path, "definition")
                        
                        # Create the output directory structure
                        os.makedirs(output_definition_path, exist_ok=True)
                        
                        # Tabular Editor executable and conversion
                        te_exec = os.path.join(tabulareditor_directory, "TabularEditor.exe")
                        
                        # Run the conversion to TMDL format
                        subprocess.run([
                            te_exec, source_file, 
                            "-TMDL", output_definition_path
                        ], check=True)
                        
                        print(f"  Converted to {output_folder_name}/definition")
                        
                        # Create definition.pbism file
                        definition_pbism_path = os.path.join(output_base_path, "definition.pbism")
                        with open(definition_pbism_path, 'w', encoding='utf-8') as f:
                            json.dump(DEFINITION_PBISM_TEMPLATE, f, indent=2)
                        print(f"  Created definition.pbism")
                        
                        # Create .platform file with model name and new GUID
                        platform_content = PLATFORM_TEMPLATE.copy()
                        platform_content["metadata"]["displayName"] = model_name
                        platform_content["config"]["logicalId"] = str(uuid.uuid4())
                        
                        platform_path = os.path.join(output_base_path, ".platform")
                        with open(platform_path, 'w', encoding='utf-8') as f:
                            json.dump(platform_content, f, indent=2)
                        print(f"  Created .platform with logicalId: {platform_content['config']['logicalId']}")
                        
                        #Delete the original source folder (commented out for now)
                        if os.path.exists(model_source_path) and model_source_path != output_base_path:
                            shutil.rmtree(model_source_path)
                        
                        print("  Done!")
                    else:
                        print(f"Skipping {model_name} - no database.json or model.bim found")
        else:
            print(f"No folders found in source directory {model_dir.resolve()}.")
else:
    print("Source directory is not set or does not exist.")

duration = datetime.now() - start_time
print(f"Script duration: {duration}")