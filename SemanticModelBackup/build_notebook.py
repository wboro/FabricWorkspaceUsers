"""Package the tested Python implementation as a self-contained Fabric notebook."""

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
            "kernelspec": {"display_name": "Synapse PySpark", "language": "python", "name": "synapse_pyspark"},
            "language_info": {"name": "python"},
        },
        "cells": [
            cell("markdown", (
                "# Semantic model backup and restore\n\n"
                "Full native `.abf` backups, including imported model data, archived in Azure Blob Storage. "
                "No migration operations are performed.\n\n"
                "**Prerequisites:** Premium/Fabric capacity with XMLA read/write; service principal enabled "
                "in tenant settings and Workspace Admin on source/target workspaces; workspace-linked ADLS "
                "Gen2 storage with the existing `power-bi-backup/<workspace name>` folder. Grant the principal "
                "Storage Blob Data Contributor on staging and archive containers. The archive container must "
                "already exist. The notebook execution identity needs Key Vault secret read access for "
                "Key Vault references. Use a Fabric **PySpark** notebook.\n\n"
                "**Safety:** Restore is dry-run by default and refuses existing model names unless explicitly "
                "enabled. Select an exact backup run ID. Restore retains role definitions without copying "
                "source role memberships. Connections, credentials, refresh schedules, reports and workspace "
                "permissions are not provisioned. Verify these separately. Direct Lake/DirectQuery source "
                "data is not materialized into the backup. Native service limitations can prevent individual "
                "models from being backed up; any such failure fails the run and is recorded, never skipped.\n\n"
                "**Results:** Every completed backup prints the number of verified semantic models backed "
                "up, failed and attempted, plus workspace and cleanup outcomes. Counts are saved in "
                "`manifest.json` under `summary`. Partial-run errors include those statistics and the "
                "manifest location; a workspace that could not be inventoried is reported separately, "
                "not treated as an empty successful backup.\n\n"
                "Backups stream through notebook memory in bounded chunks, then are read back for SHA-256 "
                "verification. Staging ABFs are retained; configure a deliberate lifecycle policy. "
                "Read README.md for setup, storage limitations and recovery instructions.\n"
                "\n**Optional temporary ADLS:** Connect ADLS **once in the workspace running this "
                "notebook and leave that control workspace connected**. Its ID is resolved dynamically "
                "from `notebookutils.runtime.context['currentWorkspaceId']`, including after moving the "
                "notebook to production. No tenant-level storage setting is used or changed. "
                "`backup.temporary_adls.enabled=True` uses the notebook "
                "execution user's Power BI token to temporarily attach the control account only "
                "to an unlinked workspace with no Gen1 dataflows. Existing connections are preserved "
                "and their staging account is discovered automatically. The service principal still "
                "performs model discovery, XMLA and Blob operations. Cleanup runs in `finally` and "
                "disconnects only connections made by this run. No account settings are weakened; no "
                "existing roles, files or dataflows are removed. Run exclusively: concurrent workspace connection "
                "changes are not supported. Interrupted sessions, uncertain XMLA outcomes or new Gen1 "
                "dataflows can require manual cleanup; immutable connection recovery records are saved "
                "alongside each workspace's archive. Existing retained backup folders are not moved "
                "automatically and can block reattachment. This option applies to backup only. "
                "Validate a small pilot before large-scale use; a 13,000-workspace deployment has "
                "not been validated.\n"
            ), "overview"),
            cell("code", "%pip install " + " ".join(f'"{r}"' for r in requirements) + "\n", "dependencies"),
            cell("markdown", (
                "## Configuration\n\nEdit only this cell. Literal credentials are supported for local testing, "
                "but Key Vault references are recommended. Do not display CONFIG or save notebook outputs "
                "containing secrets. Workspace IDs must be GUIDs. Set `mode` to `restore` only after selecting "
                "the backup run and workspace mapping below.\n"
            ), "configuration-help"),
            cell("code", (ROOT / "config.example.py").read_text(encoding="utf-8"), "configuration",
                 {"tags": ["parameters"]}),
            cell("markdown", "## Implementation\n\nEmbedded from `semantic_model_backup.py`; no extra file upload is needed.\n",
                 "implementation-help"),
            cell("code", (ROOT / "semantic_model_backup.py").read_text(encoding="utf-8"), "implementation"),
            cell("code", (
                "logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')\n"
                "result = run(CONFIG)\n"
                "print(json.dumps(result, indent=2))\n"
            ), "execute"),
        ],
    }
    destination = ROOT / "SemanticModelBackup.ipynb"
    destination.write_text(json.dumps(notebook, indent=1, ensure_ascii=True) + "\n", encoding="utf-8")
    print(f"Built {destination.name}")


if __name__ == "__main__":
    build()
