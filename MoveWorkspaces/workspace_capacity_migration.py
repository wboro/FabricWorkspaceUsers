"""Fail-closed workspace capacity reassignment, embedded in the Fabric notebook."""

import base64
from datetime import datetime, timezone
import hashlib
import json
import logging
import re
import time
from urllib.parse import urlencode, urljoin, urlparse
from uuid import UUID, uuid4


LOG = logging.getLogger("workspace_capacity_migration")
FABRIC = "https://api.fabric.microsoft.com/v1/"
POWERBI = "https://api.powerbi.com/v1.0/myorg/"
SUPPORTED_ITEMS = {"Report", "PaginatedReport", "SemanticModel", "Dashboard"}
P_TO_F = {"P1": 64, "P2": 128, "P3": 256, "P4": 512, "P5": 1024}


class SafetyError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise SafetyError(message)


def guid(value):
    result = str(UUID(value))
    require(result != "00000000-0000-0000-0000-000000000000", "Empty GUID is not a capacity or workspace.")
    return result


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def instant(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None, "Timestamp must include a timezone.")
    return parsed.astimezone(timezone.utc)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def region(value):
    require(isinstance(value, str) and bool(value.strip()), "Capacity region is unknown.")
    return re.sub(r"[^a-z0-9]", "", value.lower())


def claims(token):
    # Used only to guard against wrong-tenant execution, not as a replacement for API authentication.
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


class Api:
    def __init__(self, token_provider, session=None, sleep=time.sleep):
        if session is None:
            import requests
            session = requests.Session()
        self.token_provider, self.session, self.sleep = token_provider, session, sleep

    def request(self, method, path, body=None):
        require(any(path.startswith(base) for base in (FABRIC, POWERBI)), "Untrusted API URL.")
        for attempt in range(6):
            response = self.session.request(
                method, path, json=body,
                headers={"Authorization": "Bearer " + self.token_provider(path)},
                timeout=(15, 120), allow_redirects=False,
            )
            if method == "GET" and response.status_code in (429, 500, 502, 503, 504) and attempt < 5:
                delay = response.headers.get("Retry-After", "")
                self.sleep(min(int(delay), 300) if delay.isdigit() else 2 ** attempt)
                continue
            require(response.status_code in (200, 201, 202, 204),
                    f"{method} {urlparse(path).path}: HTTP {response.status_code}; "
                    f"request ID {response.headers.get('request-id', 'unavailable')}. "
                    "No automatic mutation retry; inspect the journal and service state.")
            return response
        raise AssertionError("Unreachable retry state")

    def get(self, url):
        return self.request("GET", url).json()

    def listing(self, url):
        initial, result, seen = url, [], set()
        while url:
            require(url not in seen, "Repeated pagination link; inventory is incomplete.")
            seen.add(url)
            page = self.get(url)
            require(isinstance(page.get("value"), list), "Inventory response has no value list.")
            result.extend(page["value"])
            url = page.get("continuationUri") or page.get("@odata.nextLink") or page.get("odata.nextLink")
            if not url and page.get("continuationToken"):
                url = initial + ("&" if "?" in initial else "?") + urlencode({"continuationToken": page["continuationToken"]})
            if url:
                url = urljoin(initial, url)
        return result


class NotebookCredential:
    def get_token(self, *scopes, **kwargs):
        from azure.core.credentials import AccessToken
        import notebookutils

        audience = "storage" if scopes[0].startswith("https://storage.azure.com") else "pbi"
        token = notebookutils.credentials.getToken(audience)
        return AccessToken(token, int(claims(token)["exp"]))


