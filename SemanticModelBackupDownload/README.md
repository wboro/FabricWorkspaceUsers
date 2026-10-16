# Semantic model download to private ADLS Gen2

Deploy **`SemanticModelBackupDownload.ipynb`** to Fabric as a **PySpark notebook**,
edit its configuration cell, then run it. The notebook embeds the complete Python
implementation; no Lakehouse or additional source upload is required.
`SemanticModelBackup` is unchanged.

This alternative uses **Power BI PBIX export with `downloadType=IncludeModel`**
to download a report together with its semantic model's **embedded imported data**
to notebook driver-local disk, then uploads it to the designated ADLS Gen2 account.
It does **not** use native ABF/XMLA backup, link ADLS to a workspace, create
temporary reports, or change network/access settings.

## Important: not every semantic model can be downloaded

There is no documented public REST API that exports an arbitrary semantic model
with all its data, regardless of configuration. The documented data-inclusive
download API operates on a **report**. This notebook inventories **every semantic
model in each configured workspace**, selects associated Power BI reports in that
same workspace by `datasetId`, and attempts data-inclusive exports. If one report
cannot export, it tries another associated report before any archive write.
It never silently falls back to a live connection or metadata-only artifact.

Microsoft's [PBIX download limitations](https://learn.microsoft.com/power-bi/create-reports/service-export-to-pbix#limitations)
include incremental-refresh models, Direct Lake, automatic aggregations,
some XMLA-modified/deployment scenarios, size limits, and **large semantic model
storage format via REST**. Other restrictions include models migrated from AAS
and certain OneDrive/Excel/CSV sources. The service enforces eligibility; this
notebook does not alter models to make them downloadable.

A model without a suitable report is recorded as **failed**, not omitted. The
semantic-model UI's "Download this file" option does not imply a supported
dataset-only REST endpoint. Paginated reports are not candidates. Models that
only have reports in other workspaces are likewise reported as failures here.

**DirectQuery/Direct Lake/external-source data is not materialized.** Even a PBIX
with an embedded model does not snapshot those external sources. This tool
provides verified PBIX archives for eligible models, not a universal or
transactionally consistent workspace disaster-recovery mechanism. If your scope
includes restricted models, this alone cannot meet full-data backup requirements;
retain source-data backups and model definitions or use another approved recovery
strategy. DAX/CSV extraction is not an equivalent lossless model backup and is not
used as a fallback.

## Private networking and deployment

