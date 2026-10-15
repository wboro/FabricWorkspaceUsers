import copy
import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

import semantic_model_backup as smb


SOURCE = "11111111-1111-1111-1111-111111111111"
TARGET = "22222222-2222-2222-2222-222222222222"
MODEL = "33333333-3333-3333-3333-333333333333"
MODEL2 = "44444444-4444-4444-4444-444444444444"
STAGING = "https://stagingacct.blob.core.windows.net"
ARCHIVE = "https://archiveacct.blob.core.windows.net"
DATA = b"native-abf-with-imported-data" * 300
ROOT = Path(__file__).resolve().parents[1]


def configuration():
    return {
        "mode": "backup",
        "archive": {"account_url": ARCHIVE, "container": "backups", "prefix": "models"},
        "staging_account_url": STAGING,
        "backup": {"workspaces": [{"id": SOURCE}], "password": None},
        "restore": {
            "run_id": "", "dry_run": True, "allow_overwrite": False, "password": None,
            "workspaces": [{
                "source_workspace_id": SOURCE, "target_workspace_id": TARGET,
                "model_ids": None, "model_names": {},
            }],
        },
    }


class MemoryStore:
    def __init__(self):
        self.blobs = {}
        self.uploads = []
        self.json_writes = []
        self.counter = 0

    def key(self, location):
        return location["account_url"], location["container"], location["blob"]

    def save(self, location, data):
        self.counter += 1
        self.blobs[self.key(location)] = (data, f'"etag-{self.counter}"')

    def check_container(self, location):
        pass

    def properties(self, location):
        data, etag = self.blobs[self.key(location)]
        return {"size": len(data), "etag": etag}

    def chunks(self, location, etag):
        data, current = self.blobs[self.key(location)]
        if etag != current:
            raise ValueError("ETag mismatch")
        return (data[i:i + 100] for i in range(0, len(data), 100))

    def upload(self, location, chunks, size):
        if self.key(location) in self.blobs:
            raise FileExistsError("Immutable destination already exists")
        self.uploads.append(copy.deepcopy(location))
        data = b"".join(chunks)
        self.save(location, data)

    def put_json(self, location, document):
        if self.key(location) in self.blobs:
            raise FileExistsError("Immutable JSON already exists")
        self.json_writes.append(copy.deepcopy(location))
        self.save(location, json.dumps(document).encode())

    def get_json(self, location):
        return json.loads(self.blobs[self.key(location)][0])


class FakeApi:
    def __init__(self):
        self.items = {
            SOURCE: [{"id": MODEL, "displayName": "Sales <Europe>"}],
            TARGET: [],
        }

    def workspace(self, identifier):
        return {
            "id": identifier, "displayName": "Source" if identifier == SOURCE else "Target",
            "capacityId": "capacity",
        }

    def models(self, identifier):
        return copy.deepcopy(self.items[identifier])


