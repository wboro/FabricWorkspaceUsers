import copy
import sys
import types
import unittest
from unittest.mock import Mock, patch

from azure.core.exceptions import ResourceNotFoundError

import semantic_model_backup as smb
from test_backup_restore import (
    ARCHIVE, MODEL, SOURCE, STAGING, TARGET,
    FakeApi, FakeNative, MemoryStore, configuration,
)


STORAGE = "55555555-5555-5555-5555-555555555555"
OTHER_STORAGE = "66666666-6666-6666-6666-666666666666"
CONTROL = "77777777-7777-7777-7777-777777777777"


class FakePowerBi:
    def __init__(self):
        self.links = {SOURCE: None, TARGET: None, CONTROL: STORAGE}
        self.accounts = [{"id": STORAGE, "name": "stagingacct", "isEnabled": True}]
        self.flows = {SOURCE: [], TARGET: []}
        self.assignments = []
        self.sleep = Mock()

    def storage_id(self, identifier):
        return self.links[identifier]

    def storage_accounts(self):
        return copy.deepcopy(self.accounts)

    def dataflows(self, identifier):
        return self.flows[identifier]

    def assign_storage(self, identifier, storage_id):
        self.assignments.append((identifier, storage_id))
        self.links[identifier] = storage_id


class TemporaryAdlsTests(unittest.TestCase):
    def setUp(self):
        self.config = configuration()
        self.config["backup"]["temporary_adls"] = {
            "enabled": True, "dataflow_storage_id": STORAGE, "timeout_seconds": 1,
        }
        self.store = MemoryStore()
        self.api = FakeApi()
        self.powerbi = FakePowerBi()

        def check_folder(location):
            identifier = SOURCE if location["blob"] == "Source" else TARGET
            if self.powerbi.links[identifier] is None:
                raise ResourceNotFoundError("No native workspace folder")

        self.store.check_native_folder = Mock(side_effect=check_folder)
        self.native = FakeNative(self.store, self.api)
        self.engine = smb.BackupRestore(
            self.config, self.api, self.store, self.native, smb.SecretResolver(), self.powerbi,
            control_workspace_id=CONTROL,
        )

    def failed_manifest(self):
        with self.assertLogs(smb.LOG, level="ERROR"), \
                self.assertRaisesRegex(RuntimeError, "semantic models backed up"):
            self.engine.backup()
        return self.store.get_json(self.store.json_writes[-1])

    def test_attach_backup_verify_then_restore_original_disconnected_state(self):
        original_upload = self.store.upload

        def upload(location, chunks, size):
            self.assertEqual(self.powerbi.links[SOURCE], STORAGE)
            original_upload(location, chunks, size)

        self.store.upload = upload
        manifest = self.engine.backup()
        self.assertEqual(self.powerbi.assignments, [(SOURCE, STORAGE), (SOURCE, None)])
        self.assertIsNone(self.powerbi.links[SOURCE])
        workspace = manifest["workspaces"][0]
        self.assertEqual(workspace["backup_status"], "succeeded")
        self.assertEqual(workspace["storage_connection"]["cleanup_status"], "succeeded")
        names = [loc["blob"].rsplit("/", 1)[-1] for loc in self.store.json_writes]
        self.assertEqual(names, [
            "started.json", "connection-before.json", "connection-after.json", "manifest.json",
        ])
        before = self.store.get_json(self.store.json_writes[1])
        self.assertIsNone(before["original_storage_id"])
        self.assertEqual(before["restore_dataflow_storage_id"], smb.EMPTY_GUID)

    def test_existing_connection_is_never_detached_and_actual_staging_is_used(self):
        self.powerbi.links[SOURCE] = OTHER_STORAGE
        self.powerbi.accounts = [
            {"id": STORAGE, "name": "controlacct", "isEnabled": True},
            {"id": OTHER_STORAGE, "name": "stagingacct", "isEnabled": True},
        ]
        self.powerbi.flows[SOURCE] = [{"objectId": "existing-gen1"}]
        self.config["staging_account_url"] = ARCHIVE
        manifest = self.engine.backup()
        self.assertEqual(manifest["status"], "succeeded")
        self.assertEqual(self.powerbi.assignments, [])
        self.assertEqual(self.powerbi.links[SOURCE], OTHER_STORAGE)
        connection = manifest["workspaces"][0]["storage_connection"]
        self.assertEqual(connection["status"], "unchanged")
        self.assertEqual(connection["staging_account_url"], STAGING)

    def test_disabled_feature_makes_no_management_calls(self):
        self.config["backup"]["temporary_adls"]["enabled"] = False
        self.engine.powerbi = Mock()
        self.engine.backup()
        self.assertEqual(self.engine.powerbi.mock_calls, [])

    def test_control_connection_missing_fails_before_any_source_changes(self):
        self.powerbi.links[CONTROL] = None
        manifest = self.failed_manifest()
        self.assertIn("One-time control workspace setup", manifest["error"])
        self.assertEqual(manifest["control_workspace"]["status"], "failed")
        self.assertEqual(manifest["workspaces"], [])
        self.assertEqual(self.powerbi.assignments, [])
        self.assertEqual(self.native.backups, [])
        self.assertEqual(manifest["summary"]["models_backed_up"], 0)
        self.assertEqual(manifest["summary"]["models_failed"], 0)
        self.assertEqual(manifest["summary"]["models_attempted"], 0)
        self.assertEqual(manifest["summary"]["workspaces_not_attempted"], 1)
        self.assertEqual(manifest["summary"]["workspaces_without_model_inventory"], 1)

    def test_disabled_control_registration_is_not_automatically_repaired(self):
        self.powerbi.accounts[0]["isEnabled"] = False
        manifest = self.failed_manifest()
        self.assertIn("persistent workspace-level connection", manifest["error"])
        self.assertEqual(self.powerbi.assignments, [])
        self.assertEqual(self.powerbi.links[CONTROL], STORAGE)

    def test_missing_runtime_control_id_is_not_replaced_with_a_configured_source(self):
        self.engine.control_workspace_id = None
        manifest = self.failed_manifest()
        self.assertIn("currentWorkspaceId", manifest["error"])
        self.assertEqual(self.powerbi.assignments, [])

    def test_control_workspace_can_change_without_source_configuration_changes(self):
        self.engine.control_workspace_id = TARGET
        self.powerbi.links[TARGET] = STORAGE
        manifest = self.engine.backup()
        self.assertEqual(manifest["control_workspace"]["id"], TARGET)
        self.assertEqual(self.powerbi.links[TARGET], STORAGE)
        self.assertNotIn((TARGET, None), self.powerbi.assignments)

    def test_control_workspace_in_backup_list_is_never_detached(self):
        self.config["backup"]["workspaces"].insert(0, {"id": CONTROL})
        self.api.items[CONTROL] = []
        manifest = self.engine.backup()
        self.assertEqual(self.powerbi.assignments, [(SOURCE, STORAGE), (SOURCE, None)])
        self.assertEqual(self.powerbi.links[CONTROL], STORAGE)
        self.assertEqual(manifest["workspaces"][0]["storage_connection"]["status"], "unchanged")

    def test_control_unlinked_during_run_is_not_repaired_or_replaced_by_a_source(self):
        original = self.powerbi.storage_id
        control_reads = 0

        def storage_id(identifier):
            nonlocal control_reads
            if identifier == CONTROL:
                control_reads += 1
                if control_reads > 1:
                    self.powerbi.links[CONTROL] = None
            return original(identifier)

        self.powerbi.storage_id = storage_id
        manifest = self.failed_manifest()
        self.assertIn("Control workspace ADLS connection changed", manifest["workspaces"][0]["error"])
        self.assertEqual(self.powerbi.assignments, [])
        self.assertIsNone(self.powerbi.links[SOURCE])

    def test_control_link_changed_before_its_own_backup_is_not_temporarily_attached(self):
        self.config["backup"]["workspaces"] = [{"id": CONTROL}]
        self.api.items[CONTROL] = []
        self.powerbi.storage_id = Mock(side_effect=[STORAGE, None])
        manifest = self.failed_manifest()
        self.assertIn("control workspace connection changed", manifest["workspaces"][0]["error"])
        self.assertEqual(self.powerbi.assignments, [])

    def test_default_temporary_account_follows_control_instead_of_old_global_staging_url(self):
        self.config["staging_account_url"] = ARCHIVE
        self.config["backup"]["temporary_adls"]["dataflow_storage_id"] = None
        manifest = self.engine.backup()
        self.assertEqual(manifest["status"], "succeeded")
        self.assertEqual(manifest["workspaces"][0]["storage_connection"]["staging_account_url"], STAGING)

    def test_unlinked_sources_cannot_use_an_unanchored_registration(self):
        self.powerbi.accounts.append({
            "id": OTHER_STORAGE, "name": "otheracct", "isEnabled": True,
        })
        self.config["backup"]["temporary_adls"]["dataflow_storage_id"] = OTHER_STORAGE
        manifest = self.failed_manifest()
        self.assertIn("control workspace's registration", manifest["workspaces"][0]["error"])
        self.assertEqual(self.powerbi.assignments, [])

    def test_multiple_workspaces_have_independent_attachment_and_recovery_records(self):
        self.powerbi.storage_accounts = Mock(wraps=self.powerbi.storage_accounts)
        self.config["backup"]["workspaces"].append({"id": TARGET})
        manifest = self.engine.backup()
        self.assertEqual(self.powerbi.assignments, [
            (SOURCE, STORAGE), (SOURCE, None), (TARGET, STORAGE), (TARGET, None),
        ])
        self.assertEqual([w["backup_status"] for w in manifest["workspaces"]], ["succeeded", "succeeded"])
        journals = [
            self.store.get_json(loc) for loc in self.store.json_writes
            if loc["blob"].endswith("connection-before.json")
        ]
        self.assertEqual([j["workspace_id"] for j in journals], [SOURCE, TARGET])
        self.powerbi.storage_accounts.assert_called_once()

    def test_registration_auto_discovery_by_staging_account(self):
        self.config["backup"]["temporary_adls"]["dataflow_storage_id"] = None
        self.engine.backup()
        self.assertEqual(self.powerbi.assignments[0], (SOURCE, STORAGE))

    def test_workspace_registration_override(self):
        self.config["backup"]["temporary_adls"]["dataflow_storage_id"] = OTHER_STORAGE
        self.config["backup"]["workspaces"][0]["dataflow_storage_id"] = STORAGE
        self.engine.backup()
        self.assertEqual(self.powerbi.assignments[0], (SOURCE, STORAGE))

    def test_existing_gen1_dataflows_prevent_attachment(self):
        self.powerbi.flows[SOURCE] = [{"objectId": "gen1"}]
        manifest = self.failed_manifest()
        self.assertIn("Gen1", manifest["workspaces"][0]["error"])
        self.assertEqual(self.powerbi.assignments, [])
        self.assertEqual(self.native.backups, [])

    def test_missing_disabled_or_mismatched_registration_never_attaches(self):
        self.config["backup"]["workspaces"][0]["staging_account_url"] = STAGING
        for accounts in (
            [], [{"id": STORAGE, "name": "stagingacct", "isEnabled": False}],
            [{"id": STORAGE, "name": "otheracct", "isEnabled": True}],
        ):
            with self.subTest(accounts=accounts):
                self.powerbi.accounts = accounts
                self.failed_manifest()
                self.assertEqual(self.powerbi.assignments, [])

    def test_custom_folder_for_new_connection_is_rejected(self):
        self.config["backup"]["workspaces"][0]["staging_folder"] = "Unexpected"
        self.failed_manifest()
        self.assertEqual(self.powerbi.assignments, [])

    def test_recovery_journal_must_persist_before_assignment(self):
        original = self.store.put_json

        def put(location, document):
            if location["blob"].endswith("connection-before.json"):
                raise OSError("Journal unavailable")
            original(location, document)

        self.store.put_json = put
        manifest = self.failed_manifest()
        self.assertIn("Journal unavailable", manifest["workspaces"][0]["error"])
        self.assertEqual(self.powerbi.assignments, [])

    def test_changed_connection_before_assignment_is_not_overwritten(self):
        original = self.powerbi.storage_id
        source_states = iter([None, OTHER_STORAGE])
        self.powerbi.storage_id = Mock(side_effect=lambda identifier: (
            next(source_states) if identifier == SOURCE else original(identifier)
        ))
        self.failed_manifest()
        self.assertEqual(self.powerbi.assignments, [])

    def test_assignment_error_after_server_commit_is_reconciled_and_detached(self):
        original = self.powerbi.assign_storage

        def assign(identifier, storage_id):
            original(identifier, storage_id)
            if storage_id is not None:
                raise TimeoutError("Lost assignment response")

        self.powerbi.assign_storage = assign
        manifest = self.failed_manifest()
        self.assertIsNone(self.powerbi.links[SOURCE])
        self.assertEqual(self.powerbi.assignments, [(SOURCE, STORAGE), (SOURCE, None)])
        self.assertEqual(manifest["workspaces"][0]["storage_connection"]["cleanup_status"], "succeeded")
        self.assertEqual(self.native.backups, [])

    def test_failed_assignment_with_unobserved_outcome_is_not_reported_as_rolled_back(self):
        self.powerbi.assign_storage = Mock(side_effect=TimeoutError("No assignment response"))
        manifest = self.failed_manifest()
        connection = manifest["workspaces"][0]["storage_connection"]
        self.assertEqual(connection["cleanup_status"], "failed")
        self.assertIn("delayed assignment", connection["cleanup_error"])
        self.assertEqual(self.powerbi.assign_storage.call_count, 1)

    def test_attachment_poll_timeout_is_bounded_and_no_backup_runs(self):
        self.powerbi.assign_storage = Mock()
        manifest = self.failed_manifest()
        self.assertIn("Timed out", manifest["workspaces"][0]["error"])
        self.powerbi.sleep.assert_called_once_with(1)
        self.assertEqual(self.native.backups, [])

    def test_native_folder_is_polled_without_creating_it(self):
        self.store.check_native_folder.side_effect = [
            ResourceNotFoundError("No old folder"), ResourceNotFoundError("Not provisioned"), None,
        ]
        self.engine.backup()
        self.assertEqual(self.store.check_native_folder.call_count, 3)
        self.powerbi.sleep.assert_called_once_with(1)

    def test_orphaned_native_folder_is_not_deleted_or_reused_for_reattachment(self):
        self.store.check_native_folder.side_effect = None
        self.store.check_native_folder.return_value = None
        manifest = self.failed_manifest()
        self.assertIn("Move or rename", manifest["workspaces"][0]["error"])
        self.assertEqual(self.powerbi.assignments, [])

    def test_folder_access_denied_is_not_silently_retried_and_connection_is_restored(self):
        self.store.check_native_folder.side_effect = PermissionError("No folder access")
        manifest = self.failed_manifest()
        self.assertIn("No folder access", manifest["workspaces"][0]["error"])
        self.assertIsNone(self.powerbi.links[SOURCE])
        self.powerbi.sleep.assert_not_called()
        self.assertEqual(self.powerbi.assignments, [])

    def test_model_discovery_error_still_disconnects(self):
        self.api.models = Mock(side_effect=RuntimeError("Discovery failed"))
        self.failed_manifest()
        self.assertIsNone(self.powerbi.links[SOURCE])

    def test_archive_transfer_failure_still_disconnects(self):
        self.store.upload = Mock(side_effect=OSError("Archive write failed"))
        manifest = self.failed_manifest()
        self.assertIsNone(self.powerbi.links[SOURCE])
        self.assertEqual(manifest["workspaces"][0]["backup_status"], "failed")

    def test_uncertain_native_error_leaves_connection_for_safe_manual_recovery(self):
        self.native.fail_models.add(MODEL)
        manifest = self.failed_manifest()
        workspace = manifest["workspaces"][0]
        self.assertEqual(self.powerbi.links[SOURCE], STORAGE)
        self.assertEqual(self.powerbi.assignments, [(SOURCE, STORAGE)])
        self.assertEqual(workspace["storage_connection"]["cleanup_status"], "failed")
        self.assertIn("uncertain", workspace["storage_cleanup_error"])
        self.assertEqual(workspace["backup_status"], "failed")

    def test_new_gen1_dataflow_prevents_detach_but_verified_backup_is_restorable(self):
        original = self.native.backup

        def backup(*args):
            original(*args)
            self.powerbi.flows[SOURCE] = [{"objectId": "new-gen1"}]

        self.native.backup = backup
        manifest = self.failed_manifest()
        workspace = manifest["workspaces"][0]
        self.assertEqual(workspace["backup_status"], "succeeded")
        self.assertEqual(workspace["status"], "failed")
        self.assertEqual(self.powerbi.links[SOURCE], STORAGE)
        self.config["restore"]["run_id"] = manifest["run_id"]
        before = list(self.powerbi.assignments)
        report = self.engine.restore()
        self.assertEqual(report["status"], "dry_run")
        self.assertEqual(self.powerbi.assignments, before)
        self.config["restore"]["dry_run"] = False
        self.assertEqual(self.engine.restore()["status"], "succeeded")
        self.assertEqual(self.powerbi.assignments, before)

    def test_external_connection_change_is_not_overwritten_on_cleanup(self):
        original = self.native.backup

        def backup(*args):
            original(*args)
            self.powerbi.links[SOURCE] = OTHER_STORAGE

        self.native.backup = backup
        manifest = self.failed_manifest()
        self.assertEqual(self.powerbi.links[SOURCE], OTHER_STORAGE)
        self.assertIn("changed externally", manifest["workspaces"][0]["storage_cleanup_error"])

    def test_cleanup_failure_is_recorded_without_hiding_backup_success(self):
        original = self.powerbi.assign_storage

        def assign(identifier, storage_id):
            if storage_id is None:
                raise PermissionError("Detach forbidden")
            original(identifier, storage_id)

        self.powerbi.assign_storage = assign
        manifest = self.failed_manifest()
        workspace = manifest["workspaces"][0]
        self.assertEqual(workspace["backup_status"], "succeeded")
        self.assertIn("Detach forbidden", workspace["storage_cleanup_error"])
        self.assertEqual(manifest["summary"]["models_backed_up"], 1)
        self.assertEqual(manifest["summary"]["models_failed"], 0)
        self.assertEqual(manifest["summary"]["workspace_cleanup_failures"], 1)
        self.assertEqual(manifest["summary"]["workspaces_failed"], 1)
        self.assertEqual(manifest["summary"]["workspaces_without_model_inventory"], 0)
        after = self.store.get_json(self.store.json_writes[-2])
        self.assertEqual(after["cleanup_status"], "failed")
        self.assertIn("Detach forbidden", after["cleanup_error"])

    def test_final_manifest_failure_happens_after_connection_restored(self):
        original = self.store.put_json

        def put(location, document):
            if location["blob"].endswith("/manifest.json"):
                raise OSError("Manifest unavailable")
            original(location, document)

        self.store.put_json = put
        with self.assertRaisesRegex(OSError, "Manifest unavailable"):
            self.engine.backup()
        self.assertIsNone(self.powerbi.links[SOURCE])

    def test_cleanup_journal_failure_fails_run_after_restoring_connection(self):
        original = self.store.put_json

        def put(location, document):
            if location["blob"].endswith("connection-after.json"):
                raise OSError("Cleanup journal unavailable")
            original(location, document)

        self.store.put_json = put
        manifest = self.failed_manifest()
        self.assertIsNone(self.powerbi.links[SOURCE])
        self.assertEqual(manifest["workspaces"][0]["backup_status"], "succeeded")
        self.assertIn("Cleanup journal unavailable", manifest["workspaces"][0]["storage_journal_error"])

    def test_keyboard_interrupt_before_xmla_still_restores_connection(self):
        self.api.models = Mock(side_effect=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.engine.backup()
        self.assertIsNone(self.powerbi.links[SOURCE])

    def test_keyboard_interrupt_during_xmla_defers_disconnect(self):
        self.native.backup = Mock(side_effect=KeyboardInterrupt())
        with self.assertLogs(smb.LOG, level="ERROR"), self.assertRaises(KeyboardInterrupt):
            self.engine.backup()
        self.assertEqual(self.powerbi.links[SOURCE], STORAGE)

    def test_invalid_options_fail_before_any_writes(self):
        for options in (
            {"enabled": "true"}, {"enabled": True, "timeout_seconds": True},
            {"enabled": True, "timeout_seconds": 0}, {"enabled": True, "timeout_seconds": 1801},
        ):
            with self.subTest(options=options):
                self.config["backup"]["temporary_adls"] = options
                with self.assertRaises(ValueError):
                    self.engine.backup()
                self.assertEqual(self.store.blobs, {})


class PowerBiApiTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.provider = Mock(return_value="test-token")
        self.api = smb.PowerBiApi(self.provider, self.session, Mock())

    def response(self, status=200, body=None):
        response = Mock(status_code=status, headers={})
        response.json.return_value = body
        return response

    def test_group_storage_state_normalizes_empty_and_missing_ids(self):
        for body in ({}, {"dataflowStorageId": smb.EMPTY_GUID}, {"dataflowStorageId": None}):
            self.session.get.return_value = self.response(body=body)
            self.assertIsNone(self.api.storage_id(SOURCE))
        self.session.get.return_value = self.response(body={"dataflowStorageId": STORAGE})
        self.assertEqual(self.api.storage_id(SOURCE), STORAGE)

    def test_assign_and_detach_use_documented_endpoint_and_empty_guid(self):
        self.session.post.return_value = self.response()
        self.api.assign_storage(SOURCE, STORAGE)
        self.assertEqual(self.session.post.call_args.kwargs["json"], {"dataflowStorageId": STORAGE})
        self.assertEqual(
            self.session.post.call_args.args[0],
            smb.POWERBI_API + f"groups/{SOURCE}/AssignToDataflowStorage",
        )
        self.api.assign_storage(SOURCE, None)
        self.assertEqual(self.session.post.call_args.kwargs["json"], {"dataflowStorageId": smb.EMPTY_GUID})
        self.assertFalse(self.session.post.call_args.kwargs["allow_redirects"])
        self.assertEqual(self.provider.call_count, 2)

    def test_assignment_failure_or_redirect_is_never_retried(self):
        for status in (302, 403, 429, 503):
            self.session.post.reset_mock()
            self.session.post.return_value = self.response(status=status)
            with self.subTest(status=status), self.assertRaisesRegex(RuntimeError, f"HTTP {status}"):
                self.api.assign_storage(SOURCE, STORAGE)
            self.assertEqual(self.session.post.call_count, 1)

    def test_powerbi_pagination_is_trusted_and_loops_rejected(self):
        self.session.get.return_value = self.response(body={
            "value": [], "@odata.nextLink": "https://evil.example/token",
        })
        with self.assertRaisesRegex(ValueError, "untrusted"):
            self.api.storage_accounts()
        self.assertEqual(self.session.get.call_count, 1)
        self.session.get.return_value = self.response(body={
            "value": [], "@odata.nextLink": smb.POWERBI_API + "dataflowStorageAccounts",
        })
        with self.assertRaisesRegex(RuntimeError, "repeated"):
            self.api.storage_accounts()

    def test_delegated_authorization_failure_is_explicit(self):
        self.session.get.return_value = self.response(status=403)
        with self.assertRaisesRegex(RuntimeError, "Power BI GET failed: HTTP 403"):
            self.api.storage_accounts()
        self.assertEqual(self.session.get.call_count, 1)


class EntrypointIdentityTests(unittest.TestCase):
    def test_backup_uses_notebook_user_only_for_management_and_closes_both_sessions(self):
        config = configuration()
        config["service_principal"] = {
            "tenant_id": "test-tenant", "client_id": "test-client", "client_secret": "test-secret",
        }
        config["backup"]["temporary_adls"] = {"enabled": True}
        utilities = types.ModuleType("notebookutils")
        utilities.credentials = Mock()
        utilities.runtime = types.SimpleNamespace(context={"currentWorkspaceId": CONTROL})
        with (
            patch.dict(sys.modules, {"notebookutils": utilities}),
            patch("azure.identity.ClientSecretCredential") as credential,
            patch.object(smb, "FabricApi") as fabric,
            patch.object(smb, "PowerBiApi") as powerbi,
            patch.object(smb, "BlobStore") as store,
            patch.object(smb, "NativeXmla") as native,
            patch.object(smb, "BackupRestore") as engine,
        ):
            engine.return_value.backup.return_value = {"status": "succeeded"}
            self.assertEqual(smb.run(config)["status"], "succeeded")
            fabric.assert_called_once_with(credential.return_value)
            store.assert_called_once_with(credential.return_value)
            native.assert_called_once_with(credential.return_value)
            provider = powerbi.call_args.args[0]
            provider()
            utilities.credentials.getToken.assert_called_once_with("pbi")
            self.assertIs(engine.call_args.args[-1], powerbi.return_value)
            self.assertEqual(engine.call_args.kwargs["control_workspace_id"], CONTROL)
            fabric.return_value.session.close.assert_called_once()
            powerbi.return_value.session.close.assert_called_once()
            credential.return_value.close.assert_called_once()

    def test_restore_does_not_initialize_temporary_management_even_when_option_is_enabled(self):
        config = configuration()
        config["mode"] = "restore"
        config["service_principal"] = {
            "tenant_id": "test-tenant", "client_id": "test-client", "client_secret": "test-secret",
        }
        config["backup"]["temporary_adls"] = {"enabled": True}
        with (
            patch("azure.identity.ClientSecretCredential"),
            patch.object(smb, "FabricApi"),
            patch.object(smb, "PowerBiApi") as powerbi,
            patch.object(smb, "BlobStore"),
            patch.object(smb, "NativeXmla"),
            patch.object(smb, "BackupRestore") as engine,
        ):
            smb.run(config)
            powerbi.assert_not_called()
            engine.return_value.restore.assert_called_once()


if __name__ == "__main__":
    unittest.main()
