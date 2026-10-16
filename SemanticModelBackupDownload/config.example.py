CONFIG = {
    "service_principal": {
        "tenant_id": "<tenant-id>",
        "client_id": "<application-id>",
        "client_secret": {
            "key_vault_url": "https://<vault-name>.vault.azure.net/",
            "secret_name": "<client-secret-name>",
        },
    },
    "archive": {
        # Use the ordinary Blob hostname; private DNS routes it to your private endpoint.
        "account_url": "https://<adls-account>.blob.core.windows.net",
        "container": "semantic-model-backups",  # Existing ADLS Gen2 filesystem.
        "prefix": "semantic-model-downloads",
    },
    # All semantic models are inventoried; unsupported downloads fail visibly.
    "workspaces": ["<source-workspace-id>"],
    # Each PBIX is downloaded to driver-local disk, validated, uploaded, then removed.
    # None uses the runtime's temporary directory. Never use a Lakehouse Files path
    # unless you intentionally want an additional persistent copy of sensitive data.
    "temp_directory": None,
    "max_download_bytes": 50 * 1024**3,
}