class BlobArchive:
    def __init__(self, config, credential):
        from azure.storage.blob import BlobServiceClient

        url = config["account_url"]
        require(bool(re.fullmatch(r"https://[a-z0-9]{3,24}\.blob\.core\.windows\.net", url)),
                "Use a public Azure Blob account URL without SAS or path.")
        require(bool(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])", config["container"]))
                and "--" not in config["container"], "Invalid archive container.")
        self.container = BlobServiceClient(url, credential=credential).get_container_client(config["container"])
        self.config = config

    def client(self, path):
        require(isinstance(path, str) and "\\" not in path
                and all(p not in ("", ".", "..") for p in path.split("/")), "Invalid archive blob path.")
        return self.container.get_blob_client(path)

    def read_manifest(self, path):
        from azure.core import MatchConditions

        blob = self.client(path)
        properties = blob.get_blob_properties()
        require(0 < properties.size <= 20 * 1024 * 1024, "Manifest is empty or unexpectedly large.")
        raw = blob.download_blob(etag=properties.etag, match_condition=MatchConditions.IfNotModified).readall()
        return json.loads(raw)

    def verify_model(self, model):
        from azure.core import MatchConditions

        blob = self.client(model["blob"])
        properties = blob.get_blob_properties()
        require(properties.size == model["size"] and properties.etag == model["etag"],
                "Backup size/ETag differs from the SemanticModelBackup manifest.")
        checksum, size = hashlib.sha256(), 0
        for chunk in blob.download_blob(etag=properties.etag, match_condition=MatchConditions.IfNotModified).chunks():
            checksum.update(chunk)
            size += len(chunk)
        require(size == model["size"] and size > 0 and checksum.hexdigest() == model["sha256"],
                "Backup ABF SHA-256 verification failed.")

    def journal(self, run_id, sequence, document):
        prefix = self.config["journal_prefix"].strip("/")
        path = f"{prefix}/{run_id}/{sequence:05d}.json"
        self.client(path).upload_blob(json.dumps(document, indent=2).encode(), overwrite=False)
        return f"{self.config['account_url']}/{self.config['container']}/{path}"


class ModelProbe:
    def __init__(self, credential=None):
        self.credential = credential

    def snapshot(self, workspace_id, model_id):
        import sempy.fabric as fabric

        with fabric.connect_semantic_model(dataset=UUID(model_id), workspace=UUID(workspace_id),
                                           readonly=True, credential=self.credential) as tom:
            database = tom.model.Database
            require(str(database.ID).lower() == model_id, "XMLA resolved an unexpected model ID.")
            require(str(tom.model.DefaultMode) == "Import", "Only Import-default models are certified.")
            schema = {
                "tables": [], "relationships": [],
                "roles": sorted((
                    str(r.Name), str(r.ModelPermission),
                    sorted((str(p.Table.Name), str(p.FilterExpression)) for p in r.TablePermissions),
                    sorted(str(m.MemberName) for m in r.Members),
                ) for r in tom.model.Roles),
            }
            counts = {}
            timestamps = [database.LastSchemaUpdate, database.LastProcessed, tom.model.ModifiedTime]
            for table in tom.model.Tables:
                timestamps.append(table.ModifiedTime)
                partitions = []
                for partition in table.Partitions:
                    timestamps.extend([partition.ModifiedTime, partition.RefreshedTime])
                    mode = str(partition.Mode)
                    source_type = str(partition.Source.GetType().Name) if partition.Source is not None else ""
                    require(mode in {"Import", "Default"} and source_type in {
                        "MPartitionSource", "QueryPartitionSource", "CalculatedPartitionSource",
                    }, f"Model {model_id}: Direct Lake, DirectQuery, streaming or unknown partition is not certified safe.")
                    require(str(partition.State) == "Ready", f"Model {model_id}: partition is not Ready.")
                    partitions.append((str(partition.Name), mode, source_type))
                require(bool(partitions), f"Model {model_id}: table has no partitions.")
                schema["tables"].append({
                    "name": str(table.Name),
                    "columns": sorted((str(c.Name), str(c.DataType),
                                       str(getattr(c, "SourceColumn", "")), str(getattr(c, "Expression", "")))
                                      for c in table.Columns),
                    "measures": sorted((str(m.Name), str(m.Expression)) for m in table.Measures),
                    "partitions": sorted(partitions),
                })
                name = str(table.Name)
                frame = fabric.evaluate_dax(
                    dataset=UUID(model_id), workspace=UUID(workspace_id),
                    dax_string="EVALUATE ROW(\"Rows\", COALESCE(COUNTROWS('" + name.replace("'", "''") + "'), 0))",
                    credential=self.credential,
                )
                require(frame.shape == (1, 1), "Row-count query returned an unexpected shape.")
                value = frame.iloc[0, 0]
                counts[name] = 0 if value is None else int(value)
            require(bool(counts), f"Model {model_id}: no readable tables.")
            schema["tables"].sort(key=lambda x: x["name"])
            for relationship in tom.model.Relationships:
                require(str(relationship.GetType().Name) == "SingleColumnRelationship",
                        "Uncertified relationship type.")
                schema["relationships"].append((
                    str(relationship.Name), str(relationship.FromTable.Name), str(relationship.FromColumn.Name),
                    str(relationship.ToTable.Name), str(relationship.ToColumn.Name), bool(relationship.IsActive),
                    str(relationship.CrossFilteringBehavior), str(relationship.SecurityFilteringBehavior),
                ))
            schema["relationships"].sort()
            latest = max(t.ToUniversalTime().ToString("o") for t in timestamps)
            return {"schema_hash": digest(schema), "rows": counts, "last_change": latest,
                    "actual_storage": str(database.StorageEngineUsed)}


