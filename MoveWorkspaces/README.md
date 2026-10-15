# Workspace capacity migration

`WorkspaceCapacityMigration.ipynb` is a self-contained Fabric PySpark notebook,
styled after the `SemanticModelBackup` notebook: introduction, prerequisite check,
one configuration cell, embedded implementation, and execution/results.
The editable implementation is `workspace_capacity_migration.py`.
The dictionary-based `CONFIG` cell is intentionally not tagged as a scalar
pipeline-parameters cell. Scheduled runs use the saved configuration. Successful
runs return their run ID, result status and journal location as the notebook exit value.

## Safety boundaries

This is a **conservative reassignment tool, not a delete-and-recreate migration tool**.
It supports P-to-F and F-to-F moves to active, equivalent-or-larger paid F capacities.
P1/P2/P3/P4/P5 map to F64/F128/F256/F512/F1024. Downsizing is blocked rather than
guessing at model-memory or workload-headroom limits.

Only reports, paginated reports, dashboards and Import semantic models are certified.
Other item types, Fabric workloads, dataflows (including Gen1), datamarts, scorecards,
Direct Lake, DirectQuery, live, push/streaming, unknown modes, unreadable inventories,
and unhealthy models are **blocked**, including in the same region. This intentionally
supports fewer workloads than the platform: there is no generic non-destructive
health/data validation API covering every Fabric workload.

Cross-region moves require each semantic model to have `targetStorageMode=Abf`
(small storage), an explicit confirmation that storage conversion is not in progress,
and residency/gateway approval. Large storage (`PremiumFiles`) blocks the move.
The workspace's default storage setting is never used to infer individual model formats.
Unsupported artifacts are **never deleted** to make a cross-region move succeed.
Hidden Dataflow Gen2 staging items are not cleaned up or bypassed.

**No script can guarantee zero data loss.** This notebook blocks known risks and
unknown/unverifiable technical states, requires verified recovery backups and an
operator-maintained write freeze, and never decommissions capacity. A service failure
can still leave a partial migration. The original source must remain active.

## Identity and prerequisites

- Run the notebook as a user in the configured tenant, with Workspace Admin on each
  source, destination capacity Contributor/Admin, and model read/write permissions
  plus XMLA read access. It runs as the **executing user**, not a copied secret or a
  hard-coded service principal.
- Use a compatible Fabric runtime/Environment containing `requests`, `azure-core`,
  `azure-storage-blob`, and `semantic-link-sempy` with `connect_semantic_model` and
  `evaluate_dax`. The notebook checks migration imports before any reassignment.
  It does **not** automatically upgrade the Spark session's runtime libraries:
  automatic inline upgrades caused scheduled-session statement failures in Dev.
  If missing, provision the libraries in a Fabric Environment and restart the session.
  The setup cell also includes a commented optional interactive `%pip` command;
  run it only during setup, never during a migration.
- The executing user needs Blob read access to the existing `SemanticModelBackup`
  archive and Blob create access for immutable migration journals. The notebook does
  not create containers or modify storage permissions/networking. Protect journal
  access: inventories include connection metadata and workspace role assignments.
- Run from a separate control workspace. Its ID is resolved from
  `notebookutils.runtime.context`; migrating that workspace is blocked.
- A human must review destination workload settings, XMLA, CMK/private networking,
  headroom, external model/report dependencies, gateway region compatibility,
  recovery procedures and data residency. Record the approved change ticket.
  The notebook does not weaken security or disable Private Link/CMK.

## Runbook

1. Run with the supplied empty `workspaces` list and `dry_run=True` to discover
   accessible capacity/workspace GUIDs. No assignment or refresh is submitted.
2. Schedule an exclusive maintenance window: pause refresh schedules, pipelines,
   deployments, ingestion and writers **externally**, and let jobs/queries finish.
   The notebook checks recent refresh status but cannot impose a global write lock
   or discover every interactive operation. Keep that freeze throughout backup,
   preflight, reassignment and pre-refresh validation. Approvals are required
   even in dry-run; false/missing approvals are blockers, not bypass switches.
3. Run `SemanticModelBackup` under the freeze. Complete and verify its native ABF
   backups; confirm the actual recovery procedure and any backup encryption key.
   Configure `archive` and each mapping's exact `backup_manifest_blob`.
   No archive location, service principal secret or storage account is copied from
   the reference notebook. Default maximum backup age is 24 hours.
4. Populate explicit workspace/target-capacity GUIDs and approvals in `CONFIG`.
   Dry-run all workspaces. Any blocked workspace blocks the **whole batch** before
   the first assignment. Backups must exactly match model IDs/names, be complete,
   newer than model/schema/partition changes, and pass full size, ETag and SHA-256
   read-back verification. Backup blobs stream in bounded SDK chunks.
