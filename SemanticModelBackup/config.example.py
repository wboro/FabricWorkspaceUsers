CONFIG = {
    "mode": "backup",  # backup or restore
    "service_principal": {
        # Each value accepts a literal string or the Key Vault reference below.
        "tenant_id": "<tenant-id>",
        "client_id": "<application-id>",
        "client_secret": {
            "key_vault_url": "https://<vault-name>.vault.azure.net/",
            "secret_name": "<client-secret-name>",
        },
        # For local testing only, client_secret can instead be "<client-secret>".
    },
    "archive": {
        "account_url": "https://<archive-account>.blob.core.windows.net",
        "container": "semantic-model-backups",  # Must already exist.
        "prefix": "semantic-models",
    },
    # Native XMLA backup requires ADLS Gen2 linked to each workspace in Power BI.
    # This is staging storage, not necessarily the archive account above.
    "staging_account_url": "https://<linked-adls-account>.blob.core.windows.net",
    "backup": {
        "temporary_adls": {
            # Connect ADLS ONCE in the workspace running this notebook and keep it linked.
            # The control workspace ID comes from runtime, including after moving tenants.
            # Opt in: temporarily attach that storage only to unlinked source workspaces.
            # Uses the notebook execution USER for connection management, not the SP.
            "enabled": False,
            # Power BI registration GUID, NOT an Azure ARM resource ID.
            # None: use the runtime control workspace's enabled registration.
            # No tenant-level storage settings are read or changed.
            "dataflow_storage_id": None,
            "timeout_seconds": 300,
        },
        "workspaces": [
            {
                "id": "<source-workspace-id>",
                # Optional overrides when workspaces use different linked accounts:
                # "staging_account_url": "https://other.blob.core.windows.net",
                # "staging_folder": "Exact folder name in power-bi-backup",
                # Optional temporary-attachment registration override:
                # "dataflow_storage_id": "<power-bi-storage-registration-id>",
            },
        ],
        # Optional ABF encryption. Keep the same secret/version available at restore.
        "password": None,
        # "password": {"key_vault_url": "https://vault.vault.azure.net/",
        #              "secret_name": "abf-password"},
    },
    "restore": {
        # Explicit run ID printed by backup; never implicitly chooses "latest".
        "run_id": "<backup-run-id>",
        "dry_run": True,
        "allow_overwrite": False,
        "password": None,
        "workspaces": [
            {
                "source_workspace_id": "<source-workspace-id>",
                "target_workspace_id": "<target-workspace-id>",
                # None means every successfully backed-up model in this workspace.
                "model_ids": None,
                # Optional source-model-ID -> new model name mapping:
                "model_names": {},
                # Optional linked storage overrides for the TARGET workspace:
                # "staging_account_url": "https://target.blob.core.windows.net",
                # "staging_folder": "Target workspace folder",
            },
        ],
    },
}