def capacity_rules(workspace, source, target, control_workspace_id):
    require(workspace["id"] != control_workspace_id, "Cannot migrate the notebook's control workspace.")
    require(workspace.get("type") == "Workspace", "Personal/system workspaces are not supported.")
    require(workspace.get("capacityAssignmentProgress") == "Completed",
            "Workspace has failed, pending or unknown capacity assignment state.")
    require(workspace.get("capacityId") == source["id"], "Source capacity changed or is unknown.")
    for capacity in (source, target):
        require(capacity.get("state") == "Active", "Both capacities must be Active.")
        region(capacity.get("region"))
    require(source["id"] != target["id"], "Workspace is already assigned to the target; use validate mode.")
    source_sku, target_sku = source.get("sku"), target.get("sku", "")
    require(bool(re.fullmatch(r"F(?:2|4|8|16|32|64|128|256|512|1024|2048)", target_sku)),
            "Target must be a recognized, paid Fabric F SKU.")
    source_units = P_TO_F.get(source_sku)
    if source_units is None and isinstance(source_sku, str) and re.fullmatch(r"F\d+", source_sku):
        source_units = int(source_sku[1:])
    require(source_units is not None, "Only P-to-F and F-to-F moves are supported.")
    require(int(target_sku[1:]) >= source_units, "Target is smaller than the source SKU equivalent; blocked.")
    return region(source["region"]) != region(target["region"])


