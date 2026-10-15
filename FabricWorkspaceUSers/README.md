# Fabric Workspace Users and Locations

`fabric_workspace_users_locations.py` inventories users with access to all
Microsoft Fabric workspaces in the tenant and enriches those users with
location information from Microsoft Entra ID through Microsoft Graph.

The script uses an interactive Azure CLI user session. It does not require a
custom app registration, client ID, client secret, or service principal.

## Prerequisites

- Python 3.10 or later.
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli).
- A Microsoft Entra user assigned the Fabric administrator role.
- Permission to read the required Microsoft Graph user and group information.

## Installation

Open PowerShell in this folder and install the Python dependency:

```powershell
python -m pip install -r requirements.txt
```

## Authentication

Sign in interactively with Azure CLI:

```powershell
az login
```

For a specific tenant:

```powershell
az login --tenant <tenant-id>
```

The script obtains two separate delegated access tokens from the Azure CLI
session:

- A Fabric token for `https://api.fabric.microsoft.com`.
- A Microsoft Graph token for Microsoft Entra user and group queries.

Azure CLI authentication supports browser sign-in, MFA, Conditional Access, and
Windows Web Account Manager. The signed-in identity must be a user identity.
The script rejects service principal and managed identity Azure CLI sessions.

## Run the script

Run the script from the folder where the CSV files should be created:

```powershell
python .\fabric_workspace_users_locations.py
```

Tenant-wide mode is the default. It uses the stable Power BI Admin API to list
all active tenant workspaces and expand their direct users and groups.

For a non-admin user, run the previous user-scoped behavior:

```powershell
python .\fabric_workspace_users_locations.py --accessible-only
```

This fallback returns only workspaces visible to the signed-in user.

To include deleted and removing workspaces in tenant-wide mode:

```powershell
python .\fabric_workspace_users_locations.py --include-inactive-workspaces
```

To select a tenant when the script needs to start an Azure CLI login:

```powershell
python .\fabric_workspace_users_locations.py --tenant-id <tenant-id>
```

To avoid expanding an `EntireTenant` workspace assignment into every tenant
user:

```powershell
python .\fabric_workspace_users_locations.py --exclude-entire-tenant
```

Additional options:

```powershell
python .\fabric_workspace_users_locations.py --help
```

## Output files

The script creates the following files in the current working directory:

### `workspaces.csv`

Contains one row for every workspace returned by the selected mode. In the
default tenant-wide mode, this includes active workspaces whether or not they
have any assigned users. Use `--include-inactive-workspaces` to also include
deleted and removing workspaces.

### `workspace_user_access.csv`

Contains effective workspace access for individual users. Direct user
assignments, group membership, and optionally tenant-wide assignments are
included.

Important columns include:

- `workspaceId`, `workspaceName`, and `workspaceType`
- `role`
- `userId`, `displayName`, and `userPrincipalName`
- `accessPath`: `Direct`, `Group`, or `EntireTenant`
- `assignedPrincipalId`, `assignedPrincipalName`, and `assignedPrincipalType`

A user can appear more than once when the user has access through multiple
roles, groups, or workspaces.

### `workspace_user_locations.csv`

Contains one row per distinct Microsoft Entra user found in the workspace
inventory. Location and organizational properties include:

- `city`
- `state`
- `country`
- `officeLocation`
- `usageLocation`
- `postalCode`
- `streetAddress`
- `companyName`
- `department`
- `jobTitle`

Empty values normally mean that the corresponding property isn't populated in
Microsoft Entra ID. `graphLookupError` records user-specific Graph lookup
failures.

### `workspace_inspection_errors.csv`

In `--accessible-only` mode, contains visible workspaces whose role assignments
could not be inspected. It is normally empty in tenant-wide admin mode.

## Permission considerations

Tenant-wide mode uses the Power BI Admin API and requires:

- The signed-in user to have the **Fabric administrator** tenant role.
- Authorization to call tenant admin APIs. Microsoft documents
  `Tenant.Read.All` or `Tenant.ReadWrite.All` for standard delegated
  applications. The Microsoft Azure CLI first-party token can instead expose
  its preconsented `user_impersonation` scope, as determined by the tenant.

The API returns direct workspace principals for every tenant workspace.
Microsoft Graph is then used to expand groups into effective users.

In `--accessible-only` mode, listing a workspace's role assignments requires
the signed-in user to be a workspace **Member** or **Admin**. Workspaces where
the user has only Contributor or Viewer access can appear in
`workspace_inspection_errors.csv`.

Microsoft Graph must allow the Azure CLI enterprise application and signed-in
user to read tenant users and transitive group membership. The required access
is typically equivalent to delegated:

- `User.Read.All`
- `GroupMember.Read.All`

These permissions can require tenant administrator consent. The script does not
create an Entra application or grant permissions. If the tenant hasn't granted
adequate access to the Microsoft Azure CLI enterprise application, Graph
directory operations return HTTP 403.

## Troubleshooting

### Azure CLI isn't installed

Install Azure CLI and confirm that this command works:

```powershell
az --version
```

### No active Azure CLI session

Run:

```powershell
az login --tenant <tenant-id>
```

Then verify the active user:

```powershell
az account show --query "{user:user.name,userType:user.type,tenantId:tenantId}"
```

`userType` must be `user`.

### Microsoft Graph returns HTTP 403

The authentication token is valid, but it doesn't have sufficient delegated
directory permissions. Ask a tenant administrator to verify the permissions
granted to the **Microsoft Azure CLI** enterprise application and the directory
permissions or roles assigned to your user.

### Fabric role assignments return HTTP 403

In tenant-wide mode, confirm the user has the Fabric administrator role and
is authorized to use tenant admin APIs. In `--accessible-only` mode, the user
can see the workspace but isn't a Member or Admin, so the workspace is recorded
in `workspace_inspection_errors.csv`.

### A location column is empty

The relevant attribute may not be populated on the user's Microsoft Entra
profile. Check the user in the Microsoft Entra admin center.

## Security

- Access tokens are held only in memory and aren't written to CSV files.
- The script doesn't request or store passwords or client secrets.
- CSV files can contain personal and organizational information. Store and
  share them according to your organization's privacy and data-handling rules.
- Interactive Azure CLI authentication is suitable for local, attended
  execution. It isn't appropriate for unattended scheduling.
