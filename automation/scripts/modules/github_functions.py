import requests
import base64

def build_headers(pat):
    """Build authorization headers for GitHub API using PAT."""
    auth = base64.b64encode(f":{pat}".encode()).decode()
    headers = {
        "Authorization": f"token {pat}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json"
    }
    return headers


def get_repository(owner, repo_name, pat):
    """Get repository details from GitHub."""
    headers = build_headers(pat)
    url = f"https://api.github.com/repos/{owner}/{repo_name}"
    
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    
    return response.json()


def get_public_key(owner, repo_name, pat):
    """Get the repository's public key for encrypting secrets."""
    headers = build_headers(pat)
    url = f"https://api.github.com/repos/{owner}/{repo_name}/actions/secrets/public-key"
    
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    
    return response.json()


def encrypt_secret(public_key_str, secret_value):
    """Encrypt a secret using the repository's public key."""
    from nacl import utils, public, encoding
    
    # Decode the public key
    public_key = public.PublicKey(public_key_str.encode('utf-8'), encoder=encoding.Base64Encoder)
    
    # Encrypt the secret
    sealed_box = public.SealedBox(public_key)
    encrypted = sealed_box.encrypt(secret_value.encode('utf-8'))
    
    # Return base64 encoded encrypted secret
    return base64.b64encode(encrypted).decode('utf-8')


def create_or_update_secret(owner, repo_name, secret_name, secret_value, pat):
    """Create or update a repository secret."""
    headers = build_headers(pat)
    
    # Get the public key for encryption
    public_key_response = get_public_key(owner, repo_name, pat)
    public_key_id = public_key_response.get('key_id')
    public_key = public_key_response.get('key')
    
    # Encrypt the secret
    encrypted_value = encrypt_secret(public_key, secret_value)
    
    # Create or update the secret
    url = f"https://api.github.com/repos/{owner}/{repo_name}/actions/secrets/{secret_name}"
    
    payload = {
        "encrypted_value": encrypted_value,
        "key_id": public_key_id
    }
    
    response = requests.put(url, headers=headers, json=payload)
    response.raise_for_status()
    
    return response.status_code in [201, 204]


def delete_secret(owner, repo_name, secret_name, pat):
    """Delete a repository secret."""
    headers = build_headers(pat)
    url = f"https://api.github.com/repos/{owner}/{repo_name}/actions/secrets/{secret_name}"
    
    response = requests.delete(url, headers=headers)
    response.raise_for_status()


def list_secrets(owner, repo_name, pat):
    """List all secrets in a repository."""
    headers = build_headers(pat)
    url = f"https://api.github.com/repos/{owner}/{repo_name}/actions/secrets"
    
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    
    return response.json()


def secret_exists(owner, repo_name, secret_name, pat):
    """Check if a secret exists in the repository."""
    headers = build_headers(pat)
    url = f"https://api.github.com/repos/{owner}/{repo_name}/actions/secrets/{secret_name}"
    
    response = requests.get(url, headers=headers)
    
    return response.status_code == 200
