"""Human-readable output without changing backup outcomes or manifest diagnostics."""

import copy
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import semantic_model_backup_download as smb


MANIFEST_URI = "https://archiveacct.blob.core.windows.net/backups/models/runs/example-run/manifest.json"


def sample_manifest():
    no_report = (
        "No associated Power BI report in the source workspace. "
        "There is no documented dataset-only PBIX export REST API; no temporary report was created."
    )
    no_data = (
        "PBIX has no embedded DataModel; metadata-only/live-connected files are not data backups."
    )
    return {
        "run_id": "example-run",
        "status": "failed",
        "summary": {
            "models_discovered": 3, "models_backed_up": 1, "models_failed": 2,
            "workspaces_succeeded": 0, "workspaces_failed": 1,
            "workspaces_inventory_incomplete": 0,
        },
        "workspaces": [{
            "id": "11111111-1111-1111-1111-111111111111",
            "inventory_complete": True, "status": "failed",
            "models": [
                {"id": "reportless-id", "name": "ReportlessModel", "status": "failed", "error": no_report},
                {
                    "id": "external-id", "name": "ExternalModel", "status": "failed",
                    "error": "No candidate produced a verified data-inclusive archive.",
                    "report_failures": [{"report_id": "external-report-id", "error": no_data}],
                },
                {
                    "id": "import-id", "name": "ImportModel", "status": "succeeded",
                    "archive": {
                        "size": 20345, "sha256": "ab" * 32, "etag": '"etag"',
                        "blob": "models/runs/example-run/import-id.pbix",
                    },
                },
            ],
        }],
    }


class ReportingTests(unittest.TestCase):
    def test_mixed_run_has_counts_aligned_results_and_manifest_location(self):
        report = smb.format_report(sample_manifest(), MANIFEST_URI)
        self.assertIn("Result     : INCOMPLETE", report)
        self.assertIn("3 discovered | 1 backed up | 2 failed", report)
        self.assertIn("1 total | 0 succeeded | 1 failed", report)
        self.assertIn("BACKED UP", report)
        self.assertIn("19.9 KiB", report)
        self.assertIn("SHA-256 verified", report)
        self.assertIn("No associated Power BI report.", report)
        self.assertIn("Export has no embedded data; not a data backup.", report)
        self.assertIn(MANIFEST_URI, report)
        self.assertNotIn("Last attempt:", report)
        self.assertNotIn("No candidate produced", report)

    def test_tables_wrap_long_names_and_errors_without_truncating(self):
        manifest = sample_manifest()
        model = manifest["workspaces"][0]["models"][0]
        model["name"] = "VeryLongSemanticModel" * 4 + "\u6a21\u578b"
        model["error"] = "An unusually detailed export failure " * 6 + "last detail preserved."
        report = smb.format_report(manifest, MANIFEST_URI)
        rows = [
            line.split(" | ") for line in report.splitlines()
            if " | " in line and not line.startswith(("Models", "Workspaces", "Status"))
        ]
        reconstructed_names = "".join(row[1].strip() for row in rows)
        self.assertIn(model["name"], reconstructed_names)
        self.assertIn("last detail preserved.", report)
        self.assertTrue(all(
            len(line) <= 110 for line in report.splitlines() if " | " in line
        ))
        self.assertNotIn("...", report)

    def test_inventory_failure_is_explicit_not_an_empty_success(self):
        manifest = sample_manifest()
        workspace = manifest["workspaces"][0]
        workspace.update(
            inventory_complete=False, models=[], error="Inventory request failed: HTTP 403.",
        )
        manifest["summary"].update(
            models_discovered=0, models_backed_up=0, models_failed=0,
            workspaces_inventory_incomplete=1,
        )
        report = smb.format_report(manifest, MANIFEST_URI)
        self.assertIn("WARNING", report)
        self.assertIn("their undiscovered models are not included in the counts", report)
        self.assertIn("Workspace error: Inventory request failed: HTTP 403.", report)
        self.assertIn("total model count is unknown", report)
        self.assertNotIn("No semantic models found.", report)

    def test_empty_successful_workspace_is_reported_as_empty(self):
        manifest = sample_manifest()
        manifest["status"] = "succeeded"
        manifest["workspaces"][0].update(models=[], status="succeeded")
        manifest["summary"].update(
            models_discovered=0, models_backed_up=0, models_failed=0,
            workspaces_succeeded=1, workspaces_failed=0,
        )
        report = smb.format_report(manifest, MANIFEST_URI)
        self.assertIn("Result     : COMPLETE", report)
        self.assertIn("No semantic models found.", report)
        self.assertNotIn("WARNING", report)
        self.assertNotIn("total model count is unknown", report)

    def test_success_after_failed_report_attempt_remains_success_with_notice(self):
        manifest = sample_manifest()
        model = manifest["workspaces"][0]["models"][2]
        model["report_failures"] = [{"report_id": "first-report", "error": "Export unavailable."}]
        report = smb.format_report(manifest, MANIFEST_URI)
        self.assertIn("BACKED UP", report)
        self.assertIn("Recovered after 1", report)
        self.assertIn("failed report attempt(s)", report)
        self.assertIn("see manifest.", report)

    def test_multiple_workspaces_have_separate_tables(self):
        manifest = sample_manifest()
        second = copy.deepcopy(manifest["workspaces"][0])
        second["id"] = "22222222-2222-2222-2222-222222222222"
        manifest["workspaces"].append(second)
        manifest["summary"].update(
            models_discovered=6, models_backed_up=2, models_failed=4, workspaces_failed=2,
        )
        report = smb.format_report(manifest, MANIFEST_URI)
        self.assertEqual(report.count("Workspace:"), 2)
        self.assertEqual(report.count("Result / reason"), 2)
        self.assertIn("2 total | 0 succeeded | 2 failed", report)
        self.assertIn(second["id"], report)

    def test_formatting_does_not_mutate_or_dump_manifest_diagnostics(self):
        manifest = sample_manifest()
        original = copy.deepcopy(manifest)
        report = smb.format_report(manifest, MANIFEST_URI)
        self.assertEqual(manifest, original)
        self.assertNotIn("ab" * 32, report)
        self.assertNotIn("external-report-id", report)
        self.assertNotIn("reportless-id", report)
        self.assertNotIn('"models_discovered"', report)
        self.assertIn("Full errors, model/report IDs", report)

    def test_control_whitespace_in_names_and_errors_cannot_break_table_rows(self):
        manifest = sample_manifest()
        model = manifest["workspaces"][0]["models"][0]
        model.update(name="Name\nwith\ttabs", error="Failure\nwith\ttabs")
        report = smb.format_report(manifest, MANIFEST_URI)
        self.assertIn("Name with tabs", report)
        self.assertIn("Failure with tabs", report)
        self.assertNotIn("\t", report)

    def test_readable_byte_units(self):
        for size, expected in (
            (0, "0 B"), (1, "1 B"), (1023, "1023 B"), (1024, "1.0 KiB"),
            (20345, "19.9 KiB"), (1024**2, "1.0 MiB"), (50 * 1024**3, "50.0 GiB"),
        ):
            with self.subTest(size=size):
                self.assertEqual(smb.format_size(size), expected)


if __name__ == "__main__":
    unittest.main()
