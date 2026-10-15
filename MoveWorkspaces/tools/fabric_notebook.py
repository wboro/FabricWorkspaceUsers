"""Read or deploy a Fabric notebook using the current, explicitly checked Azure CLI account."""

import argparse
import base64
import json
from pathlib import Path
import subprocess
import time
from uuid import UUID
from urllib.parse import urljoin, urlparse

import requests


ACCOUNT = "wborowiecki@MngEnvMCAP837260.onmicrosoft.com"
TENANT = "2ee4898e-0173-44a4-92bf-fca5f81a39ee"
BASE = "https://api.fabric.microsoft.com/v1"


def az_json(*args):
    result = subprocess.run(["az.cmd", *args, "--output", "json"], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["download", "deploy"])
    parser.add_argument("--workspace", default="Dev")
    parser.add_argument("--name", required=True)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--sibling", default="SemanticModelBackup")
    args = parser.parse_args()
    account = az_json("account", "show")
    if account["user"]["name"].casefold() != ACCOUNT.casefold():
        raise RuntimeError(f"Azure CLI must be signed in as {ACCOUNT}")
    if account["tenantId"] != TENANT:
        raise RuntimeError("Azure CLI is signed into a different tenant")
    token = az_json("account", "get-access-token", "--resource", "https://api.fabric.microsoft.com")["accessToken"]
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})

    def call(method, url, **kwargs):
        url = urljoin(BASE + "/", url)
        if urlparse(url).scheme != "https" or urlparse(url).netloc != "api.fabric.microsoft.com":
            raise RuntimeError(f"Refusing an unexpected API URL: {url}")
        response = session.request(method, url, timeout=120, **kwargs)
        response.raise_for_status()
        return response

    def listing(url):
        values, seen = [], set()
        while url:
            if url in seen:
                raise RuntimeError("Repeated inventory continuation URL")
            seen.add(url)
            data = call("GET", url).json()
            values.extend(data["value"])
            url = data.get("continuationUri")
            if not url and data.get("continuationToken"):
                raise RuntimeError("Continuation token without continuation URI")
        return values

    def finish(response, result=False):
        if response.status_code != 202:
            return response.json() if response.content else {}
        operation_id = response.headers.get("x-ms-operation-id") or urlparse(response.headers["Location"]).path.rstrip("/").split("/")[-1]
        location = BASE + "/operations/" + str(UUID(operation_id))
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            time.sleep(max(1, int(response.headers.get("Retry-After", "2"))))
            response = call("GET", location)
            state = response.json()
            if state["status"] == "Succeeded":
                return call("GET", location.rstrip("/") + "/result").json() if result else state
            if state["status"] in {"Failed", "Cancelled"}:
                raise RuntimeError(json.dumps(state))
        raise TimeoutError(f"Operation still pending: {location}; do not blindly resubmit")

    workspaces = listing(BASE + "/workspaces")
    matches = [w for w in workspaces if w["displayName"] == args.workspace]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one workspace named {args.workspace}, found {len(matches)}")
    workspace = matches[0]
    capacity = call("GET", BASE + "/capacities/" + workspace["capacityId"]).json()
    if capacity["state"] != "Active":
        raise RuntimeError("Deployment capacity is not active")
    root = f"{BASE}/workspaces/{workspace['id']}"
    items = listing(root + "/items?type=Notebook")
    existing = [item for item in items if item["displayName"] == args.name]
    if len(existing) > 1:
        raise RuntimeError("Ambiguous notebook name")
    if args.action == "download":
        if not existing:
            raise RuntimeError("Reference notebook not found")
        definition = finish(call("POST", root + f"/notebooks/{existing[0]['id']}/getDefinition?format=ipynb", json={}), True)
        part = next(p for p in definition["definition"]["parts"] if p["path"].endswith(".ipynb"))
        args.file.parent.mkdir(parents=True, exist_ok=True)
        args.file.write_bytes(base64.b64decode(part["payload"]))
        print(json.dumps({"file": str(args.file), "item": existing[0]}, indent=2))
        return
    import nbformat

    notebook = nbformat.read(args.file, as_version=4)
    nbformat.validate(notebook)
    siblings = [item for item in items if item["displayName"] == args.sibling]
    if len(siblings) != 1:
        raise RuntimeError("Could not uniquely locate reference sibling")
    sibling = call("GET", root + "/items/" + siblings[0]["id"]).json()
    definition = {"format": "ipynb", "parts": [{
        "path": "notebook-content.ipynb", "payloadType": "InlineBase64",
        "payload": base64.b64encode(args.file.read_bytes()).decode("ascii"),
    }]}
    if existing:
        item_id = existing[0]["id"]
        finish(call("POST", root + f"/notebooks/{item_id}/updateDefinition", json={"definition": definition}))
    else:
        body = {
            "displayName": args.name, "type": "Notebook",
            "description": "Fail-closed P-to-F capacity migration with preflight, validation and optional transactional refresh.",
            "definition": definition,
        }
        if sibling.get("folderId"):
            body["folderId"] = sibling["folderId"]
        created = finish(call("POST", root + "/items", json=body), True)
        item_id = created["id"]
    item = call("GET", root + "/items/" + item_id).json()
    if item["displayName"] != args.name:
        raise RuntimeError("Persisted item metadata did not match")
    print(json.dumps({"workspace": workspace["displayName"], "item": item,
                      "url": f"https://app.fabric.microsoft.com/groups/{workspace['id']}/synapsenotebooks/{item_id}"}, indent=2))


if __name__ == "__main__":
    main()
