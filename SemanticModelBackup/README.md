# Fabric semantic model backup and restore

If workspace-linked ADLS/native backup is not allowed, see the separate
**`SemanticModelBackupDownload`** directory for a Fabric notebook that downloads
eligible models as data-inclusive PBIX files and uploads them to private ADLS Gen2.
PBIX export has coverage restrictions; that alternative is not a native-backup
replacement for every model type.

Import **`SemanticModelBackup.ipynb`** into Microsoft Fabric as a **PySpark notebook**.
It is self-contained; no Lakehouse attachment or additional source-file upload is required.
Edit the configuration cell and run the notebook.

This implementation creates **native `.abf` backups**, not just model-definition
exports. These preserve semantic model metadata and imported data supported by the
native service. It contains **no capacity reassignment or migration code**.

## Required setup

1. Source and target workspaces must be on a capacity supporting XMLA backup and
   restore. Enable **XMLA read/write** on that capacity. Microsoft documents native
   restore as supporting enhanced-metadata (V3), large-format models. Service
   principals cannot use PPU workspaces; use a supported Premium/Fabric capacity.
2. Enable service principals for the relevant Power BI/Fabric APIs in tenant
   settings, including XMLA access. Add the configured principal as **Workspace
   Admin** on the selected workspaces. This deliberately uses one documented
   sufficient permission set for both backing up and creating restored models.
3. Configure **Power BI's ADLS Gen2 integration** on every source and target
   workspace. The service creates `power-bi-backup/<workspace name>/` in that
   linked storage account. By default this is a prerequisite. The optional
   temporary-attachment feature described below can attach a previously registered
   account for backup and disconnect afterward. Merely creating a folder or
   granting Blob access does not link a workspace.
4. Create an archive Blob container. Grant the configured service principal
   **Storage Blob Data Contributor** on the archive and native staging containers
   (including the target staging container when restoring to a different account).
   ADLS hierarchical namespace ACLs, if applicable, must permit traversal and file
   operations too. The notebook does not create containers or change permissions.
5. Allow the Fabric notebook to access archive and staging storage. Separately,
   Power BI itself must access its linked ADLS storage. Microsoft currently
   documents that native backup/restore storage cannot use a VNet/firewall and
   can fail when Shared Key authorization is disabled. Validate these constraints
   against your organization's policies **before** configuring storage; the notebook
   does not weaken network or authorization settings.
6. For Key Vault references, grant the **notebook execution identity** secret read
   access (for example, Key Vault Secrets User). This bootstrap identity retrieves
   the application credentials through `notebookutils.credentials.getSecret`.
   After bootstrap, model discovery, XMLA and Blob operations use the explicitly
   configured application. Optional ADLS connection management instead uses the
   notebook execution user's Power BI token, as storage-management APIs can reject
   service-principal authentication. Scheduled execution identities can differ
   from the interactive notebook user.

The configured `account_url` values use public Azure Blob endpoints
(`https://account.blob.core.windows.net`). Sovereign-cloud endpoints are not
implemented. The archive may be a different account and need not be ADLS Gen2.
**An arbitrary Blob container alone is insufficient for native backup/restore**:
the intermediate workspace-linked ADLS Gen2 account is mandatory.

## Configuration

The notebook configuration is also provided in `config.example.py`.

| Setting | Purpose |
|---|---|
| `mode` | `backup` or `restore` |
| `service_principal` | Tenant ID, application ID and client secret |
| `archive` | Archive account URL, existing container and relative prefix |
| `staging_account_url` | Default staging **Blob** endpoint for unmanaged backup/restore; temporary backup defaults to the control workspace's account |
| `backup.workspaces` | Source workspace GUIDs; every discovered semantic model is attempted |
| `backup.password` | Optional ABF encryption password or Key Vault reference |
| `backup.temporary_adls.enabled` | Opt-in backup-only temporary attachment; defaults to `False` |
| `backup.temporary_adls.dataflow_storage_id` | Optional registration pin; `None` follows the runtime control workspace |
| `backup.temporary_adls.timeout_seconds` | Poll timeout for each attachment, native-folder provisioning, or disconnection step; default 300, range 1-1800 |
| `backup.workspaces[].dataflow_storage_id` | Optional registration pin for an unlinked source; must match the control workspace |
| `restore.run_id` | Exact backup run ID from notebook output or Blob storage |
| `restore.workspaces` | Source workspace GUID to target workspace GUID mappings |
| `restore.workspaces[].model_ids` | `None` for all models, or a nonempty list of source model GUIDs |
| `restore.workspaces[].model_names` | Optional source model GUID to new display name mappings |
| `restore.dry_run` | Defaults to `True`; no writes or XMLA commands |
| `restore.allow_overwrite` | Defaults to `False`; an explicit `True` permits native replacement |
| `restore.password` | Original ABF encryption password, if used |