class Migrator:
    def __init__(self, config, api, probe, archive=None, control_workspace_id=None,
                 sleep=time.sleep, clock=time.monotonic):
        self.config, self.api, self.probe, self.archive = config, api, probe, archive
        self.control_workspace_id = control_workspace_id
        self.sleep, self.clock = sleep, clock
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
        self.report = {"run_id": self.run_id, "started_at": utc_now(), "status": "running", "workspaces": []}
        self.sequence = 0

    def save(self):
        if self.archive:
            self.sequence += 1
            self.report["journal"] = self.archive.journal(self.run_id, self.sequence, self.report)
        elif not self.config["dry_run"]:
            raise SafetyError("Live migration/validation requires a durable external journal.")

    def inventory(self, ws):
        root = FABRIC + f"workspaces/{ws}/"
        pbi = POWERBI + f"groups/{ws}/"
        result = {
            "items": self.api.listing(root + "items?recursive=true"),
            "datasets": self.api.listing(pbi + "datasets"),
            "reports": self.api.listing(pbi + "reports"),
            "dashboards": self.api.listing(pbi + "dashboards"),
            "dataflows": self.api.listing(pbi + "dataflows"),
            "roles": self.api.listing(root + "roleAssignments"),
        }
        require(not result["dataflows"], "Dataflow Gen1/Gen2 is not supported by this strict safety profile.")
        unsupported = sorted({i.get("type", "UNKNOWN") for i in result["items"]} - SUPPORTED_ITEMS)
        require(not unsupported, "Uncertified item types (never deleted): " + ", ".join(unsupported))
        for key in ("items", "datasets", "reports", "dashboards", "roles"):
            ids = [x["id"] for x in result[key]]
            require(len(ids) == len(set(ids)), "Duplicate IDs in inventory.")
        core_models = {i["id"] for i in result["items"] if i["type"] == "SemanticModel"}
        require(core_models == {m["id"] for m in result["datasets"]}, "Fabric/Power BI model inventories disagree.")
        core_reports = {i["id"] for i in result["items"] if i["type"] in {"Report", "PaginatedReport"}}
        require(core_reports == {r["id"] for r in result["reports"]}, "Fabric/Power BI report inventories disagree.")
        for model in result["datasets"]:
            require(model.get("addRowsAPIEnabled") is False, "Push/streaming or unknown model kind is blocked.")
        return result

    def refresh_idle(self, ws, model):
        history = self.api.listing(POWERBI + f"groups/{ws}/datasets/{model}/refreshes?$top=10")
        require(all(r.get("status") in {"Completed", "Failed", "Disabled", "Cancelled"} for r in history),
                f"Model {model}: active or unknown refresh state; move would cancel jobs.")

    def snapshot(self, ws, inventory, cross_region):
        result = {}
        for entry in inventory["datasets"]:
            model_id = guid(entry["id"])
            root = POWERBI + f"groups/{ws}/datasets/{model_id}"
            model = self.api.get(root)
            require(model.get("addRowsAPIEnabled") is False, "Push/streaming or unknown model type.")
            storage = model.get("targetStorageMode")
            require(storage in {"Abf", "PremiumFiles"}, "Missing/unknown per-model storage format.")
            if cross_region:
                require(storage == "Abf", "Large-storage semantic models cannot move across regions.")
            self.refresh_idle(ws, model_id)
            data = self.probe.snapshot(ws, model_id)
            require(data["actual_storage"] in {"InMemory", "TabularMetadata"},
                    "Unknown semantic-model storage engine.")
            data.update({
                "name": model["name"], "storage": storage,
                "datasources": self.api.listing(root + "/datasources"),
                "refresh_schedule": self.api.get(root + "/refreshSchedule"),
            })
            result[model_id] = data
        return result

    def verify_backups(self, spec, snapshots):
        if not snapshots:
            return None
        require(self.archive is not None, "Configure archive access to verify native ABF recovery backups.")
        manifest_path = spec.get("backup_manifest_blob", "")
        require(bool(manifest_path), "Select an explicit SemanticModelBackup manifest, never latest.")
        manifest = self.archive.read_manifest(manifest_path)
        require(manifest.get("schema_version") == 1 and manifest.get("kind") == "native_abf"
                and manifest.get("status") == "succeeded", "Backup run is incomplete, failed or unsupported.")
        require(manifest_path.endswith(f"/runs/{manifest['run_id']}/manifest.json"),
                "Manifest path does not match its explicit backup run ID.")
        completed = instant(manifest["completed_at"])
        age = (datetime.now(timezone.utc) - completed).total_seconds()
        require(0 <= age <= self.config["backup_max_age_hours"] * 3600, "Backup is stale or future-dated.")
        workspaces = [w for w in manifest["workspaces"] if w["id"] == spec["workspace_id"]]
        require(len(workspaces) == 1, "Backup manifest does not uniquely match the source workspace.")
        workspace = workspaces[0]
        require(workspace["status"] == "succeeded" and workspace.get("model_discovery_completed") is True,
                "Backup model discovery or workspace backup did not finish.")
        models = {m["id"]: m for m in workspace["models"]}
        require(len(models) == len(workspace["models"]) and set(models) == set(snapshots),
                "Backup model inventory does not exactly match the current workspace.")
        started = instant(manifest["started_at"])
        require(started <= completed, "Invalid backup time range.")
        for model_id, snapshot in snapshots.items():
            model = models[model_id]
            require(model["status"] == "succeeded" and model["name"] == snapshot["name"],
                    "Backup failed or model name changed.")
            require(instant(snapshot["last_change"]) <= started,
                    "Model changed since backup began; take a new backup under a write freeze.")
            require(bool(re.fullmatch(r"[0-9a-f]{64}", model["sha256"]))
                    and type(model["size"]) is int and model["size"] > 0, "Invalid backup checksum or size.")
            expected_blob = manifest_path.rsplit("/", 1)[0] + f"/workspaces/{spec['workspace_id']}/{model_id}.abf"
            require(model["blob"] == expected_blob,
                    "Backup ABF path does not match the selected workspace/model.")
            self.archive.verify_model(model)
        return {"manifest": manifest_path, "run_id": manifest["run_id"], "verified_models": len(models)}

    def approvals(self, spec, cross_region):
        approvals = spec.get("approvals", {})
        for name in ("exclusive_write_freeze", "source_capacity_retained", "capacity_settings_and_headroom_reviewed",
                     "no_private_link_or_cmk_migration_risk", "external_dependencies_and_recovery_reviewed"):
            require(approvals.get(name) is True, f"Missing safety approval: {name}.")
        require(bool(str(approvals.get("change_reference", "")).strip()), "Provide the reviewed change/ticket reference.")
        if cross_region:
            require(approvals.get("cross_region_residency_and_gateways_approved") is True,
                    "Cross-region residency/gateway review is required.")
            require(approvals.get("small_storage_format_stable_confirmed") is True,
                    "Confirm every model is in stable small format, with no storage conversion in progress.")

    def preflight(self, spec):
        ws, target_id = guid(spec["workspace_id"]), guid(spec["target_capacity_id"])
        require(ws != self.control_workspace_id, "Do not move the notebook's control workspace.")
        workspace = self.api.get(FABRIC + f"workspaces/{ws}")
        source_id = guid(workspace.get("capacityId", ""))
        source = self.api.get(FABRIC + f"capacities/{source_id}")
        target = self.api.get(FABRIC + f"capacities/{target_id}")
        cross_region = capacity_rules(workspace, source, target, self.control_workspace_id)
        self.approvals(spec, cross_region)
        admin_workspaces = self.api.listing(FABRIC + "workspaces?roles=Admin")
        require(any(w["id"] == ws for w in admin_workspaces), "Caller must be a workspace Admin.")
        inventory = self.inventory(ws)
        snapshots = self.snapshot(ws, inventory, cross_region)
        backups = self.verify_backups(spec, snapshots)
        return {"workspace_id": ws, "workspace_name": workspace["displayName"],
                "source_capacity": source, "target_capacity": target, "cross_region": cross_region,
                "inventory": inventory, "models": snapshots, "backup": backups,
                "approvals": spec["approvals"], "status": "ready"}

    @staticmethod
    def inventory_signature(inventory):
        return {
            "items": sorted((i["id"], i["type"], i["displayName"], i.get("folderId")) for i in inventory["items"]),
            "reports": sorted((r["id"], r["name"], r.get("datasetId"), r.get("datasetWorkspaceId"),
                               r.get("reportType")) for r in inventory["reports"]),
            "dashboards": sorted((d["id"], d["displayName"]) for d in inventory["dashboards"]),
            "datasets": sorted((m["id"], m["name"]) for m in inventory["datasets"]),
            "roles": sorted(digest(r) for r in inventory["roles"]),
        }

    def wait_assignment(self, ws, target_id, response=None):
        deadline = self.clock() + self.config["assignment_timeout_seconds"]
        operation_url = None
        if response is not None:
            operation = response.headers.get("x-ms-operation-id")
            location = response.headers.get("Location")
            if not operation and location:
                operation = urlparse(location).path.rstrip("/").split("/")[-1]
            if operation:
                operation_url = FABRIC + "operations/" + guid(operation)
        operation_done = operation_url is None
        while self.clock() < deadline:
            if not operation_done:
                operation = self.api.get(operation_url)
                require(operation["status"] not in {"Failed", "Cancelled"}, "Capacity assignment operation failed.")
                operation_done = operation["status"] == "Succeeded"
            workspace = self.api.get(FABRIC + f"workspaces/{ws}")
            state = workspace.get("capacityAssignmentProgress")
            require(state != "Failed", "Capacity assignment failed (possibly partially). Keep source capacity active.")
            if operation_done and workspace.get("capacityId") == target_id and state == "Completed":
                target = self.api.get(FABRIC + f"capacities/{target_id}")
                require(region(workspace.get("capacityRegion")) == region(target["region"]),
                        "Workspace has not reached the destination region.")
                return
            self.sleep(self.config["poll_seconds"])
        raise TimeoutError("Assignment outcome uncertain. Keep both capacities active; use validate mode, not resubmit.")

    def compare(self, before, after, compare_rows=True):
        require(set(before) == set(after), "Semantic-model inventory changed.")
        for model_id, old in before.items():
            new = after[model_id]
            fields = ("schema_hash", "name", "storage", "datasources", "refresh_schedule")
            for field in fields:
                require(digest(old[field]) == digest(new[field]), f"Model {model_id}: {field} changed.")
            if compare_rows:
                require(old["rows"] == new["rows"], f"Model {model_id}: table row counts changed.")

    def validate(self, record):
        ws, target = record["workspace_id"], record["target_capacity"]["id"]
        self.wait_assignment(ws, target)
        current = self.inventory(ws)
        require(self.inventory_signature(record["inventory"]) == self.inventory_signature(current),
                "Post-move items, report bindings, folders or workspace roles changed.")
        after = self.snapshot(ws, current, record["cross_region"])
        self.compare(record["models"], after)
        record["validation"] = {"completed_at": utc_now(), "models_checked": len(after),
                                "row_counts_match": True, "inventory_and_bindings_match": True}
        # Public item GET responses do not expose actual per-item capacity placement.
        record["status"] = "validated_pending_manual_placement_check"
        return current

    def refresh(self, record, spec):
        selected = spec.get("refresh_model_ids")
        if selected is None:
            selected = list(record["models"])
        require(isinstance(selected, list) and len(selected) == len(set(selected))
                and set(selected) <= set(record["models"]), "Invalid/duplicate refresh model IDs.")
        record["refreshes"] = []
        for model_id in selected:
            ws = record["workspace_id"]
            root = POWERBI + f"groups/{ws}/datasets/{model_id}/refreshes"
            self.refresh_idle(ws, model_id)
            refresh = {"model_id": model_id, "status": "submission_pending"}
            record["refreshes"].append(refresh)
            self.save()
            response = self.api.request("POST", root, {
                "type": "full", "commitMode": "transactional", "applyRefreshPolicy": False,
                "retryCount": 0, "maxParallelism": 1,
            })
            location = response.headers.get("Location", "")
            refresh_id = response.headers.get("x-ms-request-id") or location.rstrip("/").split("/")[-1]
            refresh_id = guid(refresh_id)
            refresh.update(status="submitted", refresh_id=refresh_id)
            self.save()
            deadline = self.clock() + self.config["refresh_timeout_seconds"]
            while self.clock() < deadline:
                result = self.api.get(root + "/" + refresh_id)
                if result.get("status") == "Completed":
                    refresh.update(status="completed", completed_at=utc_now())
                    break
                require(result.get("status") in {"Unknown", "NotStarted", "InProgress"},
                        f"Refresh {refresh_id} failed/cancelled; source capacity must remain active.")
                self.sleep(self.config["poll_seconds"])
            else:
                raise TimeoutError(f"Refresh {refresh_id} outcome uncertain; monitor this ID, do not submit again.")
            self.save()
        inventory = self.inventory(record["workspace_id"])
        require(self.inventory_signature(record["inventory"]) == self.inventory_signature(inventory),
                "Items or report bindings changed during refresh.")
        after = self.snapshot(record["workspace_id"], inventory, record["cross_region"])
        self.compare(record["models"], after, compare_rows=False)
        record["post_refresh_rows"] = {key: value["rows"] for key, value in after.items()}
        record["refresh_validation"] = "completed; schema/connections retained; data may legitimately change"

    def run(self):
        try:
            return self._run()
        except Exception as exc:
            if self.report["status"] not in {"failed_or_uncertain", "blocked_no_moves"}:
                self.report.update(status="failed", error_type=type(exc).__name__)
                LOG.error("Run %s failed (%s). No further actions will be submitted.", self.run_id, type(exc).__name__)
                if self.archive:
                    self.save()
            raise

    def _run(self):
        specs = self.config["workspaces"]
        require(type(self.config["dry_run"]) is bool and type(self.config["refresh_semantic_models"]) is bool,
                "dry_run and refresh_semantic_models must be booleans.")
        require(self.config["mode"] in {"migrate", "validate"}, "mode must be migrate or validate.")
        for spec in specs:
            if self.config["refresh_semantic_models"]:
                selected = spec.get("refresh_model_ids")
                require(selected is None or (isinstance(selected, list)
                        and all(isinstance(m, str) and guid(m) == m for m in selected)
                        and len(selected) == len(set(selected))), "Invalid refresh model selection.")
        require(10 <= self.config["poll_seconds"] <= 300, "poll_seconds must be between 10 and 300.")
        for key in ("assignment_timeout_seconds", "refresh_timeout_seconds", "backup_max_age_hours"):
            require(isinstance(self.config[key], (int, float)) and self.config[key] > 0,
                    f"{key} must be positive.")
        ids = [guid(s["workspace_id"]) for s in specs]
        require(len(ids) == len(set(ids)), "Duplicate workspace IDs in the migration batch.")
        if self.config["mode"] == "validate":
            require(self.archive is not None, "Validation recovery requires an external journal.")
            prior = self.archive.read_manifest(self.config["validation_journal_blob"])
            self.report["prior_run_id"] = prior["run_id"]
            self.report["workspaces"] = prior["workspaces"]
            require(bool(prior["workspaces"]), "The journal contains no migration baseline.")
            for record in self.report["workspaces"]:
                require("models" in record and "inventory" in record, "Journal has no complete pre-move baseline.")
                self.validate(record)
            self.report["status"] = "validated_pending_manual_placement_check"
            self.save()
            return self.report
        if not specs:
            require(self.config["dry_run"], "No workspaces configured for a live migration.")
            self.report.update(status="discovery_only",
                               capacities=self.api.listing(FABRIC + "capacities"),
                               accessible_workspaces=self.api.listing(FABRIC + "workspaces"))
            return self.report
        require(bool(self.control_workspace_id), "Notebook control workspace identity must be known.")
        if not self.config["dry_run"]:
            require(self.archive is not None, "Live migration requires an external durable journal.")
            self.save()
        blocked = False
        for spec in specs:
            try:
                record = self.preflight(spec)
                if self.config["refresh_semantic_models"] and spec.get("refresh_model_ids") is not None:
                    require(set(spec["refresh_model_ids"]) <= set(record["models"]),
                            "Refresh selection contains a model outside this workspace.")
            except Exception as exc:
                # Per-workspace isolation is for planning only; the entire batch is blocked.
                LOG.error("Preflight blocked workspace %s (%s)", spec["workspace_id"], type(exc).__name__)
                record = {"workspace_id": spec["workspace_id"], "status": "blocked", "reason": str(exc)}
                blocked = True
            self.report["workspaces"].append(record)
        if blocked:
            self.report["status"] = "blocked_no_moves"
            self.save()
            raise SafetyError("Batch preflight failed; no workspaces moved. See MIGRATOR.report for blockers.")
        self.report["status"] = "planned"
        self.save()
        if self.config["dry_run"]:
            return self.report
        try:
            for spec, record in zip(specs, self.report["workspaces"]):
                # Re-read all safety evidence under the operator-maintained exclusive write freeze.
                fresh = self.preflight(spec)
                require(self.inventory_signature(fresh["inventory"]) == self.inventory_signature(record["inventory"]),
                        "Inventory changed after batch preflight.")
                self.compare(record["models"], fresh["models"])
                require(fresh["source_capacity"] == record["source_capacity"]
                        and fresh["target_capacity"] == record["target_capacity"], "Capacity metadata changed.")
                record["status"] = "assignment_submission_pending"
                self.save()
                response = self.api.request("POST", FABRIC + f"workspaces/{record['workspace_id']}/assignToCapacity",
                                            {"capacityId": record["target_capacity"]["id"]})
                record.update(status="assignment_submitted", assignment_headers={
                    key: response.headers[key] for key in ("Location", "x-ms-operation-id", "request-id")
                    if key in response.headers
                })
                self.save()
                self.wait_assignment(record["workspace_id"], record["target_capacity"]["id"], response)
                self.validate(record)
                self.save()
                if self.config["refresh_semantic_models"]:
                    self.refresh(record, spec)
                self.save()
        except Exception as exc:
            record["status"] = "failed_or_uncertain_keep_source_capacity"
            record["error_type"] = type(exc).__name__
            self.report["status"] = "failed_or_uncertain"
            self.save()
            LOG.error("Migration stopped. Keep both capacities active. Run %s; %s", self.run_id, type(exc).__name__)
            raise
        self.report.update(status="validated_pending_manual_placement_check", completed_at=utc_now())
        self.save()
        return self.report


def create_migrator(config):
    import notebookutils

    if config["workspaces"] or config["mode"] == "validate":
        try:
            from azure.storage.blob import BlobServiceClient
            from sempy.fabric import connect_semantic_model, evaluate_dax
        except ImportError as exc:
            raise SafetyError(
                "Migration prerequisites are missing. Provision a compatible Fabric Environment with "
                "azure-storage-blob and semantic-link-sempy (including connect_semantic_model/evaluate_dax), "
                "then restart the session. No reassignment was submitted."
            ) from exc
    credential = NotebookCredential()
    expected_tenant = guid(config["expected_tenant_id"])

    def token_provider(url):
        token = credential.get_token("https://analysis.windows.net/powerbi/api/.default").token
        require(claims(token).get("tid") == expected_tenant, "Notebook is running in the wrong tenant.")
        return token

    control_id = guid(notebookutils.runtime.context["currentWorkspaceId"])
    archive = BlobArchive(config["archive"], credential) if config.get("archive") else None
    return Migrator(config, Api(token_provider), ModelProbe(credential), archive, control_id)