class FakeNative:
    def __init__(self, store, api):
        self.store = store
        self.api = api
        self.backups = []
        self.restores = []
        self.fail_models = set()

    def backup(self, ws, model, filename, password):
        self.backups.append((ws, model, filename, password))
        if model["id"] in self.fail_models:
            raise RuntimeError("Unsupported native model")
        self.store.save({
            "account_url": STAGING, "container": smb.NATIVE_CONTAINER,
            "blob": f"Source/{filename}",
        }, DATA)

    def restore(self, ws, name, filename, password, overwrite):
        self.restores.append((ws, name, filename, password, overwrite))
        self.api.items[ws] = [{"id": MODEL2, "displayName": name}]


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.config = configuration()
        self.store = MemoryStore()
        self.api = FakeApi()
        self.native = FakeNative(self.store, self.api)
        self.secrets = smb.SecretResolver()
        self.engine = smb.BackupRestore(
            self.config, self.api, self.store, self.native, self.secrets,
        )

    def backup(self):
        manifest = self.engine.backup()
        self.config["restore"]["run_id"] = manifest["run_id"]
        return manifest

    def manifest_location(self):
        return self.engine.archive_location(
            f"runs/{self.config['restore']['run_id']}/manifest.json"
        )

    def replace_manifest(self, manifest):
        self.store.save(self.manifest_location(), json.dumps(manifest).encode())

    def test_backup_round_trip_and_dry_run_then_actual_restore(self):
        manifest = self.backup()
        model = manifest["workspaces"][0]["models"][0]
        self.assertEqual(manifest["status"], "succeeded")
        self.assertEqual(model["sha256"], hashlib.sha256(DATA).hexdigest())
        self.assertEqual(model["size"], len(DATA))
        self.assertEqual(len(self.native.backups), 1)
        before = copy.deepcopy(self.store.blobs)
        report = self.engine.restore()
        self.assertEqual(report["status"], "dry_run")
        self.assertEqual(report["models"][0]["name"], "Sales <Europe>")
        self.assertEqual(self.native.restores, [])
        self.assertEqual(before, self.store.blobs)
        self.config["restore"]["dry_run"] = False
        report = self.engine.restore()
        self.assertEqual(report["status"], "succeeded")
        self.assertEqual(report["models"][0]["restored_model_id"], MODEL2)
        self.assertFalse(self.native.restores[0][-1])
        self.assertTrue(self.store.json_writes[-1]["blob"].endswith("/report.json"))

    def test_failures_are_recorded_and_other_models_attempted(self):
        self.api.items[SOURCE].append({"id": MODEL2, "displayName": "Unsupported"})
        self.native.fail_models.add(MODEL)
        output = io.StringIO()
        with redirect_stdout(output), self.assertLogs(smb.LOG, level="ERROR"), \
                self.assertRaises(RuntimeError) as error:
            self.engine.backup()
        self.assertEqual(len(self.native.backups), 2)
        manifest = self.store.get_json(self.store.json_writes[-1])
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["workspaces"][0]["models"][0]["status"], "failed")
        self.assertEqual(manifest["workspaces"][0]["models"][1]["status"], "succeeded")
        self.assertEqual(manifest["summary"]["models_backed_up"], 1)
        self.assertEqual(manifest["summary"]["models_failed"], 1)
        self.assertEqual(manifest["summary"]["models_attempted"], 2)
        self.assertIn("1 semantic models backed up, 1 failed (2 attempted)", str(error.exception))
        self.assertIn("1 semantic models backed up, 1 failed (2 attempted)", output.getvalue())
        self.assertIn(f"/runs/{manifest['run_id']}/manifest.json", str(error.exception))
        self.config["restore"]["run_id"] = manifest["run_id"]
        with self.assertRaisesRegex(ValueError, "incomplete/failed"):
            self.engine.restore()

    def test_workspace_discovery_failure_does_not_stop_other_workspaces(self):
        self.config["backup"]["workspaces"].append({"id": TARGET})
        self.api.workspace = Mock(side_effect=[RuntimeError("No access"), {
            "id": TARGET, "displayName": "Target", "capacityId": "capacity",
        }])
        with self.assertLogs(smb.LOG, level="ERROR"), self.assertRaises(RuntimeError):
            self.engine.backup()
        manifest = self.store.get_json(self.store.json_writes[-1])
        self.assertEqual([w["status"] for w in manifest["workspaces"]], ["failed", "succeeded"])
        self.assertEqual(manifest["summary"]["workspaces_without_model_inventory"], 1)
        self.assertEqual(manifest["summary"]["models_attempted"], 0)
        self.assertEqual(manifest["summary"]["workspaces_failed"], 1)
        self.assertEqual(manifest["summary"]["workspaces_succeeded"], 1)
        self.assertEqual(manifest["summary"]["workspaces_not_attempted"], 0)

    def test_successful_backup_prints_and_persists_summary(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = self.backup()
        self.assertEqual(result["summary"], {
            "models_backed_up": 1, "models_failed": 0, "models_attempted": 1,
            "workspaces_total": 1, "workspaces_succeeded": 1, "workspaces_failed": 0,
            "workspaces_not_attempted": 0, "workspaces_without_model_inventory": 0,
            "workspace_cleanup_failures": 0,
        })
        self.assertEqual(self.store.get_json(self.manifest_location())["summary"], result["summary"])
        self.assertIn("1 semantic models backed up, 0 failed (1 attempted)", output.getvalue())

    def test_all_models_failed_are_reported_with_zero_successes(self):
        self.native.fail_models.add(MODEL)
        with self.assertLogs(smb.LOG, level="ERROR"), self.assertRaisesRegex(
            RuntimeError, "0 semantic models backed up, 1 failed \\(1 attempted\\)",
        ):
            self.engine.backup()
        summary = self.store.get_json(self.store.json_writes[-1])["summary"]
        self.assertEqual(summary["models_backed_up"], 0)
        self.assertEqual(summary["models_failed"], 1)

    def test_archive_verification_failure_is_not_counted_as_backed_up(self):
        with patch.object(smb, "verified_copy", side_effect=ValueError("SHA-256 mismatch")), \
                self.assertLogs(smb.LOG, level="ERROR"), self.assertRaises(RuntimeError):
            self.engine.backup()
        self.assertEqual(len(self.native.backups), 1)
        summary = self.store.get_json(self.store.json_writes[-1])["summary"]
        self.assertEqual(summary["models_backed_up"], 0)
        self.assertEqual(summary["models_failed"], 1)

    def test_duplicate_workspaces_fail_before_writes(self):
        self.config["backup"]["workspaces"].append({"id": SOURCE})
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.engine.backup()
        self.assertEqual(self.store.blobs, {})

    def test_empty_workspace_is_recorded(self):
        self.api.items[SOURCE] = []
        result = self.backup()
        self.assertEqual(result["workspaces"][0]["models"], [])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["summary"]["models_attempted"], 0)
        self.assertEqual(result["summary"]["workspaces_succeeded"], 1)
        self.assertEqual(result["summary"]["workspaces_without_model_inventory"], 0)
        with self.assertRaisesRegex(ValueError, "nonempty"):
            self.engine.restore()

    def test_name_conflicts_and_explicit_overwrite(self):
        self.backup()
        self.api.items[TARGET] = [{"id": MODEL2, "displayName": "sales <europe>"}]
        with self.assertRaisesRegex(ValueError, "overwriting is disabled"):
            self.engine.restore()
        self.config["restore"].update(dry_run=False, allow_overwrite=True)
        self.engine.restore()
        self.assertTrue(self.native.restores[0][-1])

    def test_rename_selected_model(self):
        self.backup()
        spec = self.config["restore"]["workspaces"][0]
        spec.update(model_ids=[MODEL], model_names={MODEL: "Restored sales"})
        report = self.engine.restore()
        self.assertEqual(report["models"][0]["name"], "Restored sales")

    def test_invalid_selection_never_writes(self):
        self.backup()
        spec = self.config["restore"]["workspaces"][0]
        for selection in ([], [MODEL, MODEL], [MODEL2]):
            with self.subTest(selection=selection):
                spec["model_ids"] = selection
                with self.assertRaises((ValueError, KeyError)):
                    self.engine.restore()
        self.assertEqual(self.native.restores, [])

    def test_unknown_rename_rejected(self):
        self.backup()
        self.config["restore"]["workspaces"][0]["model_names"] = {MODEL2: "Unexpected"}
        with self.assertRaisesRegex(ValueError, "outside"):
            self.engine.restore()

    def test_duplicate_restore_targets_rejected_before_first_write(self):
        self.backup()
        self.config["restore"]["dry_run"] = False
        self.config["restore"]["workspaces"] *= 2
        uploads = len(self.store.uploads)
        with self.assertRaisesRegex(ValueError, "same target"):
            self.engine.restore()
        self.assertEqual(len(self.store.uploads), uploads)
        self.assertEqual(self.native.restores, [])

    def test_archive_replaced_after_backup_rejected(self):
        manifest = self.backup()
        model = manifest["workspaces"][0]["models"][0]
        self.store.save({**self.config["archive"], "blob": model["blob"]}, DATA)
        with self.assertRaisesRegex(ValueError, "size/ETag"):
            self.engine.restore()
        self.assertEqual(self.native.restores, [])

    def test_corruption_with_unchanged_etag_is_detected_before_xmla(self):
        manifest = self.backup()
        model = manifest["workspaces"][0]["models"][0]
        location = {**self.config["archive"], "blob": model["blob"]}
        key = self.store.key(location)
        old_data, etag = self.store.blobs[key]
        self.store.blobs[key] = (b"X" + old_data[1:], etag)
        self.config["restore"]["dry_run"] = False
        with self.assertLogs(smb.LOG, level="ERROR"), self.assertRaisesRegex(RuntimeError, "failures"):
            self.engine.restore()
        self.assertEqual(self.native.restores, [])
        report = self.store.get_json(self.store.json_writes[-1])
        self.assertIn("SHA-256 mismatch", report["models"][0]["error"])

    def test_path_in_manifest_cannot_escape_run(self):
        manifest = self.backup()
        manifest["workspaces"][0]["models"][0]["blob"] = "someone-elses-backup.abf"
        self.replace_manifest(manifest)
        with self.assertRaisesRegex(ValueError, "invalid archive blob path"):
            self.engine.restore()

    def test_manifest_schema_and_run_id_checked(self):
        manifest = self.backup()
        for key, value in (("schema_version", 2), ("run_id", "different"), ("status", "running")):
            changed = copy.deepcopy(manifest)
            changed[key] = value
            self.replace_manifest(changed)
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "manifest"):
                self.engine.restore()

    def test_password_required_and_secret_not_in_manifest(self):
        self.config["backup"]["password"] = 'p&<a>"\\s'
        manifest = self.backup()
        self.assertNotIn('p&<a>', json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "requires restore.password"):
            self.engine.restore()
        self.config["restore"].update(password='p&<a>"\\s', dry_run=False)
        self.engine.restore()
        self.assertEqual(self.native.restores[0][-2], 'p&<a>"\\s')

    def test_target_staging_override(self):
        self.backup()
        self.config["restore"]["workspaces"][0].update(
            staging_account_url="https://targetacct.blob.core.windows.net",
            staging_folder="Exact native folder",
        )
        self.config["restore"]["dry_run"] = False
        self.engine.restore()
        destination = self.store.uploads[-1]
        self.assertEqual(destination["account_url"], "https://targetacct.blob.core.windows.net")
        self.assertTrue(destination["blob"].startswith("Exact native folder/restore-"))

    def test_failed_run_allows_other_fully_successful_workspace(self):
        manifest = self.backup()
        manifest["status"] = "failed"
        manifest["workspaces"].append({"id": MODEL2, "status": "failed", "models": []})
        self.replace_manifest(manifest)
        self.assertEqual(self.engine.restore()["status"], "dry_run")

    def test_restore_flags_require_actual_booleans(self):
        self.backup()
        for flag in ("dry_run", "allow_overwrite"):
            self.config["restore"][flag] = "False"
            with self.assertRaisesRegex(ValueError, "boolean"):
                self.engine.restore()
            self.config["restore"][flag] = False

    def test_xmla_failure_is_persisted_no_success_fallback(self):
        self.backup()
        self.config["restore"]["dry_run"] = False
        self.native.restore = Mock(side_effect=RuntimeError("Not enough memory"))
        with self.assertLogs(smb.LOG, level="ERROR"), self.assertRaisesRegex(RuntimeError, "failures"):
            self.engine.restore()
        report = self.store.get_json(self.store.json_writes[-1])
        self.assertEqual(report["status"], "failed")
        self.assertIn("Not enough memory", report["models"][0]["error"])

    def test_restored_item_must_be_discovered(self):
        self.backup()
        self.config["restore"]["dry_run"] = False
        self.native.restore = Mock()
        with self.assertLogs(smb.LOG, level="ERROR"), self.assertRaises(RuntimeError):
            self.engine.restore()
        self.assertEqual(self.native.restore.call_count, 1)

    def test_final_manifest_failure_is_not_success(self):
        original = self.store.put_json

        def put(location, document):
            if location["blob"].endswith("manifest.json"):
                raise OSError("Storage unavailable")
            original(location, document)

        self.store.put_json = put
        with self.assertRaisesRegex(OSError, "Storage unavailable"):
            self.engine.backup()


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.source = {"account_url": STAGING, "container": "backups", "blob": "source"}
        self.destination = {**self.source, "blob": "destination"}
        self.store.save(self.source, DATA)

    def test_destination_corruption_detected(self):
        original = self.store.upload

        def upload(location, chunks, size):
            original(location, chunks, size)
            self.store.save(location, b"X" * size)

        self.store.upload = upload
        with self.assertRaisesRegex(ValueError, "verification failed"):
            smb.verified_copy(self.store, self.source, self.destination)

    def test_empty_backup_rejected(self):
        self.store.save(self.source, b"")
        with self.assertRaisesRegex(ValueError, "empty"):
            smb.verified_copy(self.store, self.source, self.destination)
        self.assertEqual(self.store.uploads, [])

    def test_truncated_source_rejected(self):
        original = self.store.chunks
        self.store.chunks = lambda loc, etag: iter([next(iter(original(loc, etag)))])
        with self.assertRaisesRegex(ValueError, "truncated"):
            smb.verified_copy(self.store, self.source, self.destination)

    def test_destination_never_overwritten(self):
        self.store.save(self.destination, b"original")
        with self.assertRaises(FileExistsError):
            smb.verified_copy(self.store, self.source, self.destination)
        self.assertEqual(self.store.blobs[self.store.key(self.destination)][0], b"original")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.credential = Mock()
        self.credential.get_token.return_value.token = "not-a-real-token"
        self.session = Mock()
        self.sleep = Mock()
        self.api = smb.FabricApi(self.credential, self.session, self.sleep)

    def response(self, status=200, body=None, headers=None):
        response = Mock(status_code=status, headers=headers or {})
        response.json.return_value = body
        return response

    def test_all_pages_and_explicit_audience(self):
        next_page = smb.FABRIC_API + f"workspaces/{SOURCE}/items?continuationToken=next"
        self.session.get.side_effect = [
            self.response(body={"value": [{"id": MODEL}], "continuationUri": next_page}),
            self.response(body={"value": [{"id": MODEL2}]}),
        ]
        self.assertEqual(len(self.api.models(SOURCE)), 2)
        self.credential.get_token.assert_called_with("https://api.fabric.microsoft.com/.default")
        self.assertFalse(self.session.get.call_args.kwargs["allow_redirects"])

    def test_token_only_pagination_and_throttling(self):
        self.session.get.side_effect = [
            self.response(429, headers={"Retry-After": "3"}),
            self.response(body={"value": [{"id": MODEL}], "continuationToken": "a+/="}),
            self.response(body={"value": [{"id": MODEL2}]}),
        ]
        self.api.models(SOURCE)
        self.sleep.assert_called_once_with(3)
        self.assertIn("continuationToken=a%2B%2F%3D", self.session.get.call_args.args[0])

    def test_external_pagination_url_never_receives_token(self):
        self.session.get.return_value = self.response(body={
            "value": [], "continuationUri": "https://evil.example/token",
        })
        with self.assertRaisesRegex(ValueError, "untrusted"):
            self.api.models(SOURCE)
        self.assertEqual(self.session.get.call_count, 1)

    def test_pagination_loop_detected(self):
        path = smb.FABRIC_API + "loop"
        self.session.get.return_value = self.response(body={"value": [], "continuationUri": path})
        with self.assertRaisesRegex(RuntimeError, "repeated"):
            self.api.models(SOURCE)

    def test_retry_limit_and_permission_error_no_retry(self):
        self.session.get.return_value = self.response(503)
        with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
            self.api.get("workspaces")
        self.assertEqual(self.session.get.call_count, 6)
        self.session.reset_mock()
        self.session.get.return_value = self.response(403)
        with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
            self.api.get("workspaces")
        self.assertEqual(self.session.get.call_count, 1)

    def test_capacity_required(self):
        self.session.get.return_value = self.response(body={"id": SOURCE})
        with self.assertRaisesRegex(ValueError, "capacity"):
            self.api.workspace(SOURCE)

    def test_redirect_is_error(self):
        self.session.get.return_value = self.response(302, headers={"Location": "https://evil.example"})
        with self.assertRaisesRegex(RuntimeError, "HTTP 302"):
            self.api.get("workspaces")


