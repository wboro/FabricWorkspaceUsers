"""Build the self-contained Fabric notebook from the editable Python implementation."""

from pathlib import Path
import nbformat as nbf


ROOT = Path(__file__).resolve().parents[1]


def build():
    notebook = nbf.v4.new_notebook()
    notebook.metadata = {
        "kernelspec": {"display_name": "Synapse PySpark", "language": "python", "name": "synapse_pyspark"},
        "language_info": {"name": "python"},
        "microsoft": {"language": "python", "language_group": "synapse_pyspark"},
        "kernel_info": {"name": "synapse_pyspark"},
    }
    cells = [
        nbf.v4.new_markdown_cell("""# Workspace capacity migration

Safely plan P-to-F and F-to-F workspace reassignments, including eligible cross-region moves.
Configuration-first and self-contained, like **SemanticModelBackup**. No artifact is deleted,
recreated, converted, or routed through shared capacity. **Dry-run is the default.**

**Strict safety profile:** Only workspaces containing reports, paginated reports, dashboards and
Import semantic models are supported. Fabric workloads, dataflows, datamarts, scorecards,
Direct Lake/DirectQuery/live/push models and unknown item/configuration types are blocked,
even in the same region when this notebook cannot certify their data and health.
Cross-region moves additionally block large-storage models. A smaller-than-equivalent target is blocked.

**Prerequisites:** Use a compatible Fabric runtime/Environment with `requests`, `azure-core`,
`azure-storage-blob` and `semantic-link-sempy` exposing `connect_semantic_model` and `evaluate_dax`.
Libraries are checked before migration and are not automatically upgraded during a run.
Run as a workspace Admin with destination capacity Contributor/Admin rights,
read/write model permission and XMLA read access; the execution user needs Blob read access to the
SemanticModelBackup archive and Blob create access for the external journal. Use a separate control
workspace. Keep the source capacity active until final sign-off.

**Safety is not a zero-risk guarantee:** Service failures and partial item placement remain possible.
Unknowns block the move; recent ABFs are streamed and SHA-256 verified before any reassignment.
Provide a reviewed change reference, freeze writes/schedules/jobs externally, and approve networking,
CMK, capacity settings/headroom, recovery and dependencies. Approvals do not bypass technical blockers.
No automatic rollback or mutation retry is performed.

**Validation:** Completed capacity assignment and region, exact item IDs/folders, report bindings,
workspace roles, XMLA schemas, every table's row count, connections and refresh schedules.
Optional refreshes are serial and transactional, preserve existing incremental policy partitions,
and are monitored by their exact request IDs. Refreshes can legitimately change source data.
The final state is deliberately **pending manual placement check**: public item metadata does not
prove every item left the old capacity. Inspect workspace reassignment warnings, open reports as a
consumer, and test gateways/RLS before retiring anything. See `README.md` for the runbook.
"""),
        nbf.v4.new_code_cell('''import requests
from azure.core.credentials import AccessToken

print("Using Fabric runtime libraries; automatic session-wide package upgrades are disabled.")
# If migration prerequisites are missing, provision a compatible Fabric Environment first.
# Optional interactive setup (restart the session afterward; do not run during migration):
# %pip install "azure-storage-blob>=12.23,<13" "requests>=2.32,<3" "semantic-link-sempy>=0.14.2,<0.15"
'''),
        nbf.v4.new_markdown_cell("""## Configuration

Edit only the configuration code cell below. Empty `workspaces` lists accessible workspaces/capacities without migrating.
No credentials are copied from the backup notebook: authentication uses the executing notebook user.
Replace the commented example with explicit GUID mappings and reviewed approvals.
Configure the existing archive/container from SemanticModelBackup; do not create or alter storage here.
Live runs require an external journal, kept outside all migrating workspaces.
`CONFIG` is a structured dictionary, not a scalar pipeline-parameters cell; scheduled runs use
the saved configuration.

### Parameter reference: run controls

Defaults below are the values shipped in the configuration cell, not values inferred by the service.
Use Python `True`, `False` and `None`, not strings such as `"false"` or `"None"`.
GUIDs are IDs, not display names. Keep all keys in the configuration when changing modes.

| Parameter | Default / accepted values | Explanation |
| --- | --- | --- |
| `mode` | `"migrate"`; `"migrate"` or `"validate"` | `migrate` builds a plan and, only when `dry_run=False`, reassigns eligible workspaces. `validate` loads a previous migration journal and compares the current target workspace against its original baseline; it never submits assignments or refreshes. It does not complete a failed move or resume refreshes. |
| `dry_run` | `True`; boolean | In migrate mode, performs inventory, safety approvals, backup verification and planning without assignment or refresh POSTs. Set `False` only for an approved live migration. Dry-run still makes read requests, can stream entire ABF backups, and writes journals if an archive is configured. It is not an offline check. Validate mode always performs read-only service validation, regardless of this flag, and can also write journals. |
| `expected_tenant_id` | `"2ee4898e-0173-44a4-92bf-fca5f81a39ee"`; nonempty tenant GUID | Must match the executing user's Power BI token tenant. This is a wrong-tenant guard, not a login setting, username, password or service-principal credential. Change it only when deliberately using the notebook in another tenant. |
| `refresh_semantic_models` | `False`; boolean | When `True`, requests the selected models' refreshes only after a live move passes mandatory validation. No refresh is executed in dry-run/discovery or validate mode, although configured selections are still checked. Refreshes are sequential, transactional and monitored by exact request ID. They refresh existing partitions without applying the incremental retention policy, but can still replace model values with changed upstream data. |
| `poll_seconds` | `15`; 10 through 300 seconds | Interval between assignment/refresh status checks. This controls observation frequency, not API GET retry backoff or service execution speed. |
| `assignment_timeout_seconds` | `3600` (1 hour); positive number of seconds | Polling window for each assignment wait, including assignment-completion checks during validation. Expiration does not cancel the service operation or undo the move. Treat the result as uncertain, retain both capacities and investigate before resubmission. HTTP requests/retry delays can extend elapsed wall-clock time. |
| `refresh_timeout_seconds` | `7200` (2 hours); positive number of seconds | Polling window for each submitted semantic-model refresh. This is not the refresh engine's timeout. Expiration stops this notebook's monitoring, not the refresh; use its saved refresh ID to monitor the existing request rather than start another. |
| `backup_max_age_hours` | `24`; positive number of hours | Maximum age of the selected backup run's `completed_at` timestamp at verification time. Stale or future-dated runs are blocked. Independently, model/schema/partition changes must predate that backup run's start; increasing this setting cannot permit models changed after backup. |
| `archive` | `None`; otherwise the dictionary described below | Existing external Blob archive used to read/verify SemanticModelBackup ABFs and create migration journals. `None` is sufficient for discovery; model-bearing workspace preflight, all live migrations and journal-based validation require archive access. Do not place the archive inside a migrating workspace. |
| `validation_journal_blob` | `""`; exact relative Blob path | Used only by `mode="validate"`. Select a specific migration JSON journal containing the original complete `inventory`, `models` and target-capacity baseline, for example `workspace-migrations/<migration-run-id>/00004.json`. Prefer an assignment-submitted record. This is not a URL, backup manifest, folder or automatically selected latest file. |
| `workspaces` | `[]`; list of mapping dictionaries described below | In migrate/dry-run mode, an empty list discovers accessible workspaces and capacities only. A live migrate run requires explicit mappings; each workspace can appear only once. Any preflight blocker prevents the whole batch from starting; a later execution failure stops the remaining batch without rolling back earlier moves. In validate mode, migration targets/baselines come from the journal, not this list; leave it empty. |

### Parameter reference: `archive`

Replace `archive=None` with the full dictionary from the example when needed.
The account and container must already exist. The executing user needs Blob read access to
the backups and permission to create journal blobs; this notebook grants no permissions and
does not create containers. Journal JSON contains protected workspace/connection metadata.

| Parameter | Example / required value | Explanation |
| --- | --- | --- |
| `archive.account_url` | `"https://<account>.blob.core.windows.net"` | Public Azure Blob service root for the existing backup archive. Must be HTTPS with a valid storage-account name; do not include a container, path, trailing slash, SAS token or credentials. Authentication uses the executing notebook user's storage token. |
| `archive.container` | `"backup"` | Name of the existing container holding the selected backup manifests/ABFs and migration journals. Use a valid lowercase Azure container name, not its URL. This is independent of the source workspace's linked ADLS staging storage used by SemanticModelBackup. |
| `archive.journal_prefix` | `"workspace-migrations"` | Required when the archive is configured. Relative folder-like prefix for new immutable journals: `<prefix>/<migration-run-id>/<sequence>.json`. Use a nonempty path with no backslashes, empty interior segments, `.` or `..` segments. The prefix is for output journals; it does not select a backup run. |

### Parameter reference: each `workspaces` entry

Uncomment a complete mapping and replace placeholders with real IDs/paths. Use discovery output
to find workspace/capacity IDs. All selected workspaces are checked before the first move.

| Parameter | Default / required value | Explanation |
| --- | --- | --- |
| `workspace_id` | Required source-workspace GUID | The existing workspace to reassign in place. It must be a supported collaborative workspace, and the executing user must be its Admin. The control workspace running this notebook is blocked. Do not provide the workspace name or a new destination-workspace ID. |
| `target_capacity_id` | Required destination-capacity GUID | Existing active paid Fabric F capacity to receive the workspace. The notebook discovers the current source capacity and both regions automatically. P1/P2/P3/P4/P5 require at least F64/F128/F256/F512/F1024 respectively; smaller targets, unknown SKUs and assignment to the existing source capacity are blocked. For F-to-F moves the target must not be smaller than the source. |
| `backup_manifest_blob` | Required for a workspace with semantic models; exact relative path | Explicit successful SemanticModelBackup run manifest within `archive.container`, for example `semantic-models/runs/<backup-run-id>/manifest.json`. Not a migration journal or URL. Its model IDs/names must exactly match the current workspace. Each referenced native ABF must pass size, ETag and full SHA-256 read-back checks. Unnecessary for an eligible workspace with no semantic models. |
| `refresh_model_ids` | Omitted or `None` = all workspace models; `[]` = none; otherwise a unique list of model GUID strings | Per-workspace selection used only when `refresh_semantic_models=True` during a live migration. Selected IDs must belong to that workspace. This controls optional refreshes only: every model is still subject to backup checks and mandatory pre/post-move validation. Use canonical lowercase GUIDs when explicitly listing IDs. |
| `approvals` | Required dictionary described below | Operator confirmations for risks that cannot be fully verified through the public APIs. Required even during configured dry-run planning. They document completed reviews; they do not perform the work, bypass technical blockers, prove zero risk or authorize source-capacity retirement. |

### Parameter reference: each workspace's `approvals`

The commented example deliberately starts the boolean approvals at `False`; missing or false
required approvals block planning and migration. Set an approval to `True` only after the
named action/review is genuinely complete, not simply to make the notebook run.

| Parameter | Required value / when required | What the operator is confirming |
| --- | --- | --- |
| `change_reference` | Nonempty string; every configured workspace | Real approved change/ticket or review reference tying together the migration window, accountable operator and recovery/sign-off process. Replace the example placeholder. It is recorded in the migration journal. |
| `exclusive_write_freeze` | `True`; same-region and cross-region | Writes, deployments, ingestion, scheduled refreshes, pipelines and other relevant jobs have been stopped externally and in-flight operations have finished. Maintain exclusivity through backup, preflight, reassignment and pre-refresh validation. The notebook checks recent model refreshes but cannot impose a global lock or enumerate every query/job. |
| `source_capacity_retained` | `True`; same-region and cross-region | The original capacity will remain active and stable throughout migration/recovery and until explicit final sign-off. It will not be paused, deleted or retired just because assignment/API validation appears successful. |
| `capacity_settings_and_headroom_reviewed` | `True`; same-region and cross-region | Target workload settings, XMLA access, available headroom, model/resource limits and required licensing have been reviewed for the actual workload. The automatic SKU-equivalence check does not prove sufficient capacity or copy settings. |
| `no_private_link_or_cmk_migration_risk` | `True`; same-region and cross-region | Workspace/private networking and customer-managed-key arrangements are either absent or independently verified not to introduce an unsupported migration, access, encryption or recoverability risk. The notebook does not disable Private Link, change keys or weaken security. Unresolved risk means do not approve or move. |
| `external_dependencies_and_recovery_reviewed` | `True`; same-region and cross-region | External models/reports, upstream sources, credentials, apps, subscriptions, gateways and security dependencies have been reviewed; recovery backups, any backup encryption key and the actual restore procedure are usable. ABF checksum verification alone does not test a restore or validate every external dependency. |
| `cross_region_residency_and_gateways_approved` | `True` for cross-region; not required for same-region | The destination region is approved for data residency/compliance and gateway/network connectivity. This is independent of technical item eligibility: unsupported Fabric artifacts still block the move. Regions are detected from capacity metadata, not supplied as parameters. |
| `small_storage_format_stable_confirmed` | `True` for cross-region; not required for same-region | Every semantic model is confirmed in stable small storage format, with no storage-format conversion underway. The notebook also checks each model's `targetStorageMode`; large or unknown formats remain blocked regardless of this approval. |

### How the settings work together

- **Discover:** `mode="migrate"`, `dry_run=True`, `workspaces=[]`, `archive=None`.
- **Plan:** configure explicit mappings, archive, backup manifests and completed approvals; retain `dry_run=True`.
- **Move:** use the reviewed plan/configuration with `dry_run=False`; enable refreshes only if upstream data replacement is acceptable.
- **Recover validation:** `mode="validate"`, configured archive and exact `validation_journal_blob`; leave `workspaces=[]`. No assignment or refresh is submitted, but a new validation journal is written.

There is deliberately no force/ignore-errors switch, artifact-deletion option, automatic rollback,
storage-conversion option or source-capacity-retirement parameter. Configuration cannot make an
unsupported or data-risking move eligible. Final validation still requires a manual per-item
placement and consumer-health sign-off before retiring the original capacity.
"""),
        nbf.v4.new_code_cell('''CONFIG = {
    "mode": "migrate",  # migrate or validate (journal-based recovery; no assignment/refresh POSTs)
    "dry_run": True,
    "expected_tenant_id": "2ee4898e-0173-44a4-92bf-fca5f81a39ee",
    "refresh_semantic_models": False,
    "poll_seconds": 15,
    "assignment_timeout_seconds": 3600,
    "refresh_timeout_seconds": 7200,
    "backup_max_age_hours": 24,
    "archive": None,
    # "archive": {
    #     "account_url": "https://YOURACCOUNT.blob.core.windows.net",
    #     "container": "backup",
    #     "journal_prefix": "workspace-migrations",
    # },
    "validation_journal_blob": "",  # Exact pre-move/submitted journal blob, for mode=validate.
    "workspaces": [
        # {
        #     "workspace_id": "<workspace-guid>",
        #     "target_capacity_id": "<target-f-capacity-guid>",
        #     "backup_manifest_blob": "semantic-models/runs/<run-id>/manifest.json",
        #     "refresh_model_ids": None,  # None=all models; []=none; otherwise exact GUID list.
        #     "approvals": {
        #         "change_reference": "<reviewed-change-ticket>",
        #         "exclusive_write_freeze": False,
        #         "source_capacity_retained": False,
        #         "capacity_settings_and_headroom_reviewed": False,
        #         "no_private_link_or_cmk_migration_risk": False,
        #         "external_dependencies_and_recovery_reviewed": False,
        #         "cross_region_residency_and_gateways_approved": False,
        #         "small_storage_format_stable_confirmed": False,
        #     },
        # },
    ],
}
'''),
        nbf.v4.new_markdown_cell("""## Implementation

Embedded from `workspace_capacity_migration.py`; no extra file upload is needed.
Safety checks cannot be disabled by configuration. One blocked workspace blocks the entire batch
before submission. A later execution failure stops the remaining workspaces; earlier moves are not
rolled back. Inspect `MIGRATOR.report` and the immutable external JSON journal on failure.
"""),
        nbf.v4.new_code_cell((ROOT / "workspace_capacity_migration.py").read_text(encoding="utf-8")),
        nbf.v4.new_markdown_cell("""## Run and inspect results

First run with `dry_run=True`. Resolve every blocker, then use a small pilot and set
`dry_run=False` only in the approved maintenance window. To recover after interruption, set
`mode="validate"` with an exact journal blob containing the original inventory/models;
validation does not reassign a workspace or start refreshes.

**Never pause/delete the source capacity based on this notebook's API checks alone.**
Check **Workspace settings > Workspace type > View details** for partial migration warnings.
No warnings, correct item capacity/region, successful consumer report/gateway/RLS tests, and explicit
operator sign-off are required before decommissioning.
"""),
        nbf.v4.new_code_cell('''logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
MIGRATOR = create_migrator(CONFIG)
try:
    RESULT = MIGRATOR.run()
finally:
    # Print summary only: the journal contains protected inventory/connection metadata.
    print(json.dumps({
        "run_id": MIGRATOR.run_id,
        "status": MIGRATOR.report["status"],
        "journal": MIGRATOR.report.get("journal"),
        "workspaces": [
            {key: item[key] for key in ("workspace_id", "workspace_name", "status", "reason", "validation")
             if key in item}
            for item in MIGRATOR.report["workspaces"]
        ],
    }, indent=2))
if MIGRATOR.report["status"] == "discovery_only":
    print(json.dumps({
        "capacities": MIGRATOR.report["capacities"],
        "workspaces": MIGRATOR.report["accessible_workspaces"],
    }, indent=2))
import notebookutils
notebookutils.notebook.exit(json.dumps({
    "run_id": MIGRATOR.run_id,
    "status": MIGRATOR.report["status"],
    "journal": MIGRATOR.report.get("journal"),
}))
'''),
        nbf.v4.new_markdown_cell("""## References

- [Capacity reassignment restrictions and partial-migration warnings](https://learn.microsoft.com/fabric/admin/portal-workspace-capacity-reassignment)
- [P-to-F migration FAQ and equivalent SKU sizes](https://learn.microsoft.com/power-bi/support/premium-migration-faq)
- [Fabric Assign To Capacity API](https://learn.microsoft.com/rest/api/fabric/core/workspaces/assign-to-capacity)
- [Transactional semantic-model refresh API](https://learn.microsoft.com/rest/api/power-bi/datasets/refresh-dataset-in-group)

Rules reviewed 2026-10-15. Supported platform capabilities can change; this notebook remains
fail-closed for unknown item types, storage modes and assignment states.
"""),
    ]
    for index, cell in enumerate(cells):
        cell.id = f"capacity-migration-{index:02}"
        cell.source = cell.source.rstrip() + "\n"
    notebook.cells = cells
    nbf.validate(notebook)
    nbf.write(notebook, ROOT / "WorkspaceCapacityMigration.ipynb")


if __name__ == "__main__":
    build()
