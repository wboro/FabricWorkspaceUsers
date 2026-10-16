"""Build a self-contained Fabric notebook from the tested implementation."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def cell(kind, source, identifier, metadata=None):
    result = {
        "cell_type": kind, "id": identifier, "metadata": metadata or {},
        "source": source.splitlines(keepends=True),
    }
    if kind == "code":
        result.update(execution_count=None, outputs=[])
    return result


def build():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    notebook = {
        "nbformat": 4, "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {
                "display_name": "Synapse PySpark", "language": "python", "name": "synapse_pyspark",
            },
            "language_info": {"name": "python"},
        },
        "cells": [
            cell("markdown", (
                "# Download semantic models with imported data to private ADLS Gen2\n\n"
                "Uses Power BI's PBIX export API with `downloadType=IncludeModel`. "
                "**No native backup, XMLA backup, workspace storage attachment, "
                "or network-setting changes.** No Lakehouse attachment is required.\n\n"
                "## Before running\n\n"
                "1. Import this file into Fabric as a **PySpark notebook**. Configure "
                "approved private connectivity from the notebook Spark runtime to the "
                "archive account's **Blob** endpoint. For example, create and approve a "
                "Fabric managed private endpoint for the storage account's `blob` "
                "subresource, subject to your capacity/region support. Keep storage "
                "public network access disabled. A DFS-only endpoint is insufficient "
                "for this Blob SDK upload path. Use the normal Blob hostname with "
                "private DNS, not a `privatelink` hostname. See README.md.\n"
                "2. Allow the runtime to reach Entra authentication, Fabric/Power BI "
                "APIs and regional `wabi-*.analysis.windows.net` routing endpoints, "
                "package sources (or preinstall dependencies in a Fabric "
                "Environment), and any service-managed temporary export Blob endpoint. "
                "That export endpoint is separate from your private archive account.\n"
                "3. Enable service-principal access to the relevant Fabric/Power BI "
                "APIs in tenant settings. Add the application as Workspace Admin "
                "on each source workspace. Enable PBIX downloads under tenant policy.\n"
                "4. Create the destination ADLS Gen2 filesystem and grant the application "
                "**Storage Blob Data Contributor** there, plus any required HNS ACLs. "
                "The notebook execution identity must be able to read configured Key Vault "
                "secrets. Private Key Vaults also need approved notebook connectivity.\n"
                "5. Reserve driver-local disk for the largest PBIX, plus headroom. "
                "Files are processed one at a time and deleted after each attempt.\n\n"
                "## Coverage and safety\n\n"
                "**This cannot guarantee a full backup of every semantic model.** "
                "Microsoft restricts data-inclusive PBIX export, including incremental "
                "refresh, large semantic model storage format via REST, Direct Lake, "
                "some XMLA-modified models, automatic aggregations and size limits. "
                "Models without an associated Power BI report in the same workspace "
                "are reported as failures: no undocumented dataset export API or "
                "temporary report mutation is used. See Microsoft's limitations linked "
                "in README.md.\n\n"
                "Only embedded imported data is retained. DirectQuery and external-source "
                "data is not materialized. A model is successful only after the PBIX "
                "contains a nonempty `DataModel`, passes ZIP integrity checks, and the "
                "uploaded object passes full SHA-256/length read-back verification. "
                "Live-connected/metadata-only files are rejected, with no fallback.\n\n"
                "Every workspace model is inventoried, failures are saved in "
                "`manifest.json`, and any incomplete workspace fails notebook execution. "
                "Output contains a readable summary and per-workspace tables with "
                "model names, status, archive sizes and failure reasons. Long values "
                "wrap instead of being truncated. The final exception is concise; "
                "full diagnostics remain in the manifest linked below the tables. A missing report "
                "or a PBIX without embedded data remains a failure, not a skipped model. "
                "Regional HTTP 307 routing is authenticated only on trusted Power BI "
                "endpoints for the same report and data-inclusive export mode; signed "
                "Blob downloads never receive that bearer token. Archives are immutable and "
                "contain sensitive data; use approved storage encryption, retention and "
                "access controls. No model/report, permissions, refresh schedules or "
                "storage connections are changed. Workspaces are not frozen during export. "
                "PBIX restore/republish is a separate manual operation, not ABF restore.\n"
            ), "overview"),
            cell("code", "%pip install " + " ".join(f'"{r}"' for r in requirements) + "\n",
                 "dependencies"),
            cell("markdown", (
                "## Configuration\n\nEdit this cell. Do not commit real credentials or "
                "print CONFIG. Each service-principal value accepts a literal or "
                "`{\"key_vault_url\": \"https://vault.vault.azure.net/\", "
                "\"secret_name\": \"name\"}`. Key Vault uses the notebook execution "
                "identity; API downloads and archive I/O use the configured application.\n"
            ), "configuration-help"),
            cell("code", (ROOT / "config.example.py").read_text(encoding="utf-8"),
                 "configuration", {"tags": ["parameters"]}),
            cell("markdown", "## Implementation\n\nEmbedded; no separate Python file upload is needed.\n",
                 "implementation-help"),
            cell("code", (ROOT / "semantic_model_backup_download.py").read_text(encoding="utf-8"),
                 "implementation"),
            cell("code", (
                "import notebookutils\n\n"
                "logging.getLogger('azure').setLevel(logging.WARNING)\n"
                "result = run(CONFIG, get_secret=notebookutils.credentials.getSecret)\n"
            ), "execute"),
        ],
    }
    destination = ROOT / "SemanticModelBackupDownload.ipynb"
    destination.write_text(json.dumps(notebook, indent=1, ensure_ascii=True) + "\n", encoding="utf-8")
    print(f"Built {destination.name}")


if __name__ == "__main__":
    build()
