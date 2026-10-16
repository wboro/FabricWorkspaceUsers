"""Download data-inclusive PBIX files and archive them without native XMLA backup."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
import time
import zipfile
import zlib
from contextlib import ExitStack
from datetime import datetime, timezone
from itertools import zip_longest
from pathlib import Path
from textwrap import wrap
from urllib.parse import parse_qs, urljoin, urlparse
from uuid import UUID, uuid4


LOG = logging.getLogger("semantic_model_backup_download")
FABRIC_API = "https://api.fabric.microsoft.com/v1/"
POWERBI_API = "https://api.powerbi.com/v1.0/myorg/"
CHUNK_SIZE = 8 * 1024 * 1024


class BackupError(RuntimeError):
    """An operational error whose message is safe to persist."""


def now():
    return datetime.now(timezone.utc).isoformat()


def text(value, label):
    if not isinstance(value, str) or not value.strip() or "<" in value or ">" in value:
        raise ValueError(f"Configure {label} with a nonempty value, not a placeholder.")
    return value


def guid(value):
    return str(UUID(value))


def validate_config(config):
    archive = config["archive"]
    url = text(archive["account_url"], "archive.account_url").rstrip("/")
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not re.fullmatch(r"[a-z0-9]{3,24}\.blob\.core\.windows\.net", parsed.netloc)
        or parsed.path or parsed.query or parsed.fragment
    ):
        raise ValueError("Use https://<account>.blob.core.windows.net without a path or SAS.")
    container = text(archive["container"], "archive.container")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", container) or "--" in container:
        raise ValueError("archive.container must be a valid existing Azure container name.")
    prefix = text(archive["prefix"], "archive.prefix")
    if "\\" in prefix or any(p in ("", ".", "..") for p in prefix.split("/")):
        raise ValueError("archive.prefix must be a relative Blob path without dot segments.")
    workspaces = [guid(w) for w in config["workspaces"]]
    if not workspaces or len(set(workspaces)) != len(workspaces):
        raise ValueError("Configure a nonempty list of distinct workspace GUIDs.")
    limit = config.get("max_download_bytes", 50 * 1024**3)
    if type(limit) is not int or not 1 <= limit <= 50 * 1024**3:
        raise ValueError("max_download_bytes must be an integer between 1 and 50 GiB.")
    return url, container, prefix, workspaces, limit


def safe_error(exc):
    # SDK/HTTP exceptions can contain tokens, signed URLs or response bodies.
    if isinstance(exc, BackupError):
        return str(exc)
    return f"{type(exc).__name__}: operation failed; sensitive exception details omitted."


class Api:
    def __init__(self, credential, base_url, scope, session=None, sleep=time.sleep):
        if session is None:
            import requests

            session = requests.Session()
        self.credential = credential
        self.base_url = base_url
        self.scope = scope
        self.session = session
        self.sleep = sleep

    def url(self, path):
        url = urljoin(self.base_url, path)
        if not url.startswith(self.base_url):
            raise BackupError("Refusing an API URL outside the configured service.")
        return url

    def get(self, path, stream=False):
        return self._get(self.url(path), stream=stream)

    def _get(self, url, stream=False, authorized=True):
        for attempt in range(6):
            headers = {}
            if authorized:
                token = self.credential.get_token(self.scope).token
                headers["Authorization"] = f"Bearer {token}"
            response = self.session.get(
                url, headers=headers,
                stream=stream, timeout=(15, 300), allow_redirects=False,
            )
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 5:
                retry = response.headers.get("Retry-After", "")
                response.close()
                self.sleep(min(int(retry), 300) if retry.isdigit() else 2**attempt)
                continue
            return response
        raise AssertionError("Unreachable retry state")

    def values(self, path, fabric=False):
        results = []
        seen = set()
        while path:
            url = self.url(path)
            if url in seen:
                raise BackupError("The API returned a repeated pagination link.")
            seen.add(url)
            with self.get(url) as response:
                if response.status_code != 200:
                    raise BackupError(f"Inventory request failed: HTTP {response.status_code}.")
                page = response.json()
            results.extend(page["value"])
            path = page.get("continuationUri") if fabric else page.get("@odata.nextLink")
            if fabric and not path and page.get("continuationToken"):
                from urllib.parse import urlencode

                path = urljoin(self.base_url, urlparse(url).path) + "?" + urlencode({
                    "type": "SemanticModel", "continuationToken": page["continuationToken"],
                })
        identifiers = [guid(item["id"]) for item in results]
        if len(identifiers) != len(set(identifiers)):
            raise BackupError("The API returned duplicate item IDs.")
        return results

    def close(self):
        self.session.close()


class PowerBiApi(Api):
    def __init__(self, credential, session=None, sleep=time.sleep):
        super().__init__(
            credential, POWERBI_API, "https://analysis.windows.net/powerbi/api/.default",
            session, sleep,
        )

    def export_redirect_authorized(self, target, export_path):
        parsed = urlparse(target)
        if parsed.scheme != "https" or parsed.fragment:
            raise BackupError("Export redirected to an unsupported download endpoint.")
        if (
            parsed.netloc == "api.powerbi.com"
            or re.fullmatch(r"wabi-[a-z0-9]+(?:-[a-z0-9]+)*\.analysis\.windows\.net", parsed.netloc)
        ):
            if (
                parsed.path != export_path
                or parse_qs(parsed.query, keep_blank_values=True).get("downloadType") != ["IncludeModel"]
            ):
                raise BackupError("Power BI routing changed the export resource or download type.")
            return True
        if (
            re.fullmatch(r"[a-z0-9]{3,24}\.blob\.core\.windows\.net", parsed.netloc)
            and parse_qs(parsed.query).get("sig")
        ):
            return False
        raise BackupError("Export redirected to an unsupported download endpoint.")

    def download(self, workspace, report, destination, limit):
        path = (
            f"groups/{guid(workspace)}/reports/{guid(report)}/Export"
            "?downloadType=IncludeModel&preferClientRouting=true"
        )
        current_url = self.url(path)
        export_path = urlparse(current_url).path
        seen = {current_url}
        signed_download = False
        response = self.get(path, stream=True)
        try:
            for hop in range(6):
                if response.status_code not in (301, 302, 303, 307, 308):
                    break
                target = urljoin(current_url, response.headers.get("Location", ""))
                response.close()
                if hop == 5:
                    raise BackupError("Export exceeded the redirect limit.")
                if target in seen:
                    raise BackupError("Export returned a repeated redirect URL.")
                authorized = self.export_redirect_authorized(target, export_path)
                if signed_download and authorized:
                    raise BackupError("A temporary Blob download cannot redirect back to an authenticated API.")
                # Client routing requires authentication on the same export at a
                # trusted Power BI cluster. Signed Blob downloads never receive it.
                signed_download = signed_download or not authorized
                seen.add(target)
                current_url = target
                response = self._get(target, stream=True, authorized=authorized)
            if response.status_code != 200:
                raise BackupError(
                    f"Data-inclusive PBIX export failed: HTTP {response.status_code}. "
                    "Check download permissions and Microsoft PBIX export limitations."
                )
            declared = response.headers.get("Content-Length")
            if declared is not None and (not declared.isdigit() or int(declared) > limit):
                raise BackupError("Export has an invalid length or exceeds max_download_bytes.")
            size = 0
            digest = hashlib.sha256()
            with destination.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    size += len(chunk)
                    if size > limit:
                        raise BackupError("Export exceeds max_download_bytes.")
                    handle.write(chunk)
                    digest.update(chunk)
            if not size or (
                declared is not None and not response.headers.get("Content-Encoding")
                and size != int(declared)
            ):
                raise BackupError("The downloaded PBIX is empty or truncated.")
        finally:
            response.close()
        validate_pbix(destination)
        return {"size": size, "sha256": digest.hexdigest()}


def validate_pbix(path):
    try:
        with zipfile.ZipFile(path) as package:
            models = [i for i in package.infolist() if i.filename == "DataModel"]
            if len(models) != 1 or models[0].file_size == 0:
                raise BackupError(
                    "PBIX has no embedded DataModel; metadata-only/live-connected files "
                    "are not data backups."
                )
            # Check integrity without loading the model into RAM or extracting files.
            if package.testzip() is not None:
                raise BackupError("PBIX failed ZIP integrity validation.")
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, zlib.error, EOFError) as exc:
        if isinstance(exc, BackupError):
            raise
        raise BackupError("PBIX is not a readable ZIP package; cannot verify embedded data.") from None


class Archive:
    def __init__(self, credential, url, container):
        from azure.storage.blob import BlobServiceClient

        self.service = BlobServiceClient(
            url, credential=credential, max_block_size=CHUNK_SIZE,
            max_single_put_size=CHUNK_SIZE, retry_total=4,
            connection_timeout=15, read_timeout=120,
        )
        self.container = self.service.get_container_client(container)

    def preflight(self):
        self.container.get_container_properties()

    def json(self, name, value):
        from azure.storage.blob import ContentSettings

        self.container.get_blob_client(name).upload_blob(
            json.dumps(value, indent=2, ensure_ascii=True).encode("utf-8"),
            overwrite=False, validate_content=True,
            content_settings=ContentSettings(content_type="application/json"),
        )

    def upload(self, name, path, expected):
        from azure.core import MatchConditions
        from azure.storage.blob import ContentSettings

        blob = self.container.get_blob_client(name)
        with path.open("rb") as handle:
            result = blob.upload_blob(
                handle, length=expected["size"], overwrite=False, validate_content=True,
                max_concurrency=1,
                content_settings=ContentSettings(content_type="application/octet-stream"),
            )
        digest = hashlib.sha256()
        size = 0
        for chunk in blob.download_blob(
            etag=result["etag"], match_condition=MatchConditions.IfNotModified,
            max_concurrency=1,
        ).chunks():
            digest.update(chunk)
            size += len(chunk)
        if size != expected["size"] or digest.hexdigest() != expected["sha256"]:
            raise BackupError("Archive read-back length/SHA-256 verification failed.")
        return {**expected, "etag": result["etag"], "blob": name}

    def close(self):
        self.service.close()


def format_size(size):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def format_report(manifest, manifest_uri):
    summary = manifest["summary"]
    lines = [
        "SEMANTIC MODEL BACKUP",
        "====================",
        f"Result     : {'COMPLETE' if manifest['status'] == 'succeeded' else 'INCOMPLETE'}",
        f"Run ID     : {manifest['run_id']}",
        f"Models     : {summary['models_discovered']} discovered | "
        f"{summary['models_backed_up']} backed up | {summary['models_failed']} failed",
        f"Workspaces : {len(manifest['workspaces'])} total | "
        f"{summary['workspaces_succeeded']} succeeded | {summary['workspaces_failed']} failed",
    ]
    if summary["workspaces_inventory_incomplete"]:
        lines.append(
            f"WARNING    : {summary['workspaces_inventory_incomplete']} workspace inventories "
            "are incomplete; their undiscovered models are not included in the counts."
        )

    widths = (9, 30, 10, 52)

    def add_row(values):
        cells = [
            wrap(" ".join(str(value).split()), width=width, break_on_hyphens=False) or [""]
            for value, width in zip(values, widths)
        ]
        for row in zip_longest(*cells, fillvalue=""):
            lines.append(" | ".join(
                value.rjust(width) if index == 2 else value.ljust(width)
                for index, (value, width) in enumerate(zip(row, widths))
            ).rstrip())

    for workspace in manifest["workspaces"]:
        lines.extend(["", f"Workspace: {workspace['id']}"])
        if workspace.get("error"):
            lines.extend(wrap(
                "Workspace error: " + workspace["error"], width=110, subsequent_indent="  ",
            ))
        if not workspace["inventory_complete"]:
            lines.append("Model inventory is incomplete; this workspace's total model count is unknown.")
        if not workspace["models"]:
            if workspace["inventory_complete"] and not workspace.get("error"):
                lines.append("No semantic models found.")
            continue
        add_row(("Status", "Semantic model", "Size", "Result / reason"))
        lines.append("-+-".join("-" * width for width in widths))
        for model in workspace["models"]:
            attempts = model.get("report_failures", [])
            if model["status"] == "succeeded":
                status = "BACKED UP"
                size = format_size(model["archive"]["size"])
                detail = "PBIX uploaded; SHA-256 verified."
                if attempts:
                    detail += f" Recovered after {len(attempts)} failed report attempt(s); see manifest."
            else:
                status, size = "FAILED", "-"
                detail = attempts[-1]["error"] if attempts else model["error"]
                if detail.startswith("No associated Power BI report"):
                    detail = "No associated Power BI report."
                elif detail.startswith("PBIX has no embedded DataModel"):
                    detail = "Export has no embedded data; not a data backup."
            add_row((status, model["name"], size, detail))

    lines.extend([
        "", "Backup scope: embedded imported data only. External source data is not copied.",
        "", "Manifest:", f"  {manifest_uri}",
        "Full errors, model/report IDs, report attempts and checksums are in the manifest.",
    ])
    return "\n".join(lines)


def backup(config, fabric, powerbi, archive):
    url, container, prefix, workspaces, limit = validate_config(config)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex[:8]
    root = f"{prefix}/runs/{run_id}"
    manifest = {
        "schema_version": 1, "format": "PBIX-IncludeModel", "run_id": run_id,
        "started_at": now(), "status": "running", "workspaces": [],
        "data_scope": (
            "Embedded imported data only; DirectQuery/Direct Lake/external source data "
            "is not materialized. This is not a transactionally consistent workspace snapshot."
        ),
    }
    archive.preflight()
    archive.json(f"{root}/started.json", manifest)
    LOG.info("Download backup run %s", run_id)
    for workspace in workspaces:
        entry = {
            "id": workspace, "inventory_complete": False, "status": "failed", "models": [],
        }
        manifest["workspaces"].append(entry)
        try:
            models = fabric.values(
                f"workspaces/{workspace}/items?type=SemanticModel", fabric=True,
            )
            # Validate every ID before claiming a complete inventory.
            for model in models:
                guid(model["id"])
            entry["models"] = [
                {"id": guid(m["id"]), "name": m["displayName"], "status": "pending"}
                for m in models
            ]
            entry["inventory_complete"] = True
            reports = powerbi.values(f"groups/{workspace}/reports") if models else []
            for model in entry["models"]:
                candidates = sorted(
                    (r for r in reports if isinstance(r.get("datasetId"), str)
                     and r["datasetId"].lower() == model["id"]
                     and r.get("reportType", "PowerBIReport") == "PowerBIReport"),
                    key=lambda r: r["id"],
                )
                failures = []
                for report in candidates:
                    report_id = guid(report["id"])
                    archive_attempted = False
                    try:
                        with tempfile.TemporaryDirectory(
                            prefix="semantic-model-", dir=config.get("temp_directory"),
                        ) as directory:
                            path = Path(directory) / f"{model['id']}.pbix"
                            info = powerbi.download(workspace, report_id, path, limit)
                            name = f"{root}/workspaces/{workspace}/{model['id']}.pbix"
                            archive_attempted = True
                            model["archive"] = archive.upload(name, path, info)
                        model.update(status="succeeded", report_id=report_id)
                        break
                    except Exception as exc:
                        failures.append({"report_id": report_id, "error": safe_error(exc)})
                        LOG.debug(
                            "Model %s report %s failed: %s",
                            model["id"], report_id, safe_error(exc),
                        )
                        # An uncertain upload may already have committed the immutable blob.
                        # Only try another report if no archive write was attempted.
                        if archive_attempted:
                            break
                if failures:
                    model["report_failures"] = failures
                if model["status"] != "succeeded":
                    model["status"] = "failed"
                    model["error"] = (
                        "No associated Power BI report in the source workspace. "
                        "There is no documented dataset-only PBIX export REST API; "
                        "no temporary report was created."
                        if not candidates else (
                            "No candidate produced a verified data-inclusive archive. "
                            f"Last attempt: {failures[-1]['error']}"
                        )
                    )
                    LOG.debug("Model %s failed: %s", model["id"], model["error"])
            entry["status"] = (
                "succeeded" if all(m["status"] == "succeeded" for m in entry["models"])
                else "failed"
            )
        except Exception as exc:
            entry["error"] = safe_error(exc)
            for model in entry["models"]:
                if model["status"] == "pending":
                    model.update(status="failed", error=entry["error"])
            LOG.debug("Workspace %s failed: %s", workspace, entry["error"])
    all_models = [m for w in manifest["workspaces"] for m in w["models"]]
    summary = {
        "models_discovered": len(all_models),
        "models_backed_up": sum(m["status"] == "succeeded" for m in all_models),
        "models_failed": sum(m["status"] != "succeeded" for m in all_models),
        "workspaces_succeeded": sum(w["status"] == "succeeded" for w in manifest["workspaces"]),
        "workspaces_failed": sum(w["status"] != "succeeded" for w in manifest["workspaces"]),
        "workspaces_inventory_incomplete": sum(
            not w["inventory_complete"] for w in manifest["workspaces"]
        ),
    }
    manifest.update(
        summary=summary, finished_at=now(),
        status="failed" if summary["workspaces_failed"] else "succeeded",
    )
    name = f"{root}/manifest.json"
    archive.json(name, manifest)
    manifest_uri = f"{url}/{container}/{name}"
    print(format_report(manifest, manifest_uri), flush=True)
    if manifest["status"] == "failed":
        raise BackupError(
            f"Backup incomplete: {summary['models_backed_up']} backed up; "
            f"{summary['models_failed']} failed; {summary['workspaces_failed']} failed workspace(s). "
            f"See the results table above.\nManifest: {manifest_uri}"
        )
    return manifest


def resolve_secret(value, label, get_secret):
    if isinstance(value, dict):
        if set(value) != {"key_vault_url", "secret_name"} or get_secret is None:
            raise ValueError(f"{label}: Key Vault references require a get_secret callback.")
        vault = text(value["key_vault_url"], "key_vault_url")
        if not re.fullmatch(r"https://[a-zA-Z0-9-]{3,24}\.vault\.azure\.net/?", vault):
            raise ValueError("Use a public-Azure HTTPS Key Vault URL without a query.")
        value = get_secret(vault, text(value["secret_name"], "secret_name"))
    return text(value, label)


def run(config, get_secret=None):
    """Use the configured application for both discovery/download and archive access."""
    from azure.identity import ClientSecretCredential

    url, container, _, _, _ = validate_config(config)
    principal = config["service_principal"]
    try:
        tenant, client, secret = (
            resolve_secret(principal[key], key, get_secret)
            for key in ("tenant_id", "client_id", "client_secret")
        )
        with ExitStack() as stack:
            credential = stack.enter_context(
                ClientSecretCredential(guid(tenant), guid(client), secret),
            )
            fabric = Api(credential, FABRIC_API, "https://api.fabric.microsoft.com/.default")
            stack.callback(fabric.close)
            powerbi = PowerBiApi(credential)
            stack.callback(powerbi.close)
            archive = Archive(credential, url, container)
            stack.callback(archive.close)
            return backup(config, fabric, powerbi, archive)
    except Exception as exc:
        raise BackupError(safe_error(exc)) from None