class NativeAndConfigTests(unittest.TestCase):
    def test_native_commands_explicit_credentials_and_escaped_password(self):
        sempy = types.ModuleType("sempy")
        fabric = types.ModuleType("sempy.fabric")
        fabric.execute_xmla = Mock()
        fabric.execute_tmsl = Mock()
        sempy.fabric = fabric
        credential = object()
        native = smb.NativeXmla(credential)
        with patch.dict(sys.modules, {"sempy": sempy, "sempy.fabric": fabric}):
            native.backup(SOURCE, {"id": MODEL}, "backup.abf", "p&<word>")
            xmla = fabric.execute_xmla.call_args.kwargs
            root = ET.fromstring(xmla["xmla_command"])
            ns = {"a": "http://schemas.microsoft.com/analysisservices/2003/engine"}
            self.assertEqual(root.find("a:Object/a:DatabaseID", ns).text, MODEL)
            self.assertEqual(root.find("a:Password", ns).text, "p&<word>")
            self.assertEqual(root.find("a:AllowOverwrite", ns).text, "false")
            self.assertIs(xmla["credential"], credential)
            self.assertTrue(xmla["use_readwrite_connection"])
            native.restore(TARGET, "Restored", "restore.abf", "pw", False)
            tmsl = fabric.execute_tmsl.call_args.kwargs
            self.assertEqual(tmsl["script"]["restore"]["security"], "skipMembership")
            self.assertFalse(tmsl["script"]["restore"]["allowOverwrite"])
            self.assertIs(tmsl["credential"], credential)

    def test_secret_resolution_and_error_redaction(self):
        get_secret = Mock(return_value='p&<x>"\\s')
        resolver = smb.SecretResolver(get_secret)
        secret = resolver.resolve({
            "key_vault_url": "https://vaultabc.vault.azure.net/", "secret_name": "application",
        }, "password")
        get_secret.assert_called_once_with("https://vaultabc.vault.azure.net/", "application")
        for representation in (secret, json.dumps(secret)[1:-1], secret.replace("&", "&amp;").replace("<", "&lt;")):
            self.assertNotIn(representation, resolver.error(ValueError(representation)))
        self.assertNotIn("tokenvalue", resolver.error(ValueError("Bearer tokenvalue")))

    def test_invalid_urls_and_paths(self):
        for value in (
            "http://storage.blob.core.windows.net", ARCHIVE + "?sig=secret",
            "https://archiveacct.blob.core.windows.net.evil.example",
            ARCHIVE + "/container", "https://<account>.blob.core.windows.net",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                smb.account_url(value)
        for value in ("../backup", "/absolute", "a//b", "a\\b", "a/./b"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                smb.blob_path(value, "path")

    def test_keyvault_references_fail_without_notebook_bootstrap(self):
        with self.assertRaisesRegex(ValueError, "NotebookUtils"):
            smb.SecretResolver().resolve({
                "key_vault_url": "https://vaultabc.vault.azure.net/", "secret_name": "secret",
            }, "secret")

    def test_unsafe_keyvault_url_rejected(self):
        get_secret = Mock()
        with self.assertRaisesRegex(ValueError, "Key Vault URL"):
            smb.SecretResolver(get_secret).resolve({
                "key_vault_url": "https://evil.example/", "secret_name": "secret",
            }, "secret")
        get_secret.assert_not_called()

    def test_notebook_matches_sources_and_has_no_outputs(self):
        notebook = json.loads((ROOT / "SemanticModelBackup.ipynb").read_text(encoding="utf-8"))
        self.assertEqual(notebook["nbformat"], 4)
        cells = {cell["id"]: cell for cell in notebook["cells"]}
        for identifier, filename in (
            ("implementation", "semantic_model_backup.py"), ("configuration", "config.example.py"),
        ):
            source = "".join(cells[identifier]["source"])
            self.assertEqual(source, (ROOT / filename).read_text(encoding="utf-8"))
            compile(source, filename, "exec")
        self.assertEqual(cells["configuration"]["metadata"]["tags"], ["parameters"])
        dependencies = "".join(cells["dependencies"]["source"])
        for requirement in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
            self.assertIn(f'"{requirement}"', dependencies)
        for cell in notebook["cells"]:
            if cell["cell_type"] == "code":
                self.assertEqual(cell["outputs"], [])
                self.assertIsNone(cell["execution_count"])
                source = "".join(cell["source"])
                if not source.startswith("%"):
                    compile(source, cell["id"], "exec")


if __name__ == "__main__":
    unittest.main()
