"""Phase 6 E2E: security audit catalog subset on the PostgreSQL-backed server.

Same attribution rules as Phase 3's test_27_security_audit_catalog.py,
driven through the REST/MCP front door of the PG-backed uvicorn by a second
admin whose name is not the seeded admin's:

1. creating a user with the admin role (a formerly fail-closed class; every
   class now proceeds on an audit write failure) records ``user_created``;
2. adding and removing a golden repository records ``golden_repo_added`` and
   ``golden_repo_removed`` naming that admin -- never ``admin``.

Rows are read back through MCP ``query_audit_logs``.
"""

from __future__ import annotations

import json
import os
import pathlib
import uuid
from typing import Any, Dict, List, Optional

import httpx
import pytest

from tests.e2e.helpers import (
    require_postgres,
    require_voyage_key,
    rest_call,
    wait_for_job,
)

_PASSWORD = "E2e-Pg-Audit-Pass-9431!"


def setup_module(_: Any) -> None:
    require_postgres()


def _entries(
    client: httpx.Client, token: str, action_type: str, actor: str
) -> List[Dict[str, Any]]:
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "query_audit_logs",
                "arguments": {"limit": 100, "action_type": action_type, "user": actor},
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text[:300]
    result = json.loads(resp.json()["result"]["content"][0]["text"])
    assert result.get("success") is True, result
    entries: List[Dict[str, Any]] = result["entries"]
    return entries


def _one_row(
    client: httpx.Client, token: str, action_type: str, actor: str, target_id: str
) -> Dict[str, Any]:
    rows = [
        e
        for e in _entries(client, token, action_type, actor)
        if e["target_id"] == target_id
    ]
    assert len(rows) == 1, (action_type, rows)
    return rows[0]


def _login(client: httpx.Client, username: str) -> str:
    resp = client.post(
        "/auth/login", json={"username": username, "password": _PASSWORD}
    )
    assert resp.status_code == 200, resp.text[:300]
    token: str = resp.json()["access_token"]
    return token


def _clean_up_golden_repo(
    client: httpx.Client,
    token: str,
    alias: str,
    *,
    add_accepted: bool,
    created: bool,
    removal_accepted: bool,
    removal_job_id: Optional[str],
    timeout: float,
) -> None:
    """Leave no golden repository behind without repeating an accepted DELETE.

    An accepted DELETE runs as a background removal job, and a second DELETE
    while that job runs is refused as a duplicate job.
    """
    if removal_job_id is not None:
        final = wait_for_job(
            client, removal_job_id, token=token, timeout=timeout, poll_interval=2.0
        )
        assert final["status"] == "completed", final
        return
    if removal_accepted:
        # The removal was accepted but its job id was never read. No helper
        # waits for an alias to disappear, and the registry row goes before
        # the files do, so its absence would not prove the job finished;
        # another DELETE would collide with the running job. Fail loudly.
        raise AssertionError(
            f"removal of {alias!r} was accepted but its job id was not read; "
            "cannot wait for the removal job"
        )
    if not add_accepted:
        return
    cleanup = rest_call(client, "DELETE", f"/api/admin/golden-repos/{alias}", token)
    allowed = {204} if created else {204, 404}
    assert cleanup.status_code in allowed, cleanup.text[:300]


def test_pg_admin_creation_and_golden_removal_name_the_caller(
    pg_http_client: httpx.Client, pg_admin_token: str
) -> None:
    require_voyage_key()
    seed_cache_dir = os.environ.get("E2E_SEED_CACHE_DIR", "")
    if not seed_cache_dir:
        pytest.skip("E2E_SEED_CACHE_DIR not set")
    seed_repo = str(pathlib.Path(seed_cache_dir) / "markupsafe")

    suffix = uuid.uuid4().hex[:8]
    second_admin = f"e2e-pg-audit-admin-{suffix}"
    promoted = f"e2e-pg-audit-promoted-{suffix}"
    alias = f"e2e-pg-audit-golden-{suffix}"
    created = rest_call(
        pg_http_client,
        "POST",
        "/api/admin/users",
        pg_admin_token,
        json={"username": second_admin, "password": _PASSWORD, "role": "admin"},
    )
    assert created.status_code == 201, created.text[:300]
    admin2 = _login(pg_http_client, second_admin)
    job_timeout = float(os.environ.get("E2E_GOLDEN_REPO_JOB_TIMEOUT", "300.0"))
    add_accepted = repo_created = removal_accepted = False
    removal_job_id: Optional[str] = None

    try:
        # --- Admin-role user creation (formerly fail-closed) ---
        made = rest_call(
            pg_http_client,
            "POST",
            "/api/admin/users",
            admin2,
            json={"username": promoted, "password": _PASSWORD, "role": "admin"},
        )
        assert made.status_code == 201, made.text[:300]
        row = _one_row(
            pg_http_client, pg_admin_token, "user_created", second_admin, promoted
        )
        assert (row["outcome"], row["source"]) == ("success", "rest")

        # --- Golden repo add and removal ---
        added = rest_call(
            pg_http_client,
            "POST",
            "/api/admin/golden-repos",
            admin2,
            json={"repo_url": seed_repo, "alias": alias},
        )
        assert added.status_code == 202, added.text[:300]
        add_accepted = True
        status = wait_for_job(
            pg_http_client,
            added.json()["job_id"],
            token=pg_admin_token,
            timeout=job_timeout,
            poll_interval=2.0,
        )
        assert status["status"] == "completed", status
        repo_created = True
        row = _one_row(
            pg_http_client, pg_admin_token, "golden_repo_added", second_admin, alias
        )
        assert (row["outcome"], row["source"]) == ("success", "rest")

        removed = rest_call(
            pg_http_client, "DELETE", f"/api/admin/golden-repos/{alias}", admin2
        )
        assert removed.status_code == 204, removed.text[:300]
        removal_accepted = True
        row = _one_row(
            pg_http_client, pg_admin_token, "golden_repo_removed", second_admin, alias
        )
        assert (row["outcome"], row["source"]) == ("success", "rest")
        removal_job_id = str(row["details"]["job_id"])
        seeded_admin_rows = _entries(
            pg_http_client, pg_admin_token, "golden_repo_removed", "admin"
        )
        assert all(e["target_id"] != alias for e in seeded_admin_rows)
        every_entry = json.dumps(
            _entries(pg_http_client, pg_admin_token, "golden_repo_added", second_admin)
        )
        assert seed_repo not in every_entry and _PASSWORD not in every_entry
    finally:
        try:
            _clean_up_golden_repo(
                pg_http_client,
                pg_admin_token,
                alias,
                add_accepted=add_accepted,
                created=repo_created,
                removal_accepted=removal_accepted,
                removal_job_id=removal_job_id,
                timeout=job_timeout,
            )
        finally:
            for user in (promoted, second_admin):
                rest_call(
                    pg_http_client,
                    "DELETE",
                    f"/api/admin/users/{user}",
                    pg_admin_token,
                )