Each workspace entry can override `staging_account_url` and `staging_folder`.
For backup, these identify the **source workspace's linked storage**; for restore,
the **target workspace's linked storage**. The default folder is the current
workspace display name returned by Fabric. Set an override if the service's actual
folder differs, and revisit it after a workspace rename. Blob paths use `/`
because they are object keys, not local filesystem paths.

All three service-principal values and either ABF password accept a literal
string or a Key Vault reference:

```python
{"key_vault_url": "https://my-vault.vault.azure.net/", "secret_name": "my-secret"}
```

Literal credentials are supported for isolated testing, but **do not commit or
share a notebook containing real credentials**. Prefer Key Vault. References
retrieve the current secret version; retain the original encryption password if
you rotate it, and point restore at a separate retained secret when necessary.
Configuration and passwords are not persisted in manifests or restore reports.

## Backup

Set `mode` to `backup`, configure the principal, storage and workspace IDs, then
run all cells. The notebook:

1. Writes an immutable start record with a unique UTC/UUID run ID.
2. Lists **all pages** of semantic model items in each configured workspace.
3. Runs a native XMLA backup addressed by model GUID into that workspace's linked
   ADLS folder. Unique flat filenames avoid model-name and directory collisions.
4. Streams the ABF into archive Blob storage without buffering the whole model in
   memory or downloading it to notebook disk.
5. Re-reads the uploaded archive, verifies SHA-256 and length, and records its ETag.
6. Writes a final manifest, including per-workspace/model outcomes. Any failure
   raises an error so a scheduled notebook/pipeline run fails visibly.

Every completed run prints statistics: semantic models **backed up**, **failed**
and **attempted**, plus workspace success/failure counts, workspaces not attempted,
workspaces without a complete model inventory, and cleanup/recovery failures.
These counters are also saved in the manifest's `summary` object and returned
with successful runs. A model counts as backed up only after its archive copy
passes verification. Discovery failures leave the number of models in that
workspace unknown; they are not counted as zero-model successes. Cleanup-only
failures do not subtract successfully archived models. Partial runs still raise
an error for pipeline monitoring, but the error now contains the actual counts
and manifest location instead of a generic failure message. Control-preflight
failures include zero attempted models, unattempted workspace counts and the
specific setup error.

Models are processed sequentially to avoid creating parallel capacity pressure.
API discovery retries throttling/transient server errors with bounded backoff.
Blob SDK operations use bounded retries. **XMLA backup/restore commands are never
automatically retried**, because a disconnected operation might have completed
server-side. Inspect native staging and the service before retrying an uncertain
operation.

ABFs upload as explicit, MD5-validated byte blocks of at most 8 MiB, avoiding SDK
content-validation errors on nonseekable iterators. Only the current block is
buffered; an incomplete or incorrectly sized source is never committed. The final
block-list commit requires the destination not to exist, preserving immutable
archives. A full read-back SHA-256/length verification still determines success.
The same transfer path is used when staging an archive for restore. The current
8 MiB block size supports at most 50,000 blocks per ABF (about 390.6 GiB).

```text
<prefix>/runs/<run-id>/started.json
<prefix>/runs/<run-id>/workspaces/<workspace-id>/<model-id>.abf
<prefix>/runs/<run-id>/manifest.json
<prefix>/restores/<restore-run-id>/report.json
```

An empty workspace is recorded as a successful workspace with no models. Models
that the service cannot back up (including any unsupported system/default model)
are **not silently skipped**: they fail their workspace and the run. A failure in
one workspace does not stop attempts on the other configured workspaces.

### Optional temporary ADLS attachment

```python
CONFIG["backup"]["temporary_adls"] = {
    "enabled": True,
    "dataflow_storage_id": None,
    "timeout_seconds": 300,
}
```

**One-time workspace-only bootstrap:** connect ADLS in the workspace containing
this notebook and leave that connection in place. Do not configure tenant-level
storage. The notebook reads `notebookutils.runtime.context["currentWorkspaceId"]`
at runtime to identify this persistent control workspace; no Dev ID is hardcoded.
After moving the notebook to production, connect ADLS once in its new workspace.
Existing source workspace IDs, archive settings and application credentials
remain explicit configuration and must be appropriate for the target environment.

The control workspace's connection must point to an accessible, enabled Power BI
storage registration. A missing/disabled control connection fails the whole run
before changing any source workspace. Its state and ID are recorded under
`control_workspace` in the manifest, including control-preflight failures.
The notebook never creates or repairs that initial registration through an
undocumented API.

