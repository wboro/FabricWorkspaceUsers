"""Offline download, workflow, SDK and generated-notebook regression tests."""

import copy
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import semantic_model_backup_download as smb


WORKSPACE = "11111111-1111-1111-1111-111111111111"
MODEL = "22222222-2222-2222-2222-222222222222"
REPORT = "33333333-3333-3333-3333-333333333333"
OTHER_REPORT = "44444444-4444-4444-4444-444444444444"
CONFIG = {
    "archive": {
        "account_url": "https://archiveacct.blob.core.windows.net",
        "container": "backups", "prefix": "models",
    },
    "workspaces": [WORKSPACE], "max_download_bytes": 100000,
}


def pbix(model=True):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Version", "1.0")
        if model:
            archive.writestr("DataModel", b"offline-test-model" * 100)
    return buffer.getvalue()


class Response:
    def __init__(self, status=200, data=b"", page=None, headers=None, url=None):
        self.status_code = status
        self.data = data
        self.page = page
        self.headers = headers or {}
        self.url = url or smb.POWERBI_API
        self.closed = False

    def json(self):
        return self.page

    def iter_content(self, chunk_size):
        for start in range(0, len(self.data), chunk_size):
            yield self.data[start:start + chunk_size]

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.credential = Mock()
        self.credential.get_token.return_value = SimpleNamespace(token="test-token")
        self.session = Mock()
        self.api = smb.PowerBiApi(self.credential, self.session, sleep=Mock())
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.pbix"

    def test_download_requests_data_and_validates_content(self):
        data = pbix()
        response = Response(data=data, headers={"Content-Length": str(len(data))})
        self.session.get.return_value = response
        info = self.api.download(WORKSPACE, REPORT, self.path, 100000)
        self.assertEqual(info, {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        self.assertEqual(self.path.read_bytes(), data)
        args, kwargs = self.session.get.call_args
        self.assertIn("downloadType=IncludeModel", args[0])
        self.assertNotIn("LiveConnect", args[0])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-token")
        self.assertTrue(response.closed)

    def test_rejects_live_connected_and_invalid_files(self):
        for data in (pbix(False), b"<html>error</html>", b""):
            with self.subTest(data=data[:20]):
                self.session.get.return_value = Response(data=data)
                with self.assertRaises(smb.BackupError):
                    self.api.download(WORKSPACE, REPORT, self.path, 100000)

    def test_rejects_corrupt_zip(self):
        data = bytearray(pbix())
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            item = package.getinfo("DataModel")
            offset = item.header_offset + 30 + len(item.filename) + len(item.extra)
        data[offset + 3] ^= 0xFF
        self.path.write_bytes(data)
        with self.assertRaises((smb.BackupError, zipfile.BadZipFile)):
            smb.validate_pbix(self.path)

    def test_declared_and_streaming_size_limits_and_truncation(self):
        cases = [
            Response(data=pbix(), headers={"Content-Length": "100001"}),
            Response(data=pbix(), headers={"Content-Length": "bad"}),
            Response(data=pbix(), headers={"Content-Length": "999"}),
            Response(data=b"x" * 100001),
        ]
        for response in cases:
            with self.subTest(headers=response.headers):
                self.session.get.return_value = response
                with self.assertRaises(smb.BackupError):
                    self.api.download(WORKSPACE, REPORT, self.path, 100000)
                self.assertTrue(response.closed)

    def test_signed_blob_redirect_never_receives_bearer(self):
        target = "https://temporaryacct.blob.core.windows.net/exports/model.pbix?sig=secret"
        self.session.get.side_effect = [
            Response(302, headers={"Location": target}),
            Response(data=pbix()),
        ]
        self.api.download(WORKSPACE, REPORT, self.path, 100000)
        second = self.session.get.call_args_list[1]
        self.assertEqual(second.args[0], target)
        self.assertEqual(second.kwargs["headers"], {})
        self.assertFalse(second.kwargs["allow_redirects"])

    def export_url(self, host="api.powerbi.com", query="downloadType=IncludeModel"):
        return (
            f"https://{host}/v1.0/myorg/groups/{WORKSPACE}/reports/{REPORT}/Export?{query}"
        )

    def test_regional_client_routing_redirect_preserves_powerbi_authentication(self):
        target = self.export_url("wabi-west-us3-a-primary-redirect.analysis.windows.net")
        self.session.get.side_effect = [
            Response(307, headers={"Location": target}),
            Response(data=pbix()),
        ]
        self.api.download(WORKSPACE, REPORT, self.path, 100000)
        routed = self.session.get.call_args_list[1]
        self.assertEqual(routed.args[0], target)
        self.assertEqual(routed.kwargs["headers"]["Authorization"], "Bearer test-token")
        self.assertFalse(routed.kwargs["allow_redirects"])

    def test_regional_route_then_signed_blob_does_not_leak_bearer(self):
        route = self.export_url("wabi-west-us3-a-primary-redirect.analysis.windows.net")
        blob = "https://temporaryacct.blob.core.windows.net/file.pbix?sig=secret"
        self.session.get.side_effect = [
            Response(307, headers={"Location": route}),
            Response(302, headers={"Location": blob}),
            Response(data=pbix()),
        ]
        self.api.download(WORKSPACE, REPORT, self.path, 100000)
        calls = self.session.get.call_args_list
        self.assertIn("Authorization", calls[1].kwargs["headers"])
        self.assertEqual(calls[2].args[0], blob)
        self.assertEqual(calls[2].kwargs["headers"], {})
        self.assertEqual(self.credential.get_token.call_count, 2)

    def test_routed_requests_retry_transient_errors(self):
        route = self.export_url("wabi-west-us3-a-primary-redirect.analysis.windows.net")
        transient = Response(429, headers={"Retry-After": "2"})
        self.session.get.side_effect = [
            Response(307, headers={"Location": route}),
            transient,
            Response(data=pbix()),
        ]
        self.api.download(WORKSPACE, REPORT, self.path, 100000)
        self.assertTrue(transient.closed)
        self.api.sleep.assert_called_once_with(2)
        self.assertEqual(self.session.get.call_count, 3)

    def test_relative_same_service_export_redirect_is_authenticated(self):
        self.session.get.side_effect = [
            Response(307, headers={
                "Location": "?downloadType=IncludeModel&preferClientRouting=true&route=1",
            }),
            Response(data=pbix()),
        ]
        self.api.download(WORKSPACE, REPORT, self.path, 100000)
        self.assertEqual(
            self.session.get.call_args_list[1].args[0],
            self.export_url(query="downloadType=IncludeModel&preferClientRouting=true&route=1"),
        )
        self.assertIn("Authorization", self.session.get.call_args.kwargs["headers"])

    def test_rejects_regional_redirects_to_another_resource_or_live_connection(self):
        host = "wabi-west-us3-a-primary-redirect.analysis.windows.net"
        targets = [
            self.export_url(host + ".attacker.example"),
            self.export_url("attacker.analysis.windows.net"),
            self.export_url("wabi-cluster.analysis.windows.net:443"),
            self.export_url(host).replace(REPORT, OTHER_REPORT),
            self.export_url(host, "downloadType=LiveConnect"),
            self.export_url(host, "preferClientRouting=true"),
            self.export_url(host, "downloadType=IncludeModel&downloadType=LiveConnect"),
            self.export_url(host) + "#fragment",
        ]
        for target in targets:
            with self.subTest(target=target):
                self.session.get.reset_mock()
                self.session.get.side_effect = None
                response = Response(307, headers={"Location": target})
                self.session.get.return_value = response
                with self.assertRaises(smb.BackupError):
                    self.api.download(WORKSPACE, REPORT, self.path, 100000)
                self.assertEqual(self.session.get.call_count, 1)
                self.assertTrue(response.closed)

    def test_blob_redirect_cannot_restore_powerbi_authentication(self):
        blob = "https://temporaryacct.blob.core.windows.net/file.pbix?sig=secret"
        self.session.get.side_effect = [
            Response(302, headers={"Location": blob}),
            Response(307, headers={"Location": self.export_url()}),
        ]
        with self.assertRaises(smb.BackupError):
            self.api.download(WORKSPACE, REPORT, self.path, 100000)
        self.assertEqual(self.session.get.call_count, 2)
        self.assertEqual(self.credential.get_token.call_count, 1)

    def test_redirect_cycles_are_detected_before_another_request(self):
        route = self.export_url("wabi-west-us3-a-primary-redirect.analysis.windows.net")
        self.session.get.side_effect = [
            Response(307, headers={"Location": route}),
            Response(307, headers={"Location": route}),
        ]
        with self.assertRaisesRegex(smb.BackupError, "repeated"):
            self.api.download(WORKSPACE, REPORT, self.path, 100000)
        self.assertEqual(self.session.get.call_count, 2)

    def test_redirect_limit_is_explicit_and_bounded(self):
        host = "wabi-west-us3-a-primary-redirect.analysis.windows.net"
        responses = [
            Response(307, headers={"Location": self.export_url(
                host, f"downloadType=IncludeModel&hop={hop}",
            )})
            for hop in range(6)
        ]
        self.session.get.side_effect = responses
        with self.assertRaisesRegex(smb.BackupError, "redirect limit"):
            self.api.download(WORKSPACE, REPORT, self.path, 100000)
        self.assertEqual(self.session.get.call_count, 6)
        self.assertTrue(all(r.closed for r in responses))

    def test_rejects_untrusted_redirects_and_pagination(self):
        targets = [
            "http://temporaryacct.blob.core.windows.net/file?sig=x",
            "https://attacker.example/file?sig=x",
            "https://acct.blob.core.windows.net.attacker.example/file?sig=x",
            "https://user@acct.blob.core.windows.net/file?sig=x",
            "https://acct.blob.core.windows.net/file",
            "https://acct.blob.core.windows.net/file?unrelated=value",
            "https://acct.blob.core.windows.net/file?sig=",
        ]
        for target in targets:
            with self.subTest(target=target):
                self.session.get.reset_mock()
                self.session.get.return_value = Response(302, headers={"Location": target})
                with self.assertRaises(smb.BackupError):
                    self.api.download(WORKSPACE, REPORT, self.path, 100000)
                self.assertEqual(self.session.get.call_count, 1)
        self.session.get.reset_mock()
        self.session.get.return_value = Response(
            page={"value": [], "@odata.nextLink": "https://attacker.example/items"},
        )
        with self.assertRaises(smb.BackupError):
            self.api.values("groups")
        self.assertEqual(self.session.get.call_count, 1)

    def test_http_errors_are_safe_and_response_closed(self):
        response = Response(403, data=b"sensitive upstream payload")
        self.session.get.return_value = response
        with self.assertRaisesRegex(smb.BackupError, "HTTP 403"):
            self.api.download(WORKSPACE, REPORT, self.path, 100000)
        self.assertTrue(response.closed)

    def test_retry_is_bounded_and_closes_responses(self):
        responses = [Response(429, headers={"Retry-After": "999"}) for _ in range(5)]
        responses.append(Response(page={"value": []}))
        self.session.get.side_effect = responses
        self.assertEqual(self.api.values("groups"), [])
        self.assertEqual(self.session.get.call_count, 6)
        self.assertTrue(all(r.closed for r in responses))
        self.api.sleep.assert_called_with(300)

    def test_fabric_token_pagination_and_duplicates(self):
        api = smb.Api(self.credential, smb.FABRIC_API, "fabric-scope", self.session)
        self.session.get.side_effect = [
            Response(page={"value": [{"id": MODEL}], "continuationToken": "a+b"}),
            Response(page={"value": [{"id": REPORT}]}),
        ]
        result = api.values(f"workspaces/{WORKSPACE}/items?type=SemanticModel", fabric=True)
        self.assertEqual(len(result), 2)
        self.assertIn("continuationToken=a%2Bb", self.session.get.call_args.args[0])
        self.credential.get_token.assert_called_with("fabric-scope")
        next_link = smb.FABRIC_API + "next"
        self.session.get.side_effect = [
            Response(page={"value": [{"id": MODEL}], "continuationUri": next_link}),
            Response(page={"value": [{"id": MODEL}]}),
        ]
        with self.assertRaisesRegex(smb.BackupError, "duplicate"):
            api.values("items", fabric=True)

    def test_repeated_pagination_link_fails(self):
        self.session.get.return_value = Response(page={
            "value": [], "@odata.nextLink": smb.POWERBI_API + "groups",
        })
        with self.assertRaisesRegex(smb.BackupError, "repeated"):
            self.api.values("groups")


class MemoryArchive:
    def __init__(self):
        self.records = {}
        self.uploads = []

    def preflight(self):
        pass

    def json(self, name, value):
        if name in self.records:
            raise AssertionError("Overwriting an immutable manifest")
        self.records[name] = copy.deepcopy(value)

    def upload(self, name, path, expected):
        self.uploads.append((name, path))
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected["sha256"]
        return {**expected, "blob": name, "etag": '"etag"'}

    def manifest(self):
        return next(v for k, v in self.records.items() if k.endswith("/manifest.json"))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        capture = redirect_stdout(self.output)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)
        self.fabric = Mock()
        self.fabric.values.return_value = [{"id": MODEL, "displayName": "Model"}]
        self.powerbi = Mock()
        self.powerbi.values.return_value = [
            {"id": REPORT, "datasetId": MODEL, "reportType": "PowerBIReport"},
        ]
        self.powerbi.download.side_effect = self.download
        self.archive = MemoryArchive()

    @staticmethod
    def download(workspace, report, path, limit):
        data = pbix()
        path.write_bytes(data)
        return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def execute(self, config=None):
        return smb.backup(config or CONFIG, self.fabric, self.powerbi, self.archive)

    def test_success_records_verified_archive_and_removes_temp_file(self):
        manifest = self.execute()
        self.assertEqual(manifest["summary"]["models_backed_up"], 1)
        self.assertEqual(manifest["status"], "succeeded")
        self.assertEqual(manifest, self.archive.manifest())
        name, path = self.archive.uploads[0]
        self.assertTrue(name.endswith(f"/{WORKSPACE}/{MODEL}.pbix"))
        self.assertFalse(path.exists())
        self.assertEqual(len(self.archive.records), 2)
        output = self.output.getvalue()
        self.assertEqual(output.count("SEMANTIC MODEL BACKUP"), 1)
        self.assertIn("COMPLETE", output)
        self.assertIn("BACKED UP", output)
        self.assertIn("SHA-256 verified", output)
        self.assertIn(CONFIG["archive"]["account_url"], output)
        self.assertNotIn('"models_discovered"', output)

    def test_no_report_is_a_failure_not_an_omission(self):
        self.powerbi.values.return_value = []
        with self.assertRaisesRegex(smb.BackupError, "Backup incomplete") as caught:
            self.execute()
        manifest = self.archive.manifest()
        self.assertEqual(manifest["summary"]["models_failed"], 1)
        self.assertIn("No associated", manifest["workspaces"][0]["models"][0]["error"])
        self.assertIn("No associated Power BI report", self.output.getvalue())
        self.assertIn("INCOMPLETE", self.output.getvalue())
        self.assertIn("Manifest:", str(caught.exception))
        self.assertNotIn("Failure details:", str(caught.exception))
        self.assertNotIn('"models_discovered"', str(caught.exception))
        self.assertLess(len(str(caught.exception)), 500)
        self.powerbi.download.assert_not_called()

    def test_formatted_report_includes_actual_report_failure_reason(self):
        self.powerbi.download.side_effect = smb.BackupError("PBIX export is unavailable.")
        with self.assertRaisesRegex(smb.BackupError, "Backup incomplete"):
            self.execute()
        self.assertIn("PBIX export is unavailable.", self.output.getvalue())
        self.assertEqual(self.output.getvalue().count("PBIX export is unavailable."), 1)
        self.assertIn(
            "PBIX export is unavailable.",
            self.archive.manifest()["workspaces"][0]["models"][0]["report_failures"][0]["error"],
        )

    def test_paginated_and_unrelated_reports_are_not_used(self):
        self.powerbi.values.return_value = [
            {"id": REPORT, "datasetId": MODEL, "reportType": "PaginatedReport"},
            {"id": OTHER_REPORT, "datasetId": REPORT, "reportType": "PowerBIReport"},
            {"id": WORKSPACE, "datasetId": None, "reportType": "PaginatedReport"},
        ]
        with self.assertRaises(smb.BackupError):
            self.execute()
        self.powerbi.download.assert_not_called()

    def test_another_report_can_succeed_after_export_failure(self):
        self.powerbi.values.return_value.append({"id": OTHER_REPORT, "datasetId": MODEL})

        def download(workspace, report, path, limit):
            if report == REPORT:
                raise smb.BackupError("PBIX export is unavailable.")
            return self.download(workspace, report, path, limit)

        self.powerbi.download.side_effect = download
        manifest = self.execute()
        model = manifest["workspaces"][0]["models"][0]
        self.assertEqual(model["report_id"], OTHER_REPORT)
        self.assertEqual(len(model["report_failures"]), 1)

    def test_upload_failure_does_not_retry_an_immutable_destination(self):
        self.powerbi.values.return_value.append({"id": OTHER_REPORT, "datasetId": MODEL})
        self.archive.upload = Mock(side_effect=RuntimeError("sig=secret"))
        with self.assertRaises(smb.BackupError):
            self.execute()
        self.assertEqual(self.powerbi.download.call_count, 1)
        self.assertEqual(self.archive.upload.call_count, 1)
        self.assertNotIn("sig=secret", json.dumps(self.archive.records))
        path = self.archive.upload.call_args.args[1]
        self.assertFalse(path.exists())

    def test_inventory_failure_is_not_empty_workspace_success(self):
        self.fabric.values.side_effect = RuntimeError("Bearer super-secret")
        with self.assertRaises(smb.BackupError):
            self.execute()
        summary = self.archive.manifest()["summary"]
        self.assertEqual(summary["workspaces_inventory_incomplete"], 1)
        self.assertEqual(summary["workspaces_failed"], 1)
        self.assertNotIn("super-secret", json.dumps(self.archive.records))
        self.assertIn("total model count is unknown", self.output.getvalue())
        self.assertNotIn("No semantic models found.", self.output.getvalue())

    def test_report_inventory_failure_counts_discovered_models(self):
        self.powerbi.values.side_effect = smb.BackupError("Report inventory unavailable.")
        with self.assertRaises(smb.BackupError):
            self.execute()
        summary = self.archive.manifest()["summary"]
        self.assertEqual(summary["models_failed"], 1)
        self.assertEqual(summary["workspaces_inventory_incomplete"], 0)

    def test_empty_workspace_succeeds_without_report_inventory(self):
        self.fabric.values.return_value = []
        manifest = self.execute()
        self.assertEqual(manifest["summary"]["models_backed_up"], 0)
        self.assertEqual(manifest["summary"]["workspaces_succeeded"], 1)
        self.powerbi.values.assert_not_called()

    def test_partial_model_failure_does_not_stop_remaining_models(self):
        self.fabric.values.return_value.append({"id": OTHER_REPORT, "displayName": "Orphan"})
        with self.assertRaises(smb.BackupError):
            self.execute()
        summary = self.archive.manifest()["summary"]
        self.assertEqual(summary["models_backed_up"], 1)
        self.assertEqual(summary["models_failed"], 1)

    def test_archive_preflight_failure_prevents_downloads(self):
        self.archive.preflight = Mock(side_effect=RuntimeError("network blocked"))
        with self.assertRaises(RuntimeError):
            self.execute()
        self.fabric.values.assert_not_called()
        self.assertEqual(self.archive.records, {})

    def test_manifest_persistence_failure_is_not_success(self):
        original = self.archive.json

        def save(name, value):
            if name.endswith("/manifest.json"):
                raise smb.BackupError("Manifest write failed.")
            original(name, value)

        self.archive.json = save
        with self.assertRaisesRegex(smb.BackupError, "Manifest write failed"):
            self.execute()
        self.assertNotIn("SEMANTIC MODEL BACKUP", self.output.getvalue())


class ConfigurationAndNotebookTests(unittest.TestCase):
    def test_invalid_config_is_rejected(self):
        for key, value in (
            ("workspaces", []), ("workspaces", [WORKSPACE, WORKSPACE]),
            ("max_download_bytes", True), ("max_download_bytes", -1),
        ):
            config = copy.deepcopy(CONFIG)
            config[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                smb.validate_config(config)
        for key, value in (
            ("account_url", "https://user@archiveacct.blob.core.windows.net"),
            ("account_url", "https://archiveacct.blob.core.windows.net?sig=secret"),
            ("prefix", "../models"), ("prefix", "models\\run"),
            ("container", "invalid--container"),
        ):
            config = copy.deepcopy(CONFIG)
            config["archive"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                smb.validate_config(config)

    def test_key_vault_reference_uses_supplied_notebook_identity_callback(self):
        callback = Mock(return_value="secret-value")
        ref = {"key_vault_url": "https://myvault.vault.azure.net/", "secret_name": "app-secret"}
        self.assertEqual(smb.resolve_secret(ref, "secret", callback), "secret-value")
        callback.assert_called_once_with(ref["key_vault_url"], "app-secret")
        with self.assertRaises(ValueError):
            smb.resolve_secret(ref, "secret", None)

    def test_generated_notebook_is_self_contained_and_current(self):
        notebook = json.loads((ROOT / "SemanticModelBackupDownload.ipynb").read_text())
        self.assertEqual(notebook["nbformat"], 4)
        self.assertEqual(notebook["metadata"]["kernelspec"]["name"], "synapse_pyspark")
        cells = {c["id"]: c for c in notebook["cells"]}
        self.assertEqual(
            "".join(cells["implementation"]["source"]),
            (ROOT / "semantic_model_backup_download.py").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            "".join(cells["configuration"]["source"]),
            (ROOT / "config.example.py").read_text(encoding="utf-8"),
        )
        for cell in notebook["cells"]:
            if cell["cell_type"] == "code":
                self.assertEqual(cell["outputs"], [])
                self.assertIsNone(cell["execution_count"])
                source = "".join(cell["source"])
                if not source.startswith("%pip"):
                    compile(source, f"notebook:{cell['id']}", "exec")
        namespace = {}
        exec("".join(cells["implementation"]["source"]), namespace)
        self.assertTrue(callable(namespace["run"]))
        execution = "".join(cells["execute"]["source"])
        self.assertNotIn("basicConfig", execution)
        self.assertNotIn("json.dumps", execution)
        self.assertIn("result = run(CONFIG,", execution)


if __name__ == "__main__":
    unittest.main()
