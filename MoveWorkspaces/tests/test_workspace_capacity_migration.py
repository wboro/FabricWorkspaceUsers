import ast
import copy
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import nbformat

import workspace_capacity_migration as migration
from tools.build_notebook import build


WS = "11111111-1111-1111-1111-111111111111"
SOURCE = "22222222-2222-2222-2222-222222222222"
TARGET = "33333333-3333-3333-3333-333333333333"
CONTROL = "44444444-4444-4444-4444-444444444444"
MODEL = "55555555-5555-5555-5555-555555555555"
REPORT = "66666666-6666-6666-6666-666666666666"
REFRESH = "77777777-7777-7777-7777-777777777777"


def config():
    return {
        "mode": "migrate", "dry_run": True, "refresh_semantic_models": False,
        "poll_seconds": 10, "assignment_timeout_seconds": 60, "refresh_timeout_seconds": 60,
        "backup_max_age_hours": 24,
        "workspaces": [{
            "workspace_id": WS, "target_capacity_id": TARGET,
            "backup_manifest_blob": "semantic-models/runs/backup/manifest.json",
            "refresh_model_ids": None,
            "approvals": {
                "change_reference": "CHG123",
                "exclusive_write_freeze": True,
                "source_capacity_retained": True,
                "capacity_settings_and_headroom_reviewed": True,
                "no_private_link_or_cmk_migration_risk": True,
                "external_dependencies_and_recovery_reviewed": True,
                "cross_region_residency_and_gateways_approved": True,
                "small_storage_format_stable_confirmed": True,
            },
        }],
    }


class Response:
    def __init__(self, body=None, status=200, headers=None):
        self.body, self.status_code, self.headers = body, status, headers or {}

    def json(self):
        return copy.deepcopy(self.body)


class FakeApi:
    def __init__(self):
        self.workspace = {"id": WS, "displayName": "Pilot", "type": "Workspace", "capacityId": SOURCE,
                          "capacityRegion": "West US 3", "capacityAssignmentProgress": "Completed"}
        self.source = {"id": SOURCE, "displayName": "P1", "sku": "P1", "region": "West US 3", "state": "Active"}
        self.target = {"id": TARGET, "displayName": "F64", "sku": "F64", "region": "East US", "state": "Active"}
        self.model = {"id": MODEL, "name": "Sales", "targetStorageMode": "Abf", "addRowsAPIEnabled": False}
        self.items = [{"id": MODEL, "displayName": "Sales", "type": "SemanticModel"},
                      {"id": REPORT, "displayName": "Sales report", "type": "Report"}]
        self.reports = [{"id": REPORT, "name": "Sales report", "datasetId": MODEL}]
        self.history = []
        self.dataflows = []
        self.mutations = []
        self.query_status = "Completed"
        self.progress_after_move = "Completed"
        self.post_assignment_effect = None

    def get(self, url):
        if url == migration.FABRIC + f"workspaces/{WS}":
            return copy.deepcopy(self.workspace)
        if url == migration.FABRIC + f"capacities/{SOURCE}":
            return copy.deepcopy(self.source)
        if url == migration.FABRIC + f"capacities/{TARGET}":
            return copy.deepcopy(self.target)
        if url.endswith("/refreshes/" + REFRESH):
            return {"status": self.query_status}
        if url.endswith("/refreshSchedule"):
            return {"enabled": False, "days": [], "times": []}
        if url.endswith("/datasets/" + MODEL):
            return copy.deepcopy(self.model)
        raise AssertionError("Unexpected GET " + url)

    def listing(self, url):
        if url.endswith("workspaces?roles=Admin"):
            return [{"id": WS}]
        if url.endswith("items?recursive=true"):
            return copy.deepcopy(self.items)
        if url.endswith("/datasets"):
            return [copy.deepcopy(self.model)]
        if url.endswith("/reports"):
            return copy.deepcopy(self.reports)
        if url.endswith("/dashboards"):
            return []
        if url.endswith("/dataflows"):
            return copy.deepcopy(self.dataflows)
        if url.endswith("/roleAssignments"):
            return [{"id": CONTROL, "role": "Admin", "principal": {"id": CONTROL, "type": "User"}}]
        if url.endswith("/datasources"):
            return [{"datasourceId": SOURCE, "gatewayId": TARGET, "datasourceType": "Sql"}]
        if "/refreshes?" in url:
            return copy.deepcopy(self.history)
        if url.endswith("/capacities"):
            return [self.source, self.target]
        if url.endswith("/workspaces"):
            return [self.workspace]
        raise AssertionError("Unexpected list " + url)

    def request(self, method, url, body):
        self.mutations.append((method, url, body))
        if url.endswith("/assignToCapacity"):
            self.workspace.update(capacityId=TARGET, capacityRegion=self.target["region"],
                                  capacityAssignmentProgress=self.progress_after_move)
            if self.post_assignment_effect:
                self.post_assignment_effect()
            return Response(status=202)
        if url.endswith("/refreshes"):
            return Response(status=202, headers={"Location": url + "/" + REFRESH, "x-ms-request-id": REFRESH})
        raise AssertionError("Unexpected POST " + url)


