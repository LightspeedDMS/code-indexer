"""Pilot actions driven through the REAL server front doors (REST, MCP, Web).

Each driver performs one security action and returns the PilotEvent for the
audit row it produced: (action_type, event_uuid, door), read back through the
audit-log front door (GET /api/v1/audit-logs).  The event_uuid becomes
metadata.productLogId once SIEM delivery exists, which is how the scenarios
find the delivered event at the sidecar.  Sample data is neutral.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx
import pyotp

from tests.e2e.server.conftest import AdminTokenProvider

EXAMPLE_PASSWORD = "Example-Passw0rd-1!"
WRONG_PASSWORD = "not-the-password-1!"
AUDIT_POLL_SECONDS = 0.5
AUDIT_TIMEOUT_SECONDS = 30.0
_CSRF_DOUBLE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')
_CSRF_SINGLE = re.compile(r"name='csrf_token' value='([^']*)'")
_MANUAL_KEY = re.compile(r"class='mk'>([^<]+)<")


def unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class PilotEvent:
    action_type: str
    event_uuid: str
    door: str


class FrontDoor:
    """Drives security actions as an admin (and as ordinary users) over HTTP."""

    def __init__(self, http: httpx.Client, admin: AdminTokenProvider) -> None:
        self.http = http
        self.admin = admin

    def _admin_headers(self) -> Dict[str, str]:
        return {"Authorization": "Bearer " + self.admin.get_token()}

    # -- audit read-back ----------------------------------------------------
    def _audit_rows(self, action_type: str) -> List[Dict[str, Any]]:
        resp = self.http.get(
            "/api/v1/audit-logs",
            headers=self._admin_headers(),
            params={"tier": "all", "action_type": action_type, "limit": 100},
        )
        assert resp.status_code == 200, (
            f"audit query failed: {resp.status_code} {resp.text}"
        )
        rows: List[Dict[str, Any]] = resp.json()["logs"]
        return rows

    def max_audit_id(self) -> int:
        resp = self.http.get(
            "/api/v1/audit-logs",
            headers=self._admin_headers(),
            params={"tier": "all", "limit": 1},
        )
        assert resp.status_code == 200, resp.text
        logs = resp.json()["logs"]
        return int(logs[0]["id"]) if logs else 0

    def audit_event(
        self, action_type: str, target_id: str, after_id: int, door: str
    ) -> PilotEvent:
        """The newest matching audit row written after *after_id* (bounded poll)."""
        deadline = time.monotonic() + AUDIT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            rows = [
                r
                for r in self._audit_rows(action_type)
                if r["id"] > after_id
                and r["target_id"] == target_id
                and r.get("event_uuid")
            ]
            if rows:
                return PilotEvent(action_type, str(rows[0]["event_uuid"]), door)
            time.sleep(AUDIT_POLL_SECONDS)
        raise AssertionError(
            f"no {action_type} audit row for {target_id} after id {after_id}"
        )

    # -- REST ---------------------------------------------------------------
    def create_user(self, username: str, role: str = "normal_user") -> PilotEvent:
        before = self.max_audit_id()
        resp = self.http.post(
            "/api/admin/users",
            headers=self._admin_headers(),
            json={"username": username, "password": EXAMPLE_PASSWORD, "role": role},
        )
        assert resp.status_code == 201, (
            f"create user failed: {resp.status_code} {resp.text}"
        )
        return self.audit_event("user_created", username, before, "rest")

    def rest_login(self, username: str, password: str) -> httpx.Response:
        return self.http.post(
            "/auth/login", json={"username": username, "password": password}
        )

    def rest_login_success(self, username: str) -> PilotEvent:
        before = self.max_audit_id()
        assert self.rest_login(username, EXAMPLE_PASSWORD).status_code == 200
        return self.audit_event("authentication_success", username, before, "rest")

    def rest_login_failure(self, username: str) -> PilotEvent:
        before = self.max_audit_id()
        assert self.rest_login(username, WRONG_PASSWORD).status_code == 401
        return self.audit_event("authentication_failure", username, before, "rest")

    def change_role(self, username: str, role: str) -> PilotEvent:
        before = self.max_audit_id()
        resp = self.http.put(
            f"/api/admin/users/{username}",
            headers=self._admin_headers(),
            json={"role": role},
        )
        assert resp.status_code == 200, (
            f"role change failed: {resp.status_code} {resp.text}"
        )
        return self.audit_event("user_role_changed", username, before, "rest")

    def group_id(self, name: str) -> str:
        resp = self.http.get("/api/v1/groups", headers=self._admin_headers())
        assert resp.status_code == 200, f"group listing failed: {resp.status_code}"
        ids = [str(g["id"]) for g in resp.json() if g.get("name") == name]
        assert ids, f"no group named {name!r}"
        return ids[0]

    def create_api_key(self, username: str) -> str:
        login = self.rest_login(username, EXAMPLE_PASSWORD)
        assert login.status_code == 200, login.text
        resp = self.http.post(
            "/api/keys",
            headers={"Authorization": "Bearer " + login.json()["access_token"]},
            json={"name": "example-key"},
        )
        assert resp.status_code == 201, f"api key creation failed: {resp.status_code}"
        return str(resp.json()["api_key"])

    # -- MCP ----------------------------------------------------------------
    def mcp_tool(
        self, name: str, arguments: Dict[str, Any], *, public: bool = False
    ) -> Dict[str, Any]:
        resp = self.http.post(
            "/mcp-public" if public else "/mcp",
            headers={} if public else self._admin_headers(),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        assert resp.status_code == 200, (
            f"MCP {name} failed: {resp.status_code} {resp.text}"
        )
        payload: Dict[str, Any] = json.loads(
            resp.json()["result"]["content"][0]["text"]
        )
        return payload

    def mcp_login_success(self, username: str, api_key: str) -> PilotEvent:
        before = self.max_audit_id()
        result = self.mcp_tool(
            "authenticate", {"username": username, "api_key": api_key}, public=True
        )
        assert result["success"] is True, result
        return self.audit_event("authentication_success", username, before, "mcp")

    def mcp_login_failure(self, username: str) -> PilotEvent:
        before = self.max_audit_id()
        result = self.mcp_tool(
            "authenticate",
            {"username": username, "api_key": "cidx_sk_" + "0" * 32},
            public=True,
        )
        assert result["success"] is False, result
        return self.audit_event("authentication_failure", username, before, "mcp")

    def mcp_move_group(self, username: str, group_id: str) -> PilotEvent:
        before = self.max_audit_id()
        result = self.mcp_tool(
            "manage_group_members",
            {"action": "add", "group_id": group_id, "user_id": username},
        )
        assert result["success"] is True, result
        return self.audit_event("user_group_change", username, before, "mcp")

    # -- Web ----------------------------------------------------------------
    def web_activate_mfa(self, username: str) -> PilotEvent:
        """Self-service TOTP enrollment through the Web UI (mfa_activated)."""
        before = self.max_audit_id()
        with httpx.Client(base_url=str(self.http.base_url), timeout=30) as web:
            csrf = _first(_CSRF_DOUBLE, web.get("/login").text, "login csrf")
            web.post(
                "/login",
                data={
                    "username": username,
                    "password": EXAMPLE_PASSWORD,
                    "csrf_token": csrf,
                },
            )
            setup = web.get("/user/mfa/setup").text
            secret = re.sub(r"\s", "", _first(_MANUAL_KEY, setup, "manual key"))
            verify = web.post(
                "/user/mfa/verify",
                data={
                    "totp_code": pyotp.TOTP(secret).now(),
                    "csrf_token": _first(_CSRF_SINGLE, setup, "setup csrf"),
                },
            )
            assert verify.status_code == 200, f"MFA verify failed: {verify.status_code}"
        return self.audit_event("mfa_activated", username, before, "web")


def _first(pattern: "re.Pattern[str]", text: str, what: str) -> str:
    match: Optional["re.Match[str]"] = pattern.search(text)
    assert match is not None, f"could not find the {what} in the page"
    return match.group(1)