1. Keep the archive storage account's **public network access disabled**.
   Configure approved connectivity **from the Fabric notebook Spark runtime**
   to the storage account's **`blob` private endpoint**. Fabric managed private
   endpoints are one supported networking mechanism where available for your
   capacity, region and tenant configuration. Create/approve the endpoint before
   starting the notebook session. See
   [Create and use managed private endpoints](https://learn.microsoft.com/fabric/security/security-managed-private-endpoints-create).
2. Use `https://account.blob.core.windows.net` in configuration. Private DNS must
   route this ordinary hostname to the private endpoint. Do not substitute a
   `privatelink` hostname. This implementation uses the Blob SDK against the
   HNS-enabled account; a **DFS-only private endpoint is not sufficient**.
   Provision additional `dfs` connectivity if required by other clients/workloads.
   No Power BI service-to-archive connection is needed.
3. The runtime still needs approved outbound access to Entra authentication,
   Fabric/Power BI APIs (including regional `wabi-*.analysis.windows.net` routing
   endpoints), dependency package sources (or use a preconfigured Fabric
   Environment), and potentially **Power BI-managed temporary export storage**.
   With `preferClientRouting=true`, Power BI can return HTTP 307 to its regional
   cluster; the notebook authenticates only allowlisted HTTPS Power BI endpoints
   for the **same report export path** and `downloadType=IncludeModel`.
   Separately, Power BI may redirect a download to a signed Azure Blob URL.
   Such a URL is used only for downloading, without forwarding the Power BI bearer token.
   This does not enable public networking on **your** archive account.
   If policy blocks service-managed export storage too, affected exports fail;
   this notebook does not bypass that restriction.
4. Enable the application for the relevant Fabric and Power BI APIs in tenant
   settings, and grant it **Workspace Admin** on source workspaces (a sufficient
   role; no XMLA read/write or workspace-linked storage is required).
   Ensure tenant policies permit report downloads. The documented delegated scopes
   for report export are `Report.ReadWrite.All` or
   `Report.Read.All` plus `Dataset.Read.All`; service-principal authorization uses
   tenant settings and workspace access, not delegated user consent.
5. Create an ADLS Gen2 filesystem/container and grant the configured application
   **Storage Blob Data Contributor** there. Ensure any applicable HNS ACLs allow
   traversal/read/write. No Shared Key or SAS credentials are needed for archive
   access. This notebook creates neither the account nor container.
6. Grant the **notebook execution identity** read access to configured Key Vault
   secrets and approved network access if that vault is private. Model inventory,
   downloads and archive access use the explicit service principal instead.
   Literal credentials are supported for isolated testing, but never commit them.
7. Provide enough **driver-local temporary disk for the largest PBIX**, plus
   headroom. Set `temp_directory` only to an approved runtime-local location.
   Do not put temporary downloads in Lakehouse Files unless a persistent extra
   copy is intentional. Local files are removed on success and ordinary failures;
   runtime termination can leave temporary files until the runtime is disposed.

Networking setup is external to the notebook. The initial container-properties
request tests archive connectivity/authorization before model work; it does not
prove that DNS used a private route. Administrators must verify endpoint approval,
DNS routing and disabled public network access independently. No cloud connection
or tenant-specific network configuration has been tested by the repository tests.

Only Azure public-cloud API and storage hostnames are implemented.

## Configuration

The notebook's configuration is mirrored in `config.example.py`:

| Setting | Purpose |
|---|---|
| `service_principal` | Tenant/client GUIDs and secret; each accepts a literal or Key Vault reference |
| `archive.account_url` | ADLS account's ordinary HTTPS **Blob** endpoint |
| `archive.container` | Existing filesystem/container |
| `archive.prefix` | Relative object-key prefix; `/` separates Blob keys, not local filesystem paths |
| `workspaces` | Nonempty list of distinct source workspace GUIDs |
| `temp_directory` | `None` for runtime temp storage, or an approved local directory |
| `max_download_bytes` | Per-download disk limit, default 50 GiB; service/capacity limits can be lower |

Fabric and Power BI inventory use separate token audiences and enumerate all
returned pages. Models and workspaces are processed sequentially. Discovery and
initial export requests retry throttling/transient HTTP server responses up to six
attempts with bounded backoff, including regional routing and signed Blob download
requests. Export follows at most five redirects, rejects repeated URLs, refuses
changes to the report or download mode on Power BI routing hops, and never
reauthenticates a redirect from signed Blob storage back to an API.
A broken download stream is not resumed; a different
candidate report may be tried. Azure SDK storage retries are bounded. An uncertain
upload/read-back failure does not trigger a new export to the same immutable key.
Failures never count as successful backups.

## Output and recovery

```text
<prefix>/runs/<run-id>/started.json
<prefix>/runs/<run-id>/workspaces/<workspace-id>/<model-id>.pbix
<prefix>/runs/<run-id>/manifest.json
```

Each PBIX must contain a nonempty `DataModel` entry and pass ZIP CRC/integrity
checks. Encrypted/non-ZIP files that cannot be inspected fail closed. The embedded
model's presence is a structural check, not proof that every table is Import mode.
Uploads do not overwrite existing objects and use bounded-memory, MD5-validated
blocks. The uploaded object is fully read back against its committed ETag and its
length/SHA-256 must match the local download before success is recorded.
Expect one local disk write, one archive upload, and one archive verification read
per successful model, plus ZIP integrity reads.

The final manifest contains per-workspace inventory state, per-model success or
failure, report attempts, archive paths, hashes, lengths, ETags and summary counts.
**Any workspace/model failure raises an error after the manifest is saved**,
failing notebook/pipeline execution. Both successful and incomplete runs print one
readable report with a run ID, overall status, model/workspace counts, and a
separate results table for each workspace. Each model row shows **BACKED UP** or
**FAILED**, its name, a human-readable archive size and the result or failure
reason. Long names and errors wrap instead of being truncated. Common coverage
failures have short descriptions; full messages remain in the manifest.
Recovered report attempts are called out on successful rows too.

The full manifest URL appears below the tables. The final exception is a short
count summary pointing to that report and manifest, not a repeated JSON dump.
The notebook does not print the manifest/configuration again on success or enable
global INFO logging; Azure SDK logs are limited to warnings and errors.
Per-attempt diagnostic logs are available at DEBUG level for this script's logger,
but normal output is consolidated into the final report.

A missing associated report and a PBIX without an embedded `DataModel` remain
explicit failures, even when other models back up successfully.
An inventory failure is not an empty successful
workspace. An initial archive failure stops before downloads; a final manifest
write failure also fails execution. A `started.json` without `manifest.json` is
an incomplete run. Abruptly terminated runs are not resumed automatically.

If a run reports **no associated Power BI report**, the documented report export
API has nothing to export for that model; the notebook does not mutate the
workspace to create one. If it reports **no embedded DataModel**, a successfully
downloaded report is not a data backup (for example, a Direct Lake/live-connected
report). Adding network exceptions or suppressing the final error cannot turn
either case into a full-data backup.

No secrets, configuration payloads, API response bodies or signed URLs are written
to manifests. Unexpected SDK exception text is suppressed because it can contain
credentials; the exception class and affected workspace/model are recorded.
Do not enable HTTP/SDK debug logging in production.

For recovery, download the chosen successful PBIX from ADLS, check its SHA-256
against the manifest, open it in a current Power BI Desktop, and validate imported
tables/measures before republishing to an explicitly selected workspace. This is
**not** ABF restore. Reconfigure data-source credentials, gateway bindings, refresh
schedules, workspace access and service-side role assignments separately. Other
reports/dashboards/apps are not backed up; the PBIX includes the selected report.
Use encryption, least-privilege access, retention and recovery drills appropriate
for sensitive model data.

## Development

```powershell
python -m pip install -r .\SemanticModelBackupDownload\requirements.txt
python .\SemanticModelBackupDownload\build_notebook.py
python -m unittest discover -s .\SemanticModelBackupDownload\tests -v
```

The generated notebook must be rebuilt after changing source/configuration.
Tests use offline HTTP and Azure SDK transports, not a live tenant. Production
validation still requires a pilot on eligible and restricted models using the
approved Fabric networking configuration.

API reference:
[Export Report In Group](https://learn.microsoft.com/rest/api/power-bi/reports/export-report-in-group).
[Client routing and HTTP 307](https://learn.microsoft.com/power-bi/developer/embedded/troubleshoot-rest-api#fix-timeout-exceptions-when-using-import-and-export-apis).