With `dataflow_storage_id=None`, temporary connections follow the control
workspace's registration and staging account, not the old global
`staging_account_url`. An explicit source staging URL or registration pin must
match the control account. Existing source connections to other accounts remain
unchanged and use their own discovered staging account. The archive account
remains independent.

Only the supported Power BI `AssignToDataflowStorage` workspace API is used for
attachment/disconnection. No tenant defaults/settings or unselected workspace
connections are changed. If the control workspace is also in the backup list,
its models may be backed up, but its connection is never detached. If its link
changes externally, the notebook refuses to recreate it as a temporary source
connection. The control link is rechecked before each new source attachment.

The notebook execution **user** must be Workspace Admin on every selected workspace
and have Power BI storage-management API access (`StorageAccount.ReadWrite.All`
and `Workspace.ReadWrite.All` for assignment). Management calls use
`notebookutils.credentials.getToken("pbi")`; the configured service principal still
does all model discovery, XMLA backup, Blob transfers and journal writes. This
does not elevate the principal or silently fall back between identities. A
service-principal-run schedule or a restricted notebook-user token may return
401/403; use an authorized user-run notebook or configure connections manually.

For each workspace:

1. Read its existing Power BI storage assignment. If already linked, preserve it
   and derive the actual staging account from the accessible storage registration.
   This overrides a different global/per-workspace staging URL without relinking.
2. If unlinked, require no Gen1 dataflows, an enabled matching registration, and
   the native workspace-name folder (no custom `staging_folder` for a new link).
   Refuse attachment if an old native workspace folder already exists: Microsoft
   documents reattachment conflicts. Review and move/rename that retained staging
   folder manually first; the notebook never deletes or renames backup files.
3. Write `connection-before.json` to the archive **before** requesting attachment,
   then wait for the assignment and service-created backup folder.
4. Execute the normal backups and archive verification.
5. In `finally`, restore the original disconnected state. Existing links are
   never detached, regardless of whether they match the configured temporary ID.
   Record the outcome in `connection-after.json` and the final manifest.

Registration discovery is cached once per run instead of repeated for every
source. Selected workspaces/models still run sequentially, with bounded GET
backoff and no automatic POST/XMLA retries. This change removes per-source portal
connection setup, but **does not establish production readiness for 13,000
workspaces**: perform a small attach/backup/detach pilot first. In particular,
verify the registration remains enabled while the control connection persists.
The controller has not been load-tested at that scale and has no automatic
cross-session work-queue resume or folder-rotation mechanism. Source permissions,
Gen1 dataflow restrictions, retained-folder conflicts and capacity limits remain
explicit prerequisites/failures, not silently skipped items.

**This is not a transaction or an automatic rollback guarantee.** Do not run
concurrent notebooks or change ADLS settings manually on the same workspace while
it is being processed: the Power BI API has no conditional assignment operation.
Cleanup refuses to overwrite a connection that changed externally, delete Gen1
dataflows created during the run, or detach storage after an uncertain native XMLA
backup outcome. Such cases fail the run visibly and require manual recovery. A
failed/timeout assignment that has not been observed also remains explicitly
unconfirmed rather than being reported as successfully rolled back.

`finally` cannot run after a killed driver/session. Recover using
`runs/<run-id>/workspaces/<workspace-id>/connection-before.json` and
`connection-after.json` (if present). Confirm any XMLA operation has stopped, check
the current assignment and Gen1 dataflows, then use Workspace settings to disconnect
only a link this run created. The API equivalent uses the empty GUID
`00000000-0000-0000-0000-000000000000`. Do not detach a pre-existing connection.
If there are Gen1 dataflows, resolve their dependency manually; this notebook never
deletes them.

Disconnecting retains staging files, Azure role assignments and account settings.
It does not revert HNS, network rules, Shared Key settings, or policy exemptions.
Before reconnecting later, review existing backup-folder conflicts documented by
Microsoft; no folders are renamed or deleted automatically.

`backup_status` records whether all models were archived successfully separately
from the workspace's overall status. A cleanup/journal failure fails the run but
does not invalidate already verified ABFs; those complete workspace backups remain
eligible for restore. Actual model backup failures still prevent restoring that
workspace through this notebook. The option does not manage restore-target
connections; restore and its dry run retain their existing behavior.

The final manifest is the restore index. A run with only `started.json` was
interrupted and is **not restorable by this notebook**. If the final manifest
upload fails, the notebook fails; recover the individual ABFs using native tools.
Snapshots are per model, **not an atomic cross-workspace point-in-time snapshot**.

## Restore

Set `mode` to `restore`, supply `restore.run_id`, and configure source/target
workspace mappings. Targets must already exist and have their own working native
ADLS integration. A source workspace need not still exist: restore reads source
identity and model metadata from the archive manifest.