5. Pilot a small workspace, then set `dry_run=False` in the approved window.
   The notebook persists an immutable external journal before submission, rechecks
   safety evidence, submits each assignment once, and polls assignment completion
   and destination region. It stops the remaining batch on any failure. Previously
   completed moves are **not** rolled back.
6. Post-move checks compare item IDs/types/names/folders, report model bindings,
   workspace roles, XMLA schema fingerprints, every table's row count, datasource
   bindings, and refresh schedules. These checks are mandatory, not skipped when
   optional refresh is disabled.
7. Optionally set `refresh_semantic_models=True`; `refresh_model_ids=None` means all
   models, `[]` means none, or specify exact model GUIDs per mapping. Refreshes run
   sequentially with `commitMode=transactional`, `applyRefreshPolicy=False` and
   no explicit retries. This refreshes existing partitions without advancing an
   incremental-retention policy that could remove historical partitions. It is not
   a source snapshot guarantee: a successful refresh may legitimately change rows.
   Each exact refresh ID is saved and polled; schema/bindings/schedules are checked
   again afterward. Do not enable refresh if changed upstream data must not replace
   the current model data.
8. The final result is `validated_pending_manual_placement_check`, **not approval to
   retire the source**. In Workspace settings > Workspace type, inspect warnings
   and View details for items still attached to the source. Open every report,
   paginated report and dashboard as representative consumers; test gateways,
   RLS/SSO, subscriptions, apps and external dependencies. Public item GET does not
   expose actual per-item capacity placement, and successful row-count queries do
   not prove that an item stopped using the source capacity. Obtain explicit
   sign-off before any source retirement outside this notebook.

Failures leave `MIGRATOR.report` in memory; immutable journals are written to
`<journal_prefix>/<run_id>/<sequence>.json`. Journal and backup containers must reside
outside migrating workspaces and be retained through recovery/sign-off.
The notebook prints only a summary, not full inventories.

## Interrupted/uncertain runs

Never blindly resubmit assignment or refresh POSTs after timeout or interruption.
Keep **both capacities active** and inspect the saved operation/refresh IDs.
To rerun read-only migration validation, set `mode="validate"` and
`validation_journal_blob` to an exact journal containing the original pre-move
inventory/models (prefer the `assignment_submitted` record).
This mode ignores migration mappings and performs no assignment/refresh POSTs;
it writes a new journal if archive access is configured. It cannot reconstruct a
missing baseline, complete a failed assignment, or resume/reattempt refreshes.
Monitor submitted refresh IDs using the Power BI refresh execution-details API.

If the durable journal write fails, no further mutation is allowed. If submission
was already attempted, treat its result as uncertain and investigate service state.
No automatic rollback, source deletion, storage-mode conversion, takeover,
credential modification, report rebinding or artifact recreation is implemented.

## Local authoring and deployment

Local authoring/tests use the dependencies in `requirements.txt`.
Install them with `python -m pip install -r requirements.txt` if missing.
Fabric-specific libraries belong in the compatible Fabric runtime/Environment.

```powershell
python tools\build_notebook.py
python -m unittest discover -s tests -v
python tools\fabric_notebook.py deploy --workspace Dev --name WorkspaceCapacityMigration --file WorkspaceCapacityMigration.ipynb
```

The deployment helper requires the cached Azure CLI account
`wborowiecki@MngEnvMCAP837260.onmicrosoft.com`; it verifies that identity, resolves
`Dev` and its active capacity, finds `SemanticModelBackup`, and creates the new
notebook in the same folder (workspace root if the reference is at root).
It never edits the reference. Existing exact-name notebooks are updated in place.
The notebook is deployed with dry-run discovery defaults; deployment never migrates
a workspace or refreshes a semantic model.

## Platform references

Rules reviewed 2026-10-15:

- [Capacity reassignment restrictions and partial migration](https://learn.microsoft.com/fabric/admin/portal-workspace-capacity-reassignment)
- [P-to-F FAQ and SKU mapping](https://learn.microsoft.com/power-bi/support/premium-migration-faq)
- [Assign To Capacity](https://learn.microsoft.com/rest/api/fabric/core/workspaces/assign-to-capacity)
- [Workspace capacity assignment progress](https://learn.microsoft.com/rest/api/fabric/core/workspaces/get-workspace)
- [Per-model storage format](https://learn.microsoft.com/rest/api/power-bi/datasets/get-dataset-in-group)
- [Transactional refresh](https://learn.microsoft.com/rest/api/power-bi/datasets/refresh-dataset-in-group)
