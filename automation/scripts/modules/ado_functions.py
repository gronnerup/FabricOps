import requests
import base64
from urllib.parse import quote

def get_ado_access_token(tenant_id, client_id, client_secret):
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
        "scope": "499b84ac-1321-427f-aa17-267ca6975798/.default"
    }

    response = requests.post(token_url, data=payload)
    response.raise_for_status()

    return response.json()["access_token"]


def build_headers(pat=None, tenant_id=None, client_id=None, client_secret=None):
    if pat is not None:
        auth = base64.b64encode(f":{pat}".encode()).decode()
        headers = {
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/json"
        }
    else:
        access_token = get_ado_access_token(tenant_id, client_id, client_secret)
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }

    return headers

def get_repository(org, project, repo_name, pat=None, tenant_id=None, client_id=None, client_secret=None):
    headers = build_headers(pat, tenant_id, client_id, client_secret)
    
    url = (f"https://dev.azure.com/{org}/{project}/_apis/git/repositories?api-version=7.1")

    response = requests.get(url, headers=headers)
    response.raise_for_status()

    repos = response.json()["value"]

    repo = next(
        (r for r in repos if r["name"].lower() == repo_name.lower()),
        None
    )

    if not repo:
        raise ValueError(f"Repository '{repo_name}' not found")

    return repo

def create_azure_pipeline(name, folder, pipeline_path, organization, project, repository, pat=None, tenant_id=None, client_id=None, client_secret=None):
    
    headers = build_headers(pat, tenant_id, client_id, client_secret)

    url = f"https://dev.azure.com/{organization}/{project}/_apis/pipelines?api-version=7.1"

    repo_id = get_repository(organization, project, repository, pat, tenant_id, client_id, client_secret).get("id")

    payload = {
        "name": name,
        "folder": folder,
        "configuration": {
            "type": "yaml",
            "path": pipeline_path,
            "repository": {
                "id": repo_id,   
                "type": "azureReposGit"
            }
        }
    }

    response = requests.post(url, headers=headers, json=payload)
    response.raise_for_status()
    return response.json()


def create_variable_group(name, variables, organization, project, pat=None, tenant_id=None, client_id=None, client_secret=None):
    
    headers = build_headers(pat, tenant_id, client_id, client_secret)

    url = f"https://dev.azure.com/{organization}/_apis/distributedtask/variablegroups?api-version=7.2-preview.2"

    payload = {
            "name": name,
            "providerData": None,
            "type": "Vsts",
            "variables": variables, 
            "variableGroupProjectReferences": [{
                "name": name,
                "projectReference": {
                    "name": project
                }
            }]
        }

    response = requests.post(url, headers=headers, json=payload)
    response.raise_for_status()
    return response.json()


def get_variable_group(name, organization, project, pat=None, tenant_id=None, client_id=None, client_secret=None):
    headers = build_headers(pat, tenant_id, client_id, client_secret)

    url = f"https://dev.azure.com/{organization}/{project}/_apis/distributedtask/variablegroups?groupName={name}&api-version=7.2-preview.2"

    response = requests.get(url, headers=headers)
    response.raise_for_status()
    if not response.json().get("value"):
        return None
    else:
        return response.json()["value"][0]


def get_project(organization, project_name, pat=None, tenant_id=None, client_id=None, client_secret=None):
    headers = build_headers(pat, tenant_id, client_id, client_secret)

    url = f"https://dev.azure.com/{organization}/_apis/projects/{project_name}?api-version=7.1"

    response = requests.get(url, headers=headers)
    response.raise_for_status()

    return response.json()


def delete_variable_group(name, organization, project, pat=None, tenant_id=None, client_id=None, client_secret=None):
    headers = build_headers(pat, tenant_id, client_id, client_secret)

    variable_group_response = get_variable_group(name, organization, project, pat, tenant_id, client_id, client_secret)

    if not variable_group_response:
        raise ValueError(f"Variable Group '{name}' not found")
    
    project_response = get_project(organization, project, pat, tenant_id, client_id, client_secret)

    url = f"https://dev.azure.com/{organization}/_apis/distributedtask/variablegroups/{variable_group_response.get('id')}?projectIds={project_response.get('id')}&api-version=7.2-preview.2"
    
    response = requests.delete(url, headers=headers)
    response.raise_for_status()