Keep `dry_run=True` initially. The read-only plan checks the complete selection,
target capacity, model-name conflicts, staging/archive container access and archive
size/ETag before returning the plan. It does **not** run XMLA, transfer data, or
write a report. It does not prove an ABF password is correct or that native
restore will support the model.

Set `dry_run=False` to execute the plan. Before each XMLA restore the notebook
streams the ABF into the target workspace's native folder, checks its manifest
SHA-256 and re-reads the staged file to verify the copy. A corrupted or replaced
archive cannot reach XMLA restore. Existing model names are rejected unless
`allow_overwrite=True`; the native command also receives that flag, guarding
against a new conflicting model created after preflight. Duplicate or ambiguous
target names are rejected.

Each restore re-creates the model under its backed-up name unless renamed
explicitly. New models receive **new service IDs**. Restoring a copy does not
rebind reports or modify dependencies. An overwrite is destructive: take a fresh
backup of the target before explicitly enabling it. Do not run concurrent writes
against the same target during restore.

A failed backup run can be used to restore a **fully backed-up workspace**
from that run, including one whose only failure was ADLS cleanup/journaling.
A workspace with any model backup failure is rejected, even when only
some of its models were selected, to avoid treating incomplete workspace coverage
as a complete safety copy. Restore failures are isolated per model and recorded
in a unique restore report, and the notebook fails overall.

## What is and is not protected

- Native ABF captures the model and imported/processed data supported by the
  service. DirectQuery and Direct Lake still reference external data; an ABF does
  **not** back up those external databases, OneLake files or upstream items.
- Reports, dashboards, workspace permissions, apps, refresh schedules, sensitivity
  labels and service-level operational settings are **not separately backed up
  or restored** by this notebook. ABF is not a complete workspace backup.
- Restore uses native `skipMembership`: model role definitions are retained
  without copying source role memberships. Configure/verify any required role
  membership in the Power BI portal.
- Connections, gateway mappings, credentials and external item dependencies must
  be checked/configured separately before using or refreshing the restored model.
  No automatic refresh is triggered and no data source is rewritten.
- Platform restrictions, unsupported model/storage formats, capacity resource
  limits and encrypted/specialized models can cause native commands to fail.
  Rehearse a restore in a separate workspace before relying on these backups.
- SHA-256/ETag checks detect corruption or changed content, not malicious rewriting
  of both the ABF and manifest. Restrict archive writers and consider Blob
  versioning/immutability, encryption, soft delete and a documented retention policy.
- Native and restore staging files are deliberately retained, including files
  left by a failed verification or indeterminate operation. Nothing is
  automatically deleted. Plan lifecycle/cleanup policies in **both** native and
  archive storage; account for ABF copies and verification read/egress costs.

## Development

`semantic_model_backup.py` is the maintained implementation.
`build_notebook.py` embeds it and the configuration example into the deliverable.
Regenerate the notebook after changing either source:

```powershell
python build_notebook.py
python -m unittest discover -s tests -v
```

The offline tests use in-memory storage and mocked Fabric/XMLA adapters. They do
not connect to a tenant. Fabric runtime dependencies are declared in
`requirements.txt` and installed by the notebook's first code cell; for scheduled
production use, attach a Fabric Environment with reviewed/pinned versions and
remove the interactive `%pip` cell if required by your environment.

## Microsoft references

- [Native semantic model backup/restore and limitations](https://learn.microsoft.com/en-us/fabric/enterprise/powerbi/service-premium-backup-restore-dataset)
- [Assign/unassign workspace ADLS storage](https://learn.microsoft.com/en-us/rest/api/power-bi/dataflow-storage-accounts/groups-assign-to-dataflow-storage)
- [List enabled Power BI storage registrations](https://learn.microsoft.com/en-us/rest/api/power-bi/dataflow-storage-accounts/get-dataflow-storage-accounts)
- [Service principals and XMLA](https://learn.microsoft.com/en-us/power-bi/enterprise/service-premium-service-principal)
- [SemPy XMLA/TMSL APIs and explicit credentials](https://learn.microsoft.com/en-us/python/api/semantic-link-sempy/sempy.fabric?view=semantic-link-python)
- [NotebookUtils tokens and Key Vault bootstrap](https://learn.microsoft.com/en-us/fabric/data-engineering/notebookutils/notebookutils-credentials)
- [XMLA Backup](https://learn.microsoft.com/en-us/analysis-services/xmla/xml-elements-commands/backup-element-xmla)
- [TMSL Restore](https://learn.microsoft.com/en-us/analysis-services/tmsl/restore-command-tmsl)
