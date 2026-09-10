"""Inventory Fabric workspace users and enrich them with Entra location data.

Authentication uses the interactive Azure CLI user session. This script does
not require a custom app registration, client ID, client secret, or service
principal.

Examples:
    az login --tenant <tenant-id>
    python fabric_workspace_users_locations.py
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


FABRIC_API_ROOT = "https://api.fabric.microsoft.com/v1"
GRAPH_API_ROOT = "https://graph.microsoft.com/v1.0"

USER_FIELDS = [
    "id",
    "displayName",
    "userPrincipalName",
    "mail",
    "userType",
    "accountEnabled",
    "city",
    "state",
    "country",
    "officeLocation",
    "usageLocation",
    "postalCode",
    "streetAddress",
    "companyName",
    "department",
    "jobTitle",
]
ACCESS_FIELDS = [
    "workspaceId",
    "workspaceName",
    "workspaceType",
    "capacityId",
    "capacityRegion",
    "role",
    "userId",
    "displayName",
    "userPrincipalName",
    "mail",
    "userType",
    "accessPath",
    "assignedPrincipalId",
    "assignedPrincipalName",
    "assignedPrincipalType",
]
ERROR_FIELDS = [
    "workspaceId",
    "workspaceName",
    "workspaceType",
    "statusCode",
    "error",
]


def get_azure_cli_executable() -> str:
    executable = shutil.which("az")
    if not executable:
        raise RuntimeError(
            "Azure CLI was not found. Install it and ensure 'az' is on PATH."
        )
    return executable


class ApiError(RuntimeError):
    def __init__(self, status_code: int, url: str, response_text: str):
        message = f"HTTP {status_code} calling {url}: {response_text[:1000]}"
        super().__init__(message)
        self.status_code = status_code
        self.url = url
        self.response_text = response_text


class AzureCliTokenProvider:
    def __init__(self, resource: str | None = None, resource_type: str | None = None):
        if bool(resource) == bool(resource_type):
            raise ValueError("Specify exactly one of resource or resource_type.")
        self.resource = resource
        self.resource_type = resource_type
        self.access_token: str | None = None
        self.expires_on = 0

    def get_token(self, force_refresh: bool = False) -> str:
        if (
            not force_refresh
            and self.access_token
            and time.time() < self.expires_on - 300
        ):
            return self.access_token

        command = ["az", "account", "get-access-token"]
        if self.resource:
            command.extend(["--resource", self.resource])
        else:
            command.extend(["--resource-type", self.resource_type or ""])
        command.extend(["--output", "json"])

        result = run_az(command)
        token_result = json.loads(result)
        self.access_token = token_result["accessToken"]
        self.expires_on = int(token_result.get("expires_on") or time.time() + 3600)
        return self.access_token


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "List effective users of accessible Fabric workspaces and enrich "
            "them with Microsoft Graph location attributes."
        )
    )
    parser.add_argument(
        "--tenant-id",
        help="Tenant GUID or domain. Used only if an interactive az login is required.",
    )
    parser.add_argument(
        "--exclude-entire-tenant",
        action="store_true",
        help="Do not expand EntireTenant role assignments into all tenant users.",
    )
    parser.add_argument(
        "--request-timeout",
        type=int,
        default=60,
        help="HTTP timeout in seconds. Default: 60",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=8,
        help="Maximum HTTP retries for throttling and server errors. Default: 8",
    )
    return parser.parse_args()


def run_az(command: list[str], allow_failure: bool = False) -> str:
    resolved_command = [get_azure_cli_executable(), *command[1:]]
    try:
        result = subprocess.run(
            resolved_command,
            check=not allow_failure,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError as error:
        raise RuntimeError(
            "Azure CLI was not found. Install it and ensure 'az' is on PATH."
        ) from error
    except subprocess.CalledProcessError as error:
        details = (error.stderr or error.stdout or str(error)).strip()
        raise RuntimeError(f"Azure CLI command failed: {details}") from error

    if result.returncode != 0 and not allow_failure:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Azure CLI command failed: {details}")
    return result.stdout.strip()


def ensure_azure_cli_login(tenant_id: str | None) -> dict[str, Any]:
    az_executable = get_azure_cli_executable()

    account_result = subprocess.run(
        [az_executable, "account", "show", "--output", "json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if account_result.returncode != 0:
        print("No active Azure CLI user session. Starting interactive sign-in...")
        login_command = ["az", "login"]
        if tenant_id:
            login_command.extend(["--tenant", tenant_id])
        login_command[0] = az_executable
        subprocess.run(login_command, check=True)

    account = json.loads(run_az(["az", "account", "show", "--output", "json"]))
    if account.get("user", {}).get("type") != "user":
        raise RuntimeError(
            "Azure CLI is authenticated with a workload identity. Run 'az logout' "
            "and then 'az login' with the user who should execute this inventory."
        )

    signed_in_tenant = account.get("tenantId")
    if tenant_id and signed_in_tenant and tenant_id.lower() != signed_in_tenant.lower():
        print(f"Switching Azure CLI authentication to tenant {tenant_id}...")
        subprocess.run(
            [az_executable, "login", "--tenant", tenant_id],
            check=True,
        )
        account = json.loads(run_az(["az", "account", "show", "--output", "json"]))

    return account


def decode_jwt_claims(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))


def describe_token(name: str, token: str) -> None:
    claims = decode_jwt_claims(token)
    scopes = claims.get("scp", "")
    print(
        f"{name} token: audience={claims.get('aud')}, "
        f"tenant={claims.get('tid')}, delegatedScopes={scopes or '(none listed)'}"
    )


def request_json(
    method: str,
    url: str,
    token_provider: AzureCliTokenProvider,
    *,
    request_timeout: int,
    max_retries: int,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)

    for attempt in range(max_retries):
        request_headers["Authorization"] = f"Bearer {token_provider.get_token()}"
        response = requests.request(
            method,
            url,
            headers=request_headers,
            json=body,
            timeout=request_timeout,
        )

        if response.status_code in (200, 201, 202, 204):
            return response.json() if response.content else {}

        if response.status_code == 401 and attempt == 0:
            token_provider.get_token(force_refresh=True)
            continue

        if response.status_code == 429 or 500 <= response.status_code < 600:
            retry_after = response.headers.get("Retry-After")
            delay = (
                int(retry_after)
                if retry_after and retry_after.isdigit()
                else min(2**attempt, 60)
            )
            time.sleep(delay)
            continue

        raise ApiError(response.status_code, url, response.text)

    raise RuntimeError(f"Request failed after {max_retries} attempts: {url}")


def get_all_pages(
    first_url: str,
    token_provider: AzureCliTokenProvider,
    *,
    request_timeout: int,
    max_retries: int,
    headers: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    url: str | None = first_url
    while url:
        page = request_json(
            "GET",
            url,
            token_provider,
            request_timeout=request_timeout,
            max_retries=max_retries,
            headers=headers,
        )
        rows.extend(page.get("value", []))
        url = page.get("continuationUri") or page.get("@odata.nextLink")
    return rows


def normalize_user(user: dict[str, Any]) -> dict[str, Any]:
    normalized = {field: user.get(field) for field in USER_FIELDS}
    normalized["graphLookupError"] = user.get("graphLookupError")
    return normalized


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=fields,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    account = ensure_azure_cli_login(args.tenant_id)
    print(
        f"Azure CLI user: {account.get('user', {}).get('name')} "
        f"(tenant {account.get('tenantId')})"
    )

    fabric_tokens = AzureCliTokenProvider(
        resource="https://api.fabric.microsoft.com"
    )
    graph_tokens = AzureCliTokenProvider(resource_type="ms-graph")

    fabric_token = fabric_tokens.get_token()
    graph_token = graph_tokens.get_token()
    describe_token("Fabric", fabric_token)
    describe_token("Microsoft Graph", graph_token)

    signed_in_graph_user = request_json(
        "GET",
        f"{GRAPH_API_ROOT}/me?$select=id,displayName,userPrincipalName",
        graph_tokens,
        request_timeout=args.request_timeout,
        max_retries=args.max_retries,
    )
    print(
        "Graph user: "
        f"{signed_in_graph_user.get('displayName')} "
        f"({signed_in_graph_user.get('userPrincipalName')})"
    )

    workspaces = get_all_pages(
        f"{FABRIC_API_ROOT}/workspaces",
        fabric_tokens,
        request_timeout=args.request_timeout,
        max_retries=args.max_retries,
    )
    print(f"Accessible Fabric workspaces: {len(workspaces)}")

    role_assignments: list[dict[str, Any]] = []
    inspection_errors: list[dict[str, Any]] = []
    for index, workspace in enumerate(workspaces, start=1):
        workspace_id = workspace["id"]
        workspace_name = workspace.get("displayName")
        print(f"[{index}/{len(workspaces)}] Reading {workspace_name}")
        try:
            assignments = get_all_pages(
                f"{FABRIC_API_ROOT}/workspaces/{workspace_id}/roleAssignments",
                fabric_tokens,
                request_timeout=args.request_timeout,
                max_retries=args.max_retries,
            )
            for assignment in assignments:
                role_assignments.append(
                    {
                        "workspaceId": workspace_id,
                        "workspaceName": workspace_name,
                        "workspaceType": workspace.get("type"),
                        "capacityId": workspace.get("capacityId"),
                        "capacityRegion": workspace.get("capacityRegion"),
                        "role": assignment.get("role"),
                        "principal": assignment.get("principal", {}),
                    }
                )
        except ApiError as error:
            inspection_errors.append(
                {
                    "workspaceId": workspace_id,
                    "workspaceName": workspace_name,
                    "workspaceType": workspace.get("type"),
                    "statusCode": error.status_code,
                    "error": error.response_text[:4000],
                }
            )

    user_select = ",".join(USER_FIELDS)
    user_cache: dict[str, dict[str, Any]] = {}
    group_user_cache: dict[str, list[str]] = {}
    all_tenant_user_ids: list[str] | None = None

    def get_group_users(group_id: str) -> list[str]:
        if group_id not in group_user_cache:
            url = (
                f"{GRAPH_API_ROOT}/groups/{quote(group_id)}/transitiveMembers/"
                f"microsoft.graph.user?$select=id&$top=999&$count=true"
            )
            members = get_all_pages(
                url,
                graph_tokens,
                request_timeout=args.request_timeout,
                max_retries=args.max_retries,
                headers={"ConsistencyLevel": "eventual"},
            )
            group_user_cache[group_id] = [member["id"] for member in members]
        return group_user_cache[group_id]

    def get_all_tenant_users() -> list[str]:
        nonlocal all_tenant_user_ids
        if all_tenant_user_ids is None:
            users = get_all_pages(
                f"{GRAPH_API_ROOT}/users?$select={user_select}&$top=999",
                graph_tokens,
                request_timeout=args.request_timeout,
                max_retries=args.max_retries,
            )
            all_tenant_user_ids = []
            for user in users:
                normalized = normalize_user(user)
                user_cache[normalized["id"]] = normalized
                all_tenant_user_ids.append(normalized["id"])
        return all_tenant_user_ids

    effective_access: list[dict[str, Any]] = []
    seen_access: set[tuple[Any, ...]] = set()
    for assignment in role_assignments:
        principal = assignment["principal"]
        principal_type = principal.get("type")
        principal_id = principal.get("id")

        if principal_type == "User":
            user_ids = [principal_id]
            access_path = "Direct"
        elif principal_type == "Group":
            user_ids = get_group_users(principal_id)
            access_path = "Group"
        elif principal_type == "EntireTenant" and not args.exclude_entire_tenant:
            user_ids = get_all_tenant_users()
            access_path = "EntireTenant"
        else:
            continue

        for user_id in user_ids:
            access_key = (
                assignment["workspaceId"],
                assignment["role"],
                user_id,
                access_path,
                principal_id,
            )
            if access_key in seen_access:
                continue
            seen_access.add(access_key)
            effective_access.append(
                {
                    "workspaceId": assignment["workspaceId"],
                    "workspaceName": assignment["workspaceName"],
                    "workspaceType": assignment["workspaceType"],
                    "capacityId": assignment["capacityId"],
                    "capacityRegion": assignment["capacityRegion"],
                    "role": assignment["role"],
                    "userId": user_id,
                    "accessPath": access_path,
                    "assignedPrincipalId": principal_id,
                    "assignedPrincipalName": principal.get("displayName"),
                    "assignedPrincipalType": principal_type,
                }
            )

    distinct_user_ids = sorted(
        {row["userId"] for row in effective_access if row.get("userId")}
    )
    missing_user_ids = [
        user_id for user_id in distinct_user_ids if user_id not in user_cache
    ]

    for start in range(0, len(missing_user_ids), 20):
        batch_ids = missing_user_ids[start : start + 20]
        batch_requests = [
            {
                "id": str(index),
                "method": "GET",
                "url": f"/users/{quote(user_id)}?$select={user_select}",
            }
            for index, user_id in enumerate(batch_ids)
        ]
        batch_result = request_json(
            "POST",
            f"{GRAPH_API_ROOT}/$batch",
            graph_tokens,
            request_timeout=args.request_timeout,
            max_retries=args.max_retries,
            body={"requests": batch_requests},
        )
        for batch_response in batch_result.get("responses", []):
            user_id = batch_ids[int(batch_response["id"])]
            if batch_response.get("status") == 200:
                user_cache[user_id] = normalize_user(batch_response["body"])
            else:
                body = batch_response.get("body", {})
                message = body.get("error", {}).get("message", json.dumps(body))
                user_cache[user_id] = normalize_user(
                    {
                        "id": user_id,
                        "graphLookupError": (
                            f"HTTP {batch_response.get('status')}: {message}"
                        ),
                    }
                )

    user_locations = [
        user_cache.get(
            user_id,
            normalize_user(
                {
                    "id": user_id,
                    "graphLookupError": "No Microsoft Graph result returned",
                }
            ),
        )
        for user_id in distinct_user_ids
    ]

    for access in effective_access:
        user = user_cache.get(access["userId"], {})
        for field in ["displayName", "userPrincipalName", "mail", "userType"]:
            access[field] = user.get(field)

    output_dir = Path.cwd()
    output_paths = [
        output_dir / "workspace_user_access.csv",
        output_dir / "workspace_user_locations.csv",
        output_dir / "workspace_inspection_errors.csv",
    ]
    write_csv(output_paths[0], effective_access, ACCESS_FIELDS)
    write_csv(
        output_paths[1],
        user_locations,
        USER_FIELDS + ["graphLookupError"],
    )
    write_csv(output_paths[2], inspection_errors, ERROR_FIELDS)

    print(f"Workspace-user access rows: {len(effective_access)}")
    print(f"Distinct users: {len(user_locations)}")
    print(f"Uninspectable workspaces: {len(inspection_errors)}")
    for path in output_paths:
        print(f"Created {path}")

    print(f"Completed at {datetime.now(timezone.utc).isoformat()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ApiError as error:
        if error.url.startswith(GRAPH_API_ROOT) and error.status_code == 403:
            print(
                "\nMicrosoft Graph denied a directory operation. The Azure CLI "
                "token is valid, but the Azure CLI enterprise application and "
                "signed-in user do not have sufficient delegated directory "
                "permissions. Tenant administrator consent may be required.",
                file=sys.stderr,
            )
        elif error.url.startswith(FABRIC_API_ROOT) and error.status_code in (401, 403):
            print(
                "\nFabric denied the request. Confirm the signed-in user has a "
                "Fabric license and the required workspace access.",
                file=sys.stderr,
            )
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
