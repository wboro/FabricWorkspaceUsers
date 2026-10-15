"""Native semantic-model ABF backup/restore; embedded in the Fabric notebook."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from base64 import b64encode
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse
from uuid import UUID, uuid4
from xml.etree import ElementTree as ET


LOG = logging.getLogger("semantic_model_backup")
FABRIC_API = "https://api.fabric.microsoft.com/v1/"
POWERBI_API = "https://api.powerbi.com/v1.0/myorg/"
NATIVE_CONTAINER = "power-bi-backup"
EMPTY_GUID = "00000000-0000-0000-0000-000000000000"
UPLOAD_BLOCK_SIZE = 8 * 1024 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex[:8]


def workspace_id(value: str) -> str:
    return str(UUID(value))


def required_string(value: Any, label: str) -> str:
    if (
        not isinstance(value, str) or not value.strip()
        or re.fullmatch(r"<[^<>]+>", value)
    ):
        raise ValueError(f"Configure {label} with a nonempty value, not a placeholder.")
    return value


def account_url(value: Any) -> str:
    value = required_string(value, "storage account_url").rstrip("/")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not re.fullmatch(r"[a-z0-9]{3,24}\.blob\.core\.windows\.net", parsed.hostname)
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.port
    ):
        raise ValueError("Use an HTTPS public-Azure Blob account URL without a path or SAS.")
    return value


def blob_path(value: Any, label: str) -> str:
    value = required_string(value, label)
    if "\\" in value or any(p in ("", ".", "..") for p in value.split("/")):
        raise ValueError(f"{label} must be a relative Blob path without empty or dot segments.")
    return value


class SecretResolver:
    def __init__(self, get_secret=None):
        self.get_secret = get_secret
        self._sensitive: list[str] = []

    def resolve(self, value: Any, label: str) -> str:
        if isinstance(value, dict):
            if set(value) != {"key_vault_url", "secret_name"}:
                raise ValueError(f"{label}: expected key_vault_url and secret_name.")
            if self.get_secret is None:
                raise ValueError("Key Vault references require Fabric NotebookUtils credentials.")
            vault = required_string(value["key_vault_url"], "key_vault_url")
            parsed = urlparse(vault)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or not re.fullmatch(r"[a-zA-Z0-9-]{3,24}\.vault\.azure\.net", parsed.hostname)
                or parsed.path not in ("", "/")
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.port
            ):
                raise ValueError("Use an HTTPS public-Azure Key Vault URL.")
            value = self.get_secret(vault, required_string(value["secret_name"], "secret_name"))
        value = required_string(value, label)
        self._sensitive.extend([
            value, value.replace("&", "&amp;").replace("<", "&lt;"),
            json.dumps(value)[1:-1],
        ])
        return value

    def error(self, exc: Exception) -> str:
        message = str(exc)
        for secret in sorted(set(self._sensitive), key=len, reverse=True):
            message = message.replace(secret, "[REDACTED]")
        message = re.sub(r"(?i)(bearer\s+)[^\s\"'<>]+", r"\1[REDACTED]", message)
        return f"{type(exc).__name__}: {message[:1500]}"


class FabricApi:
    base_url = FABRIC_API
    scope = "https://api.fabric.microsoft.com/.default"
    service = "Fabric"

    def __init__(self, credential, session=None, sleep=time.sleep):
        if session is None:
            import requests

            session = requests.Session()
        self.credential = credential
        self.session = session
        self.sleep = sleep

    def url(self, path: str) -> str:
        url = urljoin(self.base_url, path)
        if not url.startswith(self.base_url):
            raise ValueError(f"Refusing to send a {self.service} token to an untrusted URL.")
        return url

    def token(self) -> str:
        return self.credential.get_token(self.scope).token

    def get(self, path: str) -> dict:
        url = self.url(path)
        for attempt in range(6):
            token = self.token()
            response = self.session.get(
                url, headers={"Authorization": f"Bearer {token}"}, timeout=(15, 120),
                allow_redirects=False,
            )
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 5:
                retry_after = response.headers.get("Retry-After", "")
                self.sleep(min(int(retry_after), 300) if retry_after.isdigit() else 2 ** attempt)
                continue
            if response.status_code != 200:
                # Do not include response bodies: they can contain connection information.
                raise RuntimeError(
                    f"{self.service} GET failed: HTTP {response.status_code}; "
                    f"request ID {response.headers.get('request-id', 'unavailable')}."
                )
            return response.json()
        raise AssertionError("Unreachable retry state")

    def workspace(self, identifier: str) -> dict:
        result = self.get(f"workspaces/{workspace_id(identifier)}")
        if not result.get("capacityId"):
            raise ValueError("Native backup/restore requires a workspace assigned to capacity.")
        return result

    def models(self, identifier: str) -> list[dict]:
        path = f"workspaces/{workspace_id(identifier)}/items?type=SemanticModel"
        models = []
        seen = set()
        while path:
            if path in seen:
                raise RuntimeError("Fabric returned a repeated pagination link.")
            seen.add(path)
            page = self.get(path)
            models.extend(page["value"])
            path = page.get("continuationUri")
            if not path and page.get("continuationToken"):
                from urllib.parse import urlencode

                path = (
                    f"workspaces/{workspace_id(identifier)}/items?"
                    + urlencode({"type": "SemanticModel", "continuationToken": page["continuationToken"]})
                )
        ids = [m["id"] for m in models]
        if len(ids) != len(set(ids)):
            raise RuntimeError("Fabric returned duplicate semantic model IDs.")
        return models


class PowerBiApi(FabricApi):
    base_url = POWERBI_API
    service = "Power BI"

    def __init__(self, token_provider, session=None, sleep=time.sleep):
        super().__init__(None, session, sleep)
        self.token_provider = token_provider

    def token(self) -> str:
        return required_string(self.token_provider(), "notebook user Power BI token")

    def values(self, path: str) -> list[dict]:
        values = []
        seen = set()
        while path:
            url = self.url(path)
            if url in seen:
                raise RuntimeError("Power BI returned a repeated pagination link.")
            seen.add(url)
            page = self.get(url)
            values.extend(page["value"])
            path = page.get("@odata.nextLink")
        return values

    def storage_id(self, identifier: str) -> str | None:
        group = self.get(f"groups/{workspace_id(identifier)}")
        value = group.get("dataflowStorageId")
        if value is None:
            return None
        value = workspace_id(value)
        return None if value == EMPTY_GUID else value

    def storage_accounts(self) -> list[dict]:
        return self.values("dataflowStorageAccounts")

    def dataflows(self, identifier: str) -> list[dict]:
        return self.values(f"groups/{workspace_id(identifier)}/dataflows")

    def assign_storage(self, identifier: str, storage_id: str | None) -> None:
        url = self.url(f"groups/{workspace_id(identifier)}/AssignToDataflowStorage")
        token = self.token()
        response = self.session.post(
            url, headers={"Authorization": f"Bearer {token}"},
            json={"dataflowStorageId": workspace_id(storage_id) if storage_id else EMPTY_GUID},
            timeout=(15, 120), allow_redirects=False,
        )
        # A failed/timeout POST may have taken effect. Reconcile state; never retry it.
        if response.status_code != 200:
            raise RuntimeError(
                f"Power BI storage assignment failed: HTTP {response.status_code}; "
                f"request ID {response.headers.get('request-id', 'unavailable')}. "
                "Use a delegated notebook user with Workspace Admin and "
                "StorageAccount.ReadWrite.All / Workspace.ReadWrite.All access."
            )


class TemporaryAdls:
    def __init__(
        self, api, store, ws: dict, spec: dict, default_url, options: dict, journal,
        control: dict | None = None, accounts: list[dict] | None = None,
    ):
        self.api = api
        self.store = store
        self.ws = ws
        self.spec = spec
        self.default_url = default_url
        self.options = options
        self.journal = journal
        self.control = control
        self.accounts = accounts
        self.attempted = False
        self.uncertain_backup = False
        self.record = {
            "original_storage_id": None, "temporary_storage_id": None,
            "status": "checking", "cleanup_status": "not_needed",
        }

    def wait(self, check, label: str) -> None:
        timeout = self.options.get("timeout_seconds", 300)
        for remaining in range(timeout, -5, -5):
            if check():
                return
            if remaining > 0:
                self.api.sleep(min(5, remaining))
        raise TimeoutError(f"Timed out waiting for {label}; inspect the ADLS recovery record.")

    def native_folder_exists(self, url: str) -> bool:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            self.store.check_native_folder({
                "account_url": url, "container": NATIVE_CONTAINER,
                "blob": blob_path(self.ws["displayName"], "native staging folder"),
            })
            return True
        except ResourceNotFoundError:
            return False

    def connect(self) -> dict:
        identifier = self.ws["id"]
        original = self.api.storage_id(identifier)
        self.record["original_storage_id"] = original
        if self.control and identifier == self.control["id"]:
            if original != self.control["dataflow_storage_id"]:
                raise RuntimeError("The control workspace connection changed; it will not be modified.")
        accounts = self.accounts if self.accounts is not None else self.api.storage_accounts()
        configured_url = self.spec.get("staging_account_url", self.default_url)
        if original:
            selected = [a for a in accounts if workspace_id(a["id"]) == original]
        else:
            if self.api.dataflows(identifier):
                raise ValueError("Refusing temporary ADLS attachment: workspace has Gen1 dataflows.")
            configured_url = account_url(configured_url)
            requested = self.spec.get("dataflow_storage_id") or self.options.get("dataflow_storage_id")
            if requested is None and self.control:
                requested = self.control["dataflow_storage_id"]
            selected = [
                a for a in accounts
                if workspace_id(a["id"]) == workspace_id(requested)
            ] if requested else [
                a for a in accounts if a["name"] == urlparse(configured_url).hostname.split(".")[0]
            ]
        if len(selected) != 1 or selected[0].get("isEnabled") is not True:
            raise ValueError(
                "An enabled, accessible Power BI storage registration is required. "
                "Register/enable ADLS once in the portal; this notebook cannot register an ARM account."
            )
        selected_id = workspace_id(selected[0]["id"])
        actual_url = account_url(f"https://{selected[0]['name']}.blob.core.windows.net")
        effective = {**self.spec, "staging_account_url": actual_url}
        self.record["staging_account_url"] = actual_url
        if original:
            self.record["status"] = "unchanged"
            LOG.info("Preserving existing ADLS connection on %s", identifier)
            return effective
        if self.control and selected_id != self.control["dataflow_storage_id"]:
            raise ValueError("Temporary source connections must use the control workspace's registration.")
        if actual_url != configured_url:
            raise ValueError("Temporary storage ID does not match the configured staging account URL.")
        if self.spec.get("staging_folder", self.ws["displayName"]) != self.ws["displayName"]:
            raise ValueError("Temporary attachment must use the service-created workspace-name folder.")
        if self.native_folder_exists(actual_url):
            raise ValueError(
                "An existing native backup folder can block reattachment. Move or rename the "
                "workspace's old power-bi-backup folder after reviewing retained ABFs, then retry. "
                "No storage connection or files were changed."
            )
        self.record.update(
            temporary_storage_id=selected_id, status="attach_requested", cleanup_status="pending",
            restore_dataflow_storage_id=EMPTY_GUID,
        )
        # Persist recovery intent before any remote mutation.
        self.journal("connection-before.json", self.record)
        if self.control and self.api.storage_id(self.control["id"]) != self.control["dataflow_storage_id"]:
            raise RuntimeError("Control workspace ADLS connection changed; refusing source attachment.")
        if self.api.storage_id(identifier) is not None:
            raise RuntimeError("Workspace ADLS connection changed during preflight; refusing attachment.")
        self.attempted = True
        try:
            self.api.assign_storage(identifier, selected_id)
        except BaseException:
            self.record["attachment_outcome_uncertain"] = True
            raise
        self.wait(lambda: self.api.storage_id(identifier) == selected_id, "workspace ADLS attachment")
        self.record["status"] = "attached"

        self.wait(lambda: self.native_folder_exists(actual_url), "service-created native backup folder")
        return effective

    def restore(self) -> None:
        if not self.attempted:
            return
        identifier = self.ws["id"]
        current = self.api.storage_id(identifier)
        if current is None and (
            self.record.get("attachment_outcome_uncertain")
            or self.record["status"] == "attach_requested"
        ):
            raise RuntimeError(
                "Attachment was not confirmed. Workspace currently reports no ADLS connection, "
                "but a delayed assignment cannot be ruled out; inspect the recovery record and "
                "recheck the workspace before retrying."
            )
        if current is not None:
            if current != self.record["temporary_storage_id"]:
                raise RuntimeError("ADLS connection changed externally; refusing to overwrite it.")
            if self.uncertain_backup:
                raise RuntimeError(
                    "Native XMLA backup outcome is uncertain. ADLS remains attached: confirm the "
                    "server-side operation has stopped before disconnecting manually."
                )
            if self.api.dataflows(identifier):
                raise RuntimeError(
                    "Gen1 dataflows appeared during backup. ADLS remains attached; no dataflows were deleted."
                )
            if self.api.storage_id(identifier) != current:
                raise RuntimeError("ADLS connection changed during cleanup; refusing to detach it.")
            self.api.assign_storage(identifier, None)
            self.wait(lambda: self.api.storage_id(identifier) is None, "original disconnected state")
        self.record.update(status="restored", cleanup_status="succeeded")
        LOG.info("Restored original disconnected ADLS state on %s", identifier)


class BlobStore:
    def __init__(self, credential):
        self.credential = credential
        self._clients = {}

    def client(self, location: dict):
        from azure.storage.blob import BlobServiceClient

        url = account_url(location["account_url"])
        if url not in self._clients:
            self._clients[url] = BlobServiceClient(
                url, credential=self.credential, retry_total=5,
                max_block_size=UPLOAD_BLOCK_SIZE, max_single_put_size=UPLOAD_BLOCK_SIZE,
                connection_timeout=15, read_timeout=120,
            )
        return self._clients[url].get_blob_client(
            container=location["container"], blob=location["blob"],
        )

    def check_container(self, location: dict) -> None:
        from azure.storage.blob import BlobServiceClient

        with BlobServiceClient(
            account_url(location["account_url"]), credential=self.credential,
            connection_timeout=15, read_timeout=120, retry_total=5,
        ) as client:
            client.get_container_client(location["container"]).get_container_properties()

    def check_native_folder(self, location: dict) -> None:
        self.client(location).get_blob_properties()

    def properties(self, location: dict) -> dict:
        p = self.client(location).get_blob_properties()
        return {"size": p.size, "etag": p.etag}

    def chunks(self, location: dict, etag: str):
        from azure.core import MatchConditions

        return self.client(location).download_blob(
            etag=etag, match_condition=MatchConditions.IfNotModified,
            max_concurrency=1, validate_content=True,
        ).chunks()

    def upload(self, location: dict, chunks, size: int) -> None:
        from azure.core import MatchConditions

        if size <= 0 or size > UPLOAD_BLOCK_SIZE * 50000:
            raise ValueError("ABF size must fit between one byte and 50,000 upload blocks.")
        client = self.client(location)
        upload_id = uuid4().hex
        blocks = []
        buffer = bytearray()
        transferred = 0

        def stage() -> None:
            block_id = b64encode(f"{upload_id}-{len(blocks):05d}".encode("ascii")).decode("ascii")
            # MD5 validation/retries require a replayable payload, not the source iterator.
            client.stage_block(
                block_id, bytes(buffer), length=len(buffer), validate_content=True,
            )
            blocks.append(block_id)
            buffer.clear()

        for chunk in chunks:
            transferred += len(chunk)
            if transferred > size:
                raise ValueError("ABF stream exceeds its declared size; upload was not committed.")
            remaining = memoryview(chunk)
            while remaining:
                length = min(UPLOAD_BLOCK_SIZE - len(buffer), len(remaining))
                buffer.extend(remaining[:length])
                remaining = remaining[length:]
                if len(buffer) == UPLOAD_BLOCK_SIZE:
                    stage()
        if transferred != size:
            raise ValueError("ABF stream was truncated; upload was not committed.")
        if buffer:
            stage()
        client.commit_block_list(blocks, match_condition=MatchConditions.IfMissing)

    def put_json(self, location: dict, document: dict) -> None:
        from azure.storage.blob import ContentSettings

        self.client(location).upload_blob(
            json.dumps(document, ensure_ascii=True, indent=2).encode("utf-8"),
            overwrite=False, content_settings=ContentSettings(content_type="application/json"),
        )

    def get_json(self, location: dict) -> dict:
        properties = self.properties(location)
        if properties["size"] > 32 * 1024 * 1024:
            raise ValueError("Manifest exceeds the 32 MiB safety limit.")
        return json.loads(b"".join(self.chunks(location, properties["etag"])))

    def close(self) -> None:
        for client in self._clients.values():
            client.close()


def digest_blob(store, location: dict, properties: dict) -> str:
    digest = hashlib.sha256()
    size = 0
    for chunk in store.chunks(location, properties["etag"]):
        digest.update(chunk)
        size += len(chunk)
    if size != properties["size"]:
        raise ValueError("Blob length changed or download was truncated.")
    return digest.hexdigest()


def verified_copy(store, source: dict, destination: dict, expected: dict | None = None) -> dict:
    source_properties = store.properties(source)
    if source_properties["size"] <= 0:
        raise ValueError("Refusing an empty ABF backup.")
    if expected is not None and (
        source_properties["size"] != expected["size"]
        or source_properties["etag"] != expected["etag"]
    ):
        raise ValueError("Archive blob no longer matches its manifest size/ETag.")
    digest = hashlib.sha256()
    transferred = 0

    def chunks():
        nonlocal transferred
        for chunk in store.chunks(source, source_properties["etag"]):
            digest.update(chunk)
            transferred += len(chunk)
            yield chunk

    store.upload(destination, chunks(), source_properties["size"])
    checksum = digest.hexdigest()
    if transferred != source_properties["size"]:
        raise ValueError("ABF transfer was truncated.")
    if expected is not None and checksum != expected["sha256"]:
        raise ValueError("Archive SHA-256 mismatch; no restore was executed.")
    saved = store.properties(destination)
    if saved["size"] != transferred or digest_blob(store, destination, saved) != checksum:
        raise ValueError("Destination ABF verification failed.")
    return {**saved, "sha256": checksum}


class NativeXmla:
    def __init__(self, credential):
        self.credential = credential

    def backup(self, ws: str, model: dict, filename: str, password: str | None) -> None:
        import sempy.fabric as fabric

        namespace = "http://schemas.microsoft.com/analysisservices/2003/engine"
        command = ET.Element("Backup", xmlns=namespace)
        obj = ET.SubElement(command, "Object")
        ET.SubElement(obj, "DatabaseID").text = model["id"]
        ET.SubElement(command, "File").text = filename
        ET.SubElement(command, "ApplyCompression").text = "true"
        ET.SubElement(command, "AllowOverwrite").text = "false"
        if password is not None:
            ET.SubElement(command, "Password").text = password
        fabric.execute_xmla(
            dataset=UUID(model["id"]), workspace=UUID(ws),
            xmla_command=ET.tostring(command, encoding="unicode"),
            use_readwrite_connection=True, credential=self.credential,
        )

    def restore(
        self, ws: str, name: str, filename: str, password: str | None, overwrite: bool,
    ) -> None:
        import sempy.fabric as fabric

        command = {
            "database": name, "file": filename, "allowOverwrite": overwrite,
            "security": "skipMembership",
        }
        if password is not None:
            command["password"] = password
        fabric.execute_tmsl(
            script={"restore": command}, workspace=UUID(ws),
            credential=self.credential,
        )


class BackupRestore:
    def __init__(
        self, config: dict, api, store, native, secrets: SecretResolver, powerbi=None,
        control_workspace_id: str | None = None,
    ):
        self.config = config
        self.api = api
        self.store = store
        self.native = native
        self.secrets = secrets
        self.powerbi = powerbi
        self.control_workspace_id = control_workspace_id
        self.archive = config["archive"]
        account_url(self.archive["account_url"])
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])", self.archive["container"]):
            raise ValueError("Invalid archive container name.")
        if "--" in self.archive["container"]:
            raise ValueError("Invalid archive container name.")
        blob_path(self.archive["prefix"], "archive prefix")

    def archive_location(self, path: str) -> dict:
        return {
            "account_url": self.archive["account_url"], "container": self.archive["container"],
            "blob": f"{self.archive['prefix']}/{path}",
        }

    def staging(self, ws: dict, spec: dict, filename: str) -> dict:
        folder = blob_path(spec.get("staging_folder", ws["displayName"]), "staging_folder")
        url = spec.get("staging_account_url", self.config.get("staging_account_url"))
        return {
            "account_url": account_url(url), "container": NATIVE_CONTAINER,
            "blob": f"{folder}/{filename}",
        }

    def failure(self, record: dict, exc: Exception) -> None:
        record.update(status="failed", error=self.secrets.error(exc))
        LOG.error("%s: %s", record.get("id", "operation"), record["error"])

    def backup_summary(self, manifest: dict) -> str:
        workspaces = manifest["workspaces"]
        models = [m for w in workspaces for m in w["models"]]
        total_workspaces = len(self.config["backup"]["workspaces"])
        summary = {
            "models_backed_up": sum(m["status"] == "succeeded" for m in models),
            "models_failed": sum(m["status"] == "failed" for m in models),
            "models_attempted": len(models),
            "workspaces_total": total_workspaces,
            "workspaces_succeeded": sum(w["status"] == "succeeded" for w in workspaces),
            "workspaces_failed": sum(w["status"] == "failed" for w in workspaces),
            "workspaces_not_attempted": total_workspaces - len(workspaces),
            "workspaces_without_model_inventory": total_workspaces - sum(
                w["model_discovery_completed"] for w in workspaces
            ),
            "workspace_cleanup_failures": sum(
                bool(w.get("storage_cleanup_error") or w.get("storage_journal_error"))
                for w in workspaces
            ),
        }
        manifest["summary"] = summary
        location = self.archive_location(f"runs/{manifest['run_id']}/manifest.json")
        return (
            f"Backup run {manifest['run_id']}: "
            f"{summary['models_backed_up']} semantic models backed up, "
            f"{summary['models_failed']} failed ({summary['models_attempted']} attempted). "
            f"Workspaces: {summary['workspaces_succeeded']} succeeded, "
            f"{summary['workspaces_failed']} failed, "
            f"{summary['workspaces_not_attempted']} not attempted; "
            f"{summary['workspaces_without_model_inventory']} without a complete model inventory; "
            f"{summary['workspace_cleanup_failures']} with cleanup/recovery failures. "
            f"Manifest: {location['account_url']}/{location['container']}/{location['blob']}"
        )

    def control_registration(self, record: dict) -> list[dict]:
        identifier = workspace_id(required_string(
            self.control_workspace_id, "notebook runtime currentWorkspaceId",
        ))
        record.update(id=identifier, status="checking")
        storage_id = self.powerbi.storage_id(identifier)
        record["dataflow_storage_id"] = storage_id
        if storage_id is None:
            raise ValueError(
                f"One-time control workspace setup required: connect ADLS in the workspace "
                f"running this notebook ({identifier}) and leave it connected. "
                "Use that workspace's Azure connections, NOT tenant-level settings. "
                "No source workspaces were changed."
            )
        accounts = self.powerbi.storage_accounts()
        selected = [a for a in accounts if workspace_id(a["id"]) == storage_id]
        if len(selected) != 1 or selected[0].get("isEnabled") is not True:
            raise ValueError(
                "The notebook control workspace's Power BI storage registration is not enabled "
                "or accessible. Repair its persistent workspace-level connection first. "
                "No source workspaces were changed."
            )
        record.update(
            status="preserved",
            account_url=account_url(f"https://{selected[0]['name']}.blob.core.windows.net"),
        )
        LOG.info("Persistent control workspace: %s; storage registration: %s", identifier, storage_id)
        return accounts

    def backup(self) -> dict:
        options = self.config["backup"]
        temporary = options.get("temporary_adls", {})
        if not isinstance(temporary, dict) or type(temporary.get("enabled", False)) is not bool:
            raise ValueError("backup.temporary_adls.enabled must be a boolean.")
        if temporary.get("enabled", False):
            timeout = temporary.get("timeout_seconds", 300)
            if type(timeout) is not int or not 1 <= timeout <= 1800:
                raise ValueError("temporary_adls.timeout_seconds must be an integer from 1 to 1800.")
            if self.powerbi is None:
                raise ValueError("Temporary ADLS requires notebook-user Power BI connection management.")
        specs = options["workspaces"]
        if not specs:
            raise ValueError("Configure at least one backup workspace.")
        identifiers = [workspace_id(s["id"]) for s in specs]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Duplicate backup workspace IDs.")
        password = (
            self.secrets.resolve(options["password"], "backup password")
            if options.get("password") is not None else None
        )
        run_id = new_run_id()
        manifest = {
            "schema_version": 1, "kind": "native_abf", "run_id": run_id,
            "started_at": utc_now(), "status": "running", "workspaces": [],
        }
        self.store.check_container(self.archive)
        # Immutable start record makes interrupted runs discoverable, but not restorable.
        self.store.put_json(self.archive_location(f"runs/{run_id}/started.json"), manifest)
        LOG.info("Backup run: %s", run_id)
        control = None
        accounts = None
        if temporary.get("enabled", False):
            control = {"id": self.control_workspace_id, "status": "checking"}
            manifest["control_workspace"] = control
            try:
                accounts = self.control_registration(control)
            except Exception as exc:
                control["status"] = "failed"
                self.failure(manifest, exc)
                manifest["completed_at"] = utc_now()
                summary = self.backup_summary(manifest)
                print(summary, flush=True)
                self.store.put_json(self.archive_location(f"runs/{run_id}/manifest.json"), manifest)
                raise RuntimeError(
                    f"{summary} Control-workspace preflight: {manifest['error']}"
                ) from None
        for spec, identifier in zip(specs, identifiers):
            workspace_record = {
                "id": identifier, "status": "running", "models": [],
                "model_discovery_completed": False,
            }
            manifest["workspaces"].append(workspace_record)
            connection = None

            def journal(filename, document):
                self.store.put_json(
                    self.archive_location(f"runs/{run_id}/workspaces/{identifier}/{filename}"),
                    {"workspace_id": identifier, "recorded_at": utc_now(), **document},
                )

            try:
                ws = self.api.workspace(identifier)
                workspace_record["name"] = ws["displayName"]
                if temporary.get("enabled", False):
                    connection = TemporaryAdls(
                        self.powerbi, self.store, ws, spec,
                        control["account_url"], temporary, journal, control, accounts,
                    )
                    workspace_record["storage_connection"] = connection.record
                    spec = connection.connect()
                stage = self.staging(ws, spec, "preflight.abf")
                self.store.check_container(stage)
                models = self.api.models(identifier)
                workspace_record["model_discovery_completed"] = True
                for model in models:
                    record = {
                        "id": model["id"], "name": model["displayName"], "status": "running",
                        "password_required": password is not None,
                    }
                    workspace_record["models"].append(record)
                    try:
                        filename = f"{run_id}-{workspace_id(model['id'])}.abf"
                        source = self.staging(ws, spec, filename)
                        destination = self.archive_location(
                            f"runs/{run_id}/workspaces/{identifier}/{model['id']}.abf"
                        )
                        try:
                            self.native.backup(identifier, model, filename, password)
                        except BaseException:
                            if connection is not None:
                                connection.uncertain_backup = True
                            raise
                        record.update(verified_copy(self.store, source, destination))
                        record.update(status="succeeded", blob=destination["blob"])
                        LOG.info("Backed up %s / %s", identifier, model["id"])
                    except Exception as exc:
                        # Failure isolation is intentional; the run still fails after all models.
                        self.failure(record, exc)
                workspace_record["status"] = (
                    "failed" if any(m["status"] == "failed" for m in workspace_record["models"])
                    else "succeeded"
                )
                workspace_record["backup_status"] = workspace_record["status"]
            except Exception as exc:
                self.failure(workspace_record, exc)
                workspace_record["backup_status"] = "failed"
            finally:
                if connection is not None:
                    try:
                        connection.restore()
                    except Exception as exc:
                        connection.record["cleanup_status"] = "failed"
                        connection.record["cleanup_error"] = self.secrets.error(exc)
                        workspace_record.update(
                            status="failed", storage_cleanup_error=self.secrets.error(exc),
                        )
                        LOG.error("ADLS cleanup for %s failed: %s", identifier, workspace_record["storage_cleanup_error"])
                    if connection.attempted:
                        try:
                            journal("connection-after.json", connection.record)
                        except Exception as exc:
                            workspace_record.update(
                                status="failed", storage_journal_error=self.secrets.error(exc),
                            )
                            LOG.error("ADLS recovery journal for %s failed: %s", identifier, workspace_record["storage_journal_error"])
        manifest["status"] = (
            "failed" if any(w["status"] == "failed" for w in manifest["workspaces"])
            else "succeeded"
        )
        manifest["completed_at"] = utc_now()
        summary = self.backup_summary(manifest)
        print(summary, flush=True)
        self.store.put_json(self.archive_location(f"runs/{run_id}/manifest.json"), manifest)
        if manifest["status"] == "failed":
            raise RuntimeError(summary)
        LOG.info("Backup completed: %s", run_id)
        return manifest

    def restore_plan(self, options: dict, manifest: dict) -> list[dict]:
        run_id = options["run_id"]
        if (
            manifest.get("schema_version") != 1
            or manifest.get("kind") != "native_abf"
            or manifest.get("run_id") != run_id
            or manifest.get("status") not in ("succeeded", "failed")
        ):
            raise ValueError("Unsupported, incomplete, or mismatched backup manifest.")
        sources = {w["id"]: w for w in manifest["workspaces"]}
        if len(sources) != len(manifest["workspaces"]):
            raise ValueError("Duplicate workspaces in manifest.")
        if not options["workspaces"]:
            raise ValueError("Configure at least one restore workspace mapping.")
        plan = []
        targets = set()
        for spec in options["workspaces"]:
            source_id = workspace_id(spec["source_workspace_id"])
            target_id = workspace_id(spec["target_workspace_id"])
            source = sources[source_id]
            if source.get("backup_status", source["status"]) != "succeeded":
                raise ValueError(f"Source workspace {source_id} has an incomplete/failed backup.")
            ws = self.api.workspace(target_id)
            available = {m["id"]: m for m in source["models"]}
            if len(available) != len(source["models"]):
                raise ValueError("Duplicate model IDs in manifest.")
            requested = spec.get("model_ids")
            selected = list(available) if requested is None else [workspace_id(m) for m in requested]
            if not selected or len(selected) != len(set(selected)):
                raise ValueError("Restore selection must be nonempty and contain no duplicate IDs.")
            renames = spec.get("model_names", {})
            if not set(renames).issubset(selected):
                raise ValueError("model_names contains IDs outside the selected models.")
            existing_models = self.api.models(target_id)
            existing = {}
            for model in existing_models:
                existing.setdefault(model["displayName"].casefold(), []).append(model["id"])
            for identifier in selected:
                model = available[identifier]
                if model["status"] != "succeeded":
                    raise ValueError(f"Model {identifier} was not successfully backed up.")
                name = required_string(renames.get(identifier, model["name"]), "target model name")
                key = (target_id, name.casefold())
                if key in targets:
                    raise ValueError("Multiple restore entries resolve to the same target model name.")
                targets.add(key)
                matches = existing.get(name.casefold(), [])
                if len(matches) > 1:
                    raise ValueError(f"Target model name {name!r} is ambiguous.")
                if matches and not options["allow_overwrite"]:
                    raise ValueError(f"Target model {name!r} exists; overwriting is disabled.")
                expected_blob = (
                    f"{self.archive['prefix']}/runs/{run_id}/workspaces/{source_id}/{identifier}.abf"
                )
                if model["blob"] != expected_blob or not re.fullmatch("[0-9a-f]{64}", model["sha256"]):
                    raise ValueError("Manifest contains an invalid archive blob path or checksum.")
                if type(model["size"]) is not int or model["size"] <= 0 or not model["etag"]:
                    raise ValueError("Manifest contains invalid blob size/ETag.")
                archive = {**self.archive, "blob": model["blob"]}
                properties = self.store.properties(archive)
                if properties != {"size": model["size"], "etag": model["etag"]}:
                    raise ValueError("Archive blob size/ETag differs from the manifest.")
                stage = self.staging(ws, spec, f"restore-{uuid4().hex}.abf")
                self.store.check_container(stage)
                plan.append({
                    "source_workspace_id": source_id, "target_workspace_id": target_id,
                    "model_id": identifier, "name": name, "model": model,
                    "archive": archive, "stage": stage,
                })
        return plan

    def restore(self) -> dict:
        options = self.config["restore"]
        for flag in ("dry_run", "allow_overwrite"):
            if type(options[flag]) is not bool:
                raise ValueError(f"restore.{flag} must be a boolean.")
        run_id = blob_path(options["run_id"], "restore run_id")
        if "/" in run_id:
            raise ValueError("restore.run_id cannot contain path separators.")
        manifest = self.store.get_json(self.archive_location(f"runs/{run_id}/manifest.json"))
        plan = self.restore_plan(options, manifest)
        password = (
            self.secrets.resolve(options["password"], "restore password")
            if options.get("password") is not None else None
        )
        if any(p["model"]["password_required"] for p in plan) and password is None:
            raise ValueError("Selected backup requires restore.password.")
        report = {
            "schema_version": 1, "backup_run_id": run_id, "restore_run_id": new_run_id(),
            "started_at": utc_now(), "dry_run": options["dry_run"], "models": [],
        }
        # Finish the entire preflight before performing any restore or staging upload.
        for item in plan:
            record = {
                "source_workspace_id": item["source_workspace_id"],
                "target_workspace_id": item["target_workspace_id"],
                "id": item["model_id"], "name": item["name"], "status": "planned",
            }
            report["models"].append(record)
            if options["dry_run"]:
                LOG.info("Would restore %s to %s as %s", record["id"], record["target_workspace_id"], record["name"])
                continue
            try:
                verified_copy(self.store, item["archive"], item["stage"], expected=item["model"])
                self.native.restore(
                    item["target_workspace_id"], item["name"],
                    item["stage"]["blob"].rsplit("/", 1)[1], password, options["allow_overwrite"],
                )
                restored = [
                    m for m in self.api.models(item["target_workspace_id"])
                    if m["displayName"].casefold() == item["name"].casefold()
                ]
                if len(restored) != 1:
                    raise RuntimeError("XMLA returned but restored model discovery was not unambiguous.")
                record.update(status="succeeded", restored_model_id=restored[0]["id"])
                LOG.info("Restored %s to %s", record["id"], record["target_workspace_id"])
            except Exception as exc:
                self.failure(record, exc)
        report["status"] = (
            "failed" if any(m["status"] == "failed" for m in report["models"])
            else "dry_run" if options["dry_run"] else "succeeded"
        )
        report["completed_at"] = utc_now()
        # Dry runs are read-only, including archive storage.
        if not options["dry_run"]:
            self.store.put_json(
                self.archive_location(f"restores/{report['restore_run_id']}/report.json"), report,
            )
        if report["status"] == "failed":
            raise RuntimeError(f"Restore {report['restore_run_id']} has failures; see its report.")
        return report


def run(config: dict) -> dict:
    """Entrypoint for Fabric; cloud dependencies are loaded only during execution."""
    from azure.identity import ClientSecretCredential

    mode = config["mode"]
    if mode not in ("backup", "restore"):
        raise ValueError("mode must be 'backup' or 'restore'.")
    principal = config["service_principal"]
    needs_vault = any(isinstance(v, dict) for v in principal.values())
    options = config[mode]
    needs_vault = needs_vault or isinstance(options.get("password"), dict)
    get_secret = None
    if needs_vault:
        from notebookutils import credentials

        get_secret = credentials.getSecret
    secrets = SecretResolver(get_secret)
    credential = ClientSecretCredential(
        tenant_id=secrets.resolve(principal["tenant_id"], "tenant_id"),
        client_id=secrets.resolve(principal["client_id"], "client_id"),
        client_secret=secrets.resolve(principal["client_secret"], "client_secret"),
    )
    store = BlobStore(credential)
    api = FabricApi(credential)
    powerbi = None
    control_workspace_id = None
    try:
        temporary = config.get("backup", {}).get("temporary_adls", {})
        if mode == "backup" and isinstance(temporary, dict) and temporary.get("enabled") is True:
            from notebookutils import runtime

            control_workspace_id = workspace_id(required_string(
                runtime.context["currentWorkspaceId"], "notebook runtime currentWorkspaceId",
            ))

            def management_token():
                from notebookutils import credentials

                return credentials.getToken("pbi")

            powerbi = PowerBiApi(management_token)
        engine = BackupRestore(
            config, api, store, NativeXmla(credential), secrets, powerbi,
            control_workspace_id=control_workspace_id,
        )
        return engine.backup() if mode == "backup" else engine.restore()
    except Exception as exc:
        raise RuntimeError(secrets.error(exc)) from None
    finally:
        store.close()
        api.session.close()
        if powerbi is not None:
            powerbi.session.close()
        credential.close()