class FakeProbe:
    def __init__(self):
        self.rows = 42
        self.last_change = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        self.schema = "schema"

    def snapshot(self, ws, model):
        return {"schema_hash": self.schema, "rows": {"Sales": self.rows},
                "last_change": self.last_change, "actual_storage": "InMemory"}


class FakeArchive:
    def __init__(self):
        self.documents = []
        self.verifications = []
        self.journal_failure = False
        now = datetime.now(timezone.utc)
        self.manifest = {
            "schema_version": 1, "kind": "native_abf", "run_id": "backup",
            "status": "succeeded", "started_at": (now - timedelta(hours=2)).isoformat(),
            "completed_at": (now - timedelta(hours=1)).isoformat(),
            "workspaces": [{
                "id": WS, "status": "succeeded", "model_discovery_completed": True,
                "models": [{"id": MODEL, "name": "Sales", "status": "succeeded", "size": 100, "etag": "etag",
                            "blob": f"semantic-models/runs/backup/workspaces/{WS}/{MODEL}.abf", "sha256": "a" * 64}],
            }],
        }

    def read_manifest(self, path):
        return copy.deepcopy(self.manifest)

    def verify_model(self, model):
        self.verifications.append(model["id"])

    def journal(self, run_id, sequence, document):
        if self.journal_failure:
            raise OSError("Archive unavailable")
        self.documents.append(copy.deepcopy(document))
        return f"https://example.blob.core.windows.net/backup/{run_id}/{sequence:05d}.json"


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.config, self.api, self.probe, self.archive = config(), FakeApi(), FakeProbe(), FakeArchive()
        self.now = 0
        self.migrator = migration.Migrator(self.config, self.api, self.probe, self.archive, CONTROL,
                                           sleep=self.advance, clock=lambda: self.now)

    def advance(self, seconds):
        self.now += seconds

    def blocked(self):
        with self.assertRaises(migration.SafetyError):
            self.migrator.run()
        self.assertEqual(self.api.mutations, [])
        self.assertEqual(self.migrator.report["status"], "blocked_no_moves")

    def test_dry_run_verifies_backup_and_never_mutates(self):
        report = self.migrator.run()
        self.assertEqual(report["status"], "planned")
        self.assertEqual(self.api.mutations, [])
        self.assertEqual(self.archive.verifications, [MODEL])

    def test_cross_region_import_move_and_post_validation(self):
        self.config["dry_run"] = False
        report = self.migrator.run()
        self.assertEqual(report["status"], "validated_pending_manual_placement_check")
        self.assertTrue(report["workspaces"][0]["validation"]["row_counts_match"])
        self.assertEqual(len(self.api.mutations), 1)
        self.assertTrue(any(d["workspaces"] and d["workspaces"][0]["status"] == "assignment_submission_pending"
                            for d in self.archive.documents))
        self.assertGreaterEqual(len(self.archive.documents), 5)

    def test_large_storage_cross_region_is_blocked(self):
        self.api.model["targetStorageMode"] = "PremiumFiles"
        self.blocked()

    def test_large_storage_same_region_is_allowed(self):
        self.api.model["targetStorageMode"] = "PremiumFiles"
        self.api.target["region"] = "West US 3"
        self.assertEqual(self.migrator.run()["status"], "planned")

    def test_f_to_f_supported(self):
        self.api.source["sku"], self.api.target["sku"] = "F64", "F128"
        self.assertEqual(self.migrator.run()["status"], "planned")

    def test_fabric_and_unknown_items_are_blocked_same_and_cross_region(self):
        for kind in ("Lakehouse", "Warehouse", "Notebook", "DataPipeline", "Dataflow", "FutureItem", "Datamart"):
            for target_region in ("East US", "West US 3"):
                with self.subTest(kind=kind, region=target_region):
                    self.setUp()
                    self.api.target["region"] = target_region
                    self.api.items.append({"id": SOURCE, "displayName": "Unsafe", "type": kind})
                    self.blocked()

    def test_gen1_dataflows_are_blocked_even_if_absent_from_core_inventory(self):
        self.api.dataflows = [{"objectId": SOURCE}]
        self.blocked()

    def test_unknown_or_push_model_is_blocked(self):
        for value in (None, True):
            with self.subTest(value=value):
                self.setUp()
                self.api.model["addRowsAPIEnabled"] = value
                self.blocked()

    def test_unknown_storage_never_falls_back_to_workspace_default(self):
        self.api.model.pop("targetStorageMode")
        self.blocked()

    def test_capacities_must_be_active_and_not_downsized(self):
        for field, value in (("state", "Paused"), ("region", ""), ("sku", "F32"), ("sku", "Trial")):
            with self.subTest(field=field, value=value):
                self.setUp()
                self.api.target[field] = value
                self.blocked()

    def test_pending_and_failed_assignment_block(self):
        for state in ("InProgress", "Failed", None):
            with self.subTest(state=state):
                self.setUp()
                self.api.workspace["capacityAssignmentProgress"] = state
                self.blocked()

    def test_control_workspace_cannot_be_moved(self):
        self.migrator.control_workspace_id = WS
        self.blocked()

    def test_missing_approval_blocks_entire_batch(self):
        self.config["workspaces"][0]["approvals"]["exclusive_write_freeze"] = False
        self.blocked()

    def test_duplicate_workspaces_fail_before_side_effects(self):
        self.config["workspaces"].append(copy.deepcopy(self.config["workspaces"][0]))
        with self.assertRaises(migration.SafetyError):
            self.migrator.run()
        self.assertEqual(self.api.mutations, [])

    def test_failed_missing_stale_backup_blocks(self):
        for modification in ("failed", "stale", "missing", "bad-checksum"):
            with self.subTest(modification=modification):
                self.setUp()
                if modification == "failed":
                    self.archive.manifest["status"] = "failed"
                elif modification == "stale":
                    self.archive.manifest["completed_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
                elif modification == "missing":
                    self.archive.manifest["workspaces"][0]["models"] = []
                else:
                    self.archive.manifest["workspaces"][0]["models"][0]["sha256"] = "not-a-checksum"
                self.blocked()

    def test_model_change_after_backup_blocks(self):
        self.probe.last_change = datetime.now(timezone.utc).isoformat()
        self.blocked()

    def test_unreadable_or_corrupt_archive_blocks(self):
        self.archive.verify_model = Mock(side_effect=OSError("Checksum mismatch"))
        self.blocked()

    def test_missing_archive_blocks_live_run_before_mutation(self):
        self.config["dry_run"] = False
        self.migrator.archive = None
        with self.assertRaises(migration.SafetyError):
            self.migrator.run()
        self.assertEqual(self.api.mutations, [])

    def test_journal_failure_blocks_before_assignment(self):
        self.config["dry_run"] = False
        self.archive.journal_failure = True
        with self.assertRaises(OSError):
            self.migrator.run()
        self.assertEqual(self.api.mutations, [])

    def test_post_move_row_loss_raises_and_does_not_refresh(self):
        self.config.update(dry_run=False, refresh_semantic_models=True)
        self.api.post_assignment_effect = lambda: setattr(self.probe, "rows", 0)
        with self.assertRaisesRegex(migration.SafetyError, "row counts changed"):
            self.migrator.run()
        self.assertEqual(len(self.api.mutations), 1)
        self.assertEqual(self.migrator.report["status"], "failed_or_uncertain")

    def test_report_rebinding_is_validation_failure(self):
        self.config["dry_run"] = False
        self.api.post_assignment_effect = lambda: self.api.reports[0].update(datasetId=SOURCE)
        with self.assertRaisesRegex(migration.SafetyError, "bindings"):
            self.migrator.run()
        self.assertEqual(len(self.api.mutations), 1)

    def test_active_unknown_refresh_blocks(self):
        for state in ("Unknown", "InProgress", "NewStatus"):
            with self.subTest(state=state):
                self.setUp()
                self.api.history = [{"status": state}]
                self.blocked()

    def test_serial_transactional_refresh_tracks_exact_id(self):
        self.config.update(dry_run=False, refresh_semantic_models=True)
        report = self.migrator.run()
        self.assertEqual(len(self.api.mutations), 2)
        body = self.api.mutations[1][2]
        self.assertEqual(body["commitMode"], "transactional")
        self.assertIs(body["applyRefreshPolicy"], False)
        self.assertNotIn("notifyOption", body)
        self.assertEqual(report["workspaces"][0]["refreshes"][0]["refresh_id"], REFRESH)

    def test_failed_refresh_fails_run_and_is_not_retried(self):
        self.config.update(dry_run=False, refresh_semantic_models=True)
        self.api.query_status = "Failed"
        with self.assertRaisesRegex(migration.SafetyError, "failed/cancelled"):
            self.migrator.run()
        self.assertEqual(len(self.api.mutations), 2)
        self.assertEqual(self.migrator.report["status"], "failed_or_uncertain")

    def test_refresh_timeout_is_not_retried(self):
        self.config.update(dry_run=False, refresh_semantic_models=True)
        self.api.query_status = "Unknown"
        with self.assertRaises(TimeoutError):
            self.migrator.run()
        self.assertEqual(len(self.api.mutations), 2)

    def test_assignment_failure_stops_without_refresh_or_rollback(self):
        self.config.update(dry_run=False, refresh_semantic_models=True)
        self.api.progress_after_move = "Failed"
        with self.assertRaisesRegex(migration.SafetyError, "assignment failed"):
            self.migrator.run()
        self.assertEqual(len(self.api.mutations), 1)

    def test_wrong_refresh_selection_is_blocked_pre_move(self):
        self.config.update(dry_run=False, refresh_semantic_models=True)
        self.config["workspaces"][0]["refresh_model_ids"] = [TARGET]
        self.blocked()

    def test_validate_recovery_never_posts(self):
        baseline = self.migrator.preflight(self.config["workspaces"][0])
        self.archive.manifest = {"run_id": "prior", "workspaces": [baseline]}
        self.api.workspace.update(capacityId=TARGET, capacityRegion=self.api.target["region"])
        self.config.update(mode="validate", dry_run=False, validation_journal_blob="baseline.json")
        self.migrator.run()
        self.assertEqual(self.api.mutations, [])

    def test_validation_failure_reports_failed_not_running(self):
        baseline = self.migrator.preflight(self.config["workspaces"][0])
        self.archive.manifest = {"run_id": "prior", "workspaces": [baseline]}
        self.api.workspace.update(capacityId=TARGET, capacityRegion=self.api.target["region"])
        self.probe.rows = 0
        self.config.update(mode="validate", dry_run=False, validation_journal_blob="baseline.json")
        with self.assertRaises(migration.SafetyError):
            self.migrator.run()
        self.assertEqual(self.migrator.report["status"], "failed")
        self.assertEqual(self.api.mutations, [])

    def test_empty_default_configuration_is_read_only_discovery(self):
        self.config["workspaces"] = []
        self.assertEqual(self.migrator.run()["status"], "discovery_only")
        self.assertEqual(self.api.mutations, [])

    def test_all_batch_preflight_before_first_assignment(self):
        self.config["dry_run"] = False
        second = copy.deepcopy(self.config["workspaces"][0])
        second["workspace_id"] = CONTROL
        self.config["workspaces"].append(second)
        self.blocked()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.api = migration.Api(lambda url: "token", self.session, sleep=lambda seconds: None)

    def test_pagination_all_pages_and_continuation_token(self):
        self.session.request.side_effect = [
            Response({"value": [{"id": 1}], "continuationToken": "a b"}),
            Response({"value": [{"id": 2}]}),
        ]
        self.assertEqual(len(self.api.listing(migration.FABRIC + "workspaces")), 2)
        self.assertIn("continuationToken=a+b", self.session.request.call_args.args[1])

    def test_repeated_and_untrusted_continuation_urls_fail(self):
        for url in (migration.FABRIC + "workspaces", "https://evil.example/workspaces"):
            with self.subTest(url=url):
                self.session.request.return_value = Response({"value": [], "continuationUri": url})
                with self.assertRaises(migration.SafetyError):
                    self.api.listing(migration.FABRIC + "workspaces")

    def test_get_retries_but_post_never_retries(self):
        self.session.request.side_effect = [Response(status=429), Response({"value": []})]
        self.api.get(migration.FABRIC + "workspaces")
        self.assertEqual(self.session.request.call_count, 2)
        self.session.reset_mock(side_effect=True)
        self.session.request.return_value = Response(status=429)
        with self.assertRaises(migration.SafetyError):
            self.api.request("POST", migration.FABRIC + "workspaces", {})
        self.assertEqual(self.session.request.call_count, 1)

    def test_redirects_and_wrong_hosts_do_not_receive_tokens(self):
        with self.assertRaises(migration.SafetyError):
            self.api.get("https://api.fabric.microsoft.com.evil.example/v1/")
        self.session.request.assert_not_called()
        self.session.request.return_value = Response(status=302)
        with self.assertRaises(migration.SafetyError):
            self.api.get(migration.FABRIC + "workspaces")
        self.assertFalse(self.session.request.call_args.kwargs["allow_redirects"])


class NotebookTests(unittest.TestCase):
    def test_generated_notebook_is_valid_standalone_and_safe(self):
        build()
        path = Path(__file__).resolve().parents[1] / "WorkspaceCapacityMigration.ipynb"
        notebook = nbformat.read(path, as_version=4)
        nbformat.validate(notebook)
        self.assertEqual(notebook.cells[5].source.rstrip(),
                         path.with_name("workspace_capacity_migration.py").read_text(encoding="utf-8").rstrip())
        config_namespace = {}
        exec(notebook.cells[3].source, config_namespace)
        config = config_namespace["CONFIG"]
        self.assertTrue(config["dry_run"])
        self.assertFalse(config["refresh_semantic_models"])
        self.assertEqual(config["workspaces"], [])
        self.assertIsNone(config["archive"])
        self.assertNotIn("parameters", notebook.cells[3].metadata.get("tags", []))
        example = "\n".join(
            line.replace("# ", "", 1) if line.lstrip().startswith("# ") else line
            for line in notebook.cells[3].source.splitlines()
        )
        example_config = ast.literal_eval(ast.parse(example).body[0].value)
        documented_parameters = list(example_config)
        documented_parameters.extend(f"archive.{key}" for key in example_config["archive"])
        workspace = example_config["workspaces"][0]
        documented_parameters.extend(workspace)
        documented_parameters.extend(workspace["approvals"])
        for parameter in documented_parameters:
            with self.subTest(parameter=parameter):
                self.assertIn(f"| `{parameter}` |", notebook.cells[2].source)
        for index, cell in enumerate(notebook.cells):
            self.assertTrue(cell.source.endswith("\n"))
            if cell.cell_type == "code":
                self.assertEqual(cell.outputs, [])
                self.assertIsNone(cell.execution_count)
                if not cell.source.startswith("%pip"):
                    compile(cell.source, f"cell-{index}", "exec")
        self.assertNotIn("client_secret", path.read_text(encoding="utf-8"))


class BlobVerificationTests(unittest.TestCase):
    def test_real_streaming_verifier_checks_full_checksum_and_conditional_read(self):
        payload = b"native ABF data" * 100
        blob = Mock()
        blob.get_blob_properties.return_value = Mock(size=len(payload), etag="version")
        blob.download_blob.return_value.chunks.return_value = iter([payload[:20], payload[20:]])
        archive = object.__new__(migration.BlobArchive)
        archive.client = Mock(return_value=blob)
        model = {"blob": "backup/model.abf", "size": len(payload),
                 "etag": "version", "sha256": migration.hashlib.sha256(payload).hexdigest()}
        archive.verify_model(model)
        self.assertEqual(blob.download_blob.call_args.kwargs["etag"], "version")
        self.assertIn("match_condition", blob.download_blob.call_args.kwargs)

    def test_real_streaming_verifier_rejects_truncation_and_same_size_corruption(self):
        payload = b"native ABF data"
        for actual in (payload[:-1], b"X" + payload[1:]):
            with self.subTest(actual=actual):
                blob = Mock()
                blob.get_blob_properties.return_value = Mock(size=len(payload), etag="version")
                blob.download_blob.return_value.chunks.return_value = iter([actual])
                archive = object.__new__(migration.BlobArchive)
                archive.client = Mock(return_value=blob)
                with self.assertRaisesRegex(migration.SafetyError, "SHA-256"):
                    archive.verify_model({"blob": "backup/model.abf", "size": len(payload),
                                          "etag": "version", "sha256": migration.hashlib.sha256(payload).hexdigest()})


class ModelProbeTests(unittest.TestCase):
    def model(self, mode="Import", source="MPartitionSource", state="Ready"):
        stamp = SimpleNamespace(ToUniversalTime=lambda: SimpleNamespace(ToString=lambda _: "2026-10-01T00:00:00Z"))
        partition = SimpleNamespace(
            Name="Partition", Mode=mode, State=state, ModifiedTime=stamp, RefreshedTime=stamp,
            Source=SimpleNamespace(GetType=lambda: SimpleNamespace(Name=source)),
        )
        table = SimpleNamespace(
            Name="O'Brien", ModifiedTime=stamp, Partitions=[partition],
            Columns=[SimpleNamespace(Name="ID", DataType="Int64", SourceColumn="ID")], Measures=[],
        )
        database = SimpleNamespace(ID=MODEL, LastSchemaUpdate=stamp, LastProcessed=stamp, StorageEngineUsed="TabularMetadata")
        return SimpleNamespace(Database=database, DefaultMode="Import", ModifiedTime=stamp,
                               Roles=[], Tables=[table], Relationships=[])

    def run_probe(self, model):
        @contextmanager
        def connect(**kwargs):
            self.assertTrue(kwargs["readonly"])
            self.assertEqual(str(kwargs["workspace"]), WS)
            yield SimpleNamespace(model=model)

        frame = SimpleNamespace(shape=(1, 1), iloc={(0, 0): 0})
        fake = SimpleNamespace(connect_semantic_model=connect, evaluate_dax=Mock(return_value=frame))
        with patch.dict(sys.modules, {"sempy": SimpleNamespace(fabric=fake), "sempy.fabric": fake}):
            result = migration.ModelProbe().snapshot(WS, MODEL)
        return result, fake

    def test_probe_reads_all_tables_and_escapes_dax_identifier(self):
        result, fake = self.run_probe(self.model())
        self.assertEqual(result["rows"], {"O'Brien": 0})
        self.assertEqual(len(result["schema_hash"]), 64)
        self.assertIn("O''Brien", fake.evaluate_dax.call_args.kwargs["dax_string"])
        self.assertIn("COALESCE", fake.evaluate_dax.call_args.kwargs["dax_string"])

    def test_directlake_directquery_unknown_and_unready_partitions_block(self):
        for kwargs in ({"mode": "DirectLake"}, {"mode": "DirectQuery"},
                       {"source": "UnknownPartitionSource"}, {"state": "NoData"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(migration.SafetyError):
                    self.run_probe(self.model(**kwargs))

    def test_model_default_directquery_blocks_inherited_partitions(self):
        model = self.model(mode="Default")
        model.DefaultMode = "DirectQuery"
        with self.assertRaises(migration.SafetyError):
            self.run_probe(model)


if __name__ == "__main__":
    unittest.main()