def get_definition(name, organization, project, pat=None, tenant_id=None, client_id=None, client_secret=None):
    headers = build_headers(pat, tenant_id, client_id, client_secret)

    encoded_name = quote(name, safe="")
    url = f"https://dev.azure.com/{organization}/{project}/_apis/build/definitions?name={encoded_name}&api-version=7.2-preview.7"

    response = requests.get(url, headers=headers)
    response.raise_for_status()

    return response.json()["value"][0]


def delete_azure_pipeline(name, organization, project, pat=None, tenant_id=None, client_id=None, client_secret=None):
    headers = build_headers(pat, tenant_id, client_id, client_secret)

    definition_id = get_definition(name, organization, project, pat, tenant_id, client_id, client_secret).get("id")

    url = f"https://dev.azure.com/{organization}/{project}/_apis/build/definitions/{definition_id}?api-version=7.0"

    response = requests.delete(url, headers=headers)
    response.raise_for_status()


def delete_definition_folder(folder_path, organization, project, pat=None, tenant_id=None, client_id=None, client_secret=None):
    headers = build_headers(pat, tenant_id, client_id, client_secret)

    url = f"https://dev.azure.com/{organization}/{project}/_apis/build/folders?path={folder_path}&api-version=7.2-preview.2"

    return requests.delete(url, headers=headers)


def set_variable_group_permissions(organization, project, variable_group_id, pipeline_id, pat=None, tenant_id=None, client_id=None, client_secret=None):
    headers = build_headers(pat, tenant_id, client_id, client_secret)

    url = f"https://dev.azure.com/{organization}/{project}/_apis/pipelines/pipelinePermissions/variablegroup/{variable_group_id}?api-version=7.1-preview.1"

    payload = {
        "resource":{},
        "pipelines": [
            {
            "id": pipeline_id,
            "authorized": True,
            "authorizedBy":None,
            "authorizedOn":None
            }
        ]
    }

    response = requests.patch(url, headers=headers, json=payload)
    response.raise_for_status()


def set_queue_build_permission(organization, project_name, folder_path, pipeline_name, pat=None, tenant_id=None, client_id=None, client_secret=None):
    headers = build_headers(pat, tenant_id, client_id, client_secret)

    project = get_project(organization, project_name, pat, tenant_id, client_id, client_secret)
    project_id= project.get('id')

    pipeline = get_definition(pipeline_name, organization, project_name, pat, tenant_id, client_id, client_secret)
    pipeline_id = pipeline.get('id')

    url = f"https://dev.azure.com/{organization}/_apis/AccessControlEntries/33344d9c-fc72-4d6f-aba5-fa317101a7e9?api-version=7.1"
   
    sp_list = get_service_principals(organization, pat, tenant_id, client_id, client_secret)
    origin_id = next(( item.get("originId") for item in sp_list.get("value", []) if item.get("applicationId") == client_id), None)

    if not origin_id:
        raise ValueError("Service principal originId not found")
    
    acl = get_acl(organization, pat, tenant_id, client_id, client_secret)
    target_suffix = f":Build:{project_id}"
    build_service_descriptor = None
    for entry in acl.get("value", []):
        aces = entry.get("acesDictionary", {})
        for key, ace in aces.items():
            if key.endswith(target_suffix):
                build_service_descriptor = ace.get("descriptor")

    folder_name = "/"

    if folder_path:
        path = folder_path.strip("/")
        folder_name = f"/{path}/"
    
    payload = {
        "token": f"{project_id}{folder_name}{pipeline_id}",
        "merge": True,
        "accessControlEntries": [
            {
                "descriptor": build_service_descriptor,
                "allow": 128,   # Queue builds
                "deny": 0,
                "extendedInfo":{"effectiveAllow":128,"effectiveDeny":0,"inheritedAllow":128,"inheritedDeny":0}
            }
        ]
    }

    response = requests.post(url, headers=headers, json=payload)
    response.raise_for_status()


def get_service_principals(organization, pat=None, tenant_id=None, client_id=None, client_secret=None):
    headers = build_headers(pat, tenant_id, client_id, client_secret)

    url= f"https://vssps.dev.azure.com/{organization}/_apis/graph/serviceprincipals?api-version=7.1-preview.1"
    response = requests.get(url, headers=headers)
    response.raise_for_status()

    return response.json()


def get_acl(organization, pat=None, tenant_id=None, client_id=None, client_secret=None):
    headers = build_headers(pat, tenant_id, client_id, client_secret)

    url= f"https://dev.azure.com/{organization}/_apis/accesscontrollists/2e9eb7ed-3c0a-47d4-87c1-0ffdd275fd87?api-version=7.1"
   
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()