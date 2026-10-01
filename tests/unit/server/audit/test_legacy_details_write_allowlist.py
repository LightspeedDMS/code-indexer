"""Legacy audit writers store only the fields the read side may show.

The pre-existing writers (PR-creation audit, group management, the legacy
authentication events) build their payload as free-form dicts.  The row
builder applies the read-side legacy allowlist (``LEGACY_DETAILS_READ_SCHEMA``
and the PR-URL rule) at WRITE time too, so a credential or free text in such
a payload is never stored.  Rows written before this rule are still shown
only through the read projection (no migration rewrites them).

Real stores throughout: ``AuditLogService`` on SQLite files, the real
``PasswordChangeAuditLogger``, a real ``GroupAccessManager`` and the real MCP
dispatcher (``_audit_front_doors``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator

import pytest

from _audit_front_doors import DoorsEnv, front_door_env
from code_indexer.server.auth.audit_logger import PasswordChangeAuditLogger
from code_indexer.server.services.audit_log_service import AuditLogService
from tests.unit.server._audit_read_support import (
    audit_logs,
    make_event,
    seed,
    stored_audit_rows,
)

_SECRET = "example-secret-value"
_TOKEN_URL = f"https://ci-bot:{_SECRET}@forge.example.com/example-org/example-repo.git"
_PLAIN_PR_URL = "https://github.com/example-org/example-repo/pull/7"


def _only_stored(db_path: Path) -> Dict[str, Any]:
    rows = stored_audit_rows(db_path)
    assert len(rows) == 1, rows
    return rows[0]


def _stored_details(row: Dict[str, Any]) -> Dict[str, Any]:
    raw = row["details"]
    assert raw is not None
    decoded: Dict[str, Any] = json.loads(raw)
    return decoded


def test_pr_creation_failure_stores_only_allowlisted_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "audit.db"
    writer = PasswordChangeAuditLogger(audit_service=AuditLogService(db_path))

    writer.log_pr_creation_failure(
        job_id="job-7",
        repo_alias="example-repo",
        reason=f"push to {_TOKEN_URL} rejected",
        branch_name="fix/example",
        additional_context={
            "remote": _TOKEN_URL,
            "api_token": _SECRET,
            "nested": {"url": _TOKEN_URL},
        },
    )

    row = _only_stored(db_path)
    assert _stored_details(row) == {
        "job_id": "job-7",
        "repo_alias": "example-repo",
        "branch_name": "fix/example",
    }
    assert _SECRET not in json.dumps(row)


def test_pr_creation_success_stores_the_plain_pr_url(tmp_path: Path) -> None:
    db_path = tmp_path / "audit.db"
    writer = PasswordChangeAuditLogger(audit_service=AuditLogService(db_path))

    writer.log_pr_creation_success(
        job_id="job-8",
        repo_alias="example-repo",
        branch_name="fix/example",
        pr_url=(
            f"https://ci-bot:{_SECRET}@github.com/example-org/example-repo/pull/7"
            f"?private_token={_SECRET}#top"
        ),
        commit_hash="abc123",
        files_modified=["src/example.py"],
        additional_context={"remote": _TOKEN_URL},
    )

    row = _only_stored(db_path)
    assert _stored_details(row) == {
        "job_id": "job-8",
        "repo_alias": "example-repo",
        "branch_name": "fix/example",
        "pr_url": _PLAIN_PR_URL,
        "commit_hash": "abc123",
    }
    assert _SECRET not in json.dumps(row)


def test_a_pr_url_that_is_not_a_pull_request_path_is_not_stored(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "audit.db"
    writer = PasswordChangeAuditLogger(audit_service=AuditLogService(db_path))

    writer.log_pr_creation_success(
        job_id="job-9",
        repo_alias="example-repo",
        branch_name="fix/example",
        pr_url=f"https://forge.example.com/example-org/{_SECRET}/pull/7;x=y",
        commit_hash="abc123",
        files_modified=[],
    )

    details = _stored_details(_only_stored(db_path))
    assert "pr_url" not in details
    assert _SECRET not in json.dumps(details)


def test_standalone_group_manager_row_has_no_description(tmp_path: Path) -> None:
    from code_indexer.server.services.group_access_manager import (
        GroupAccessManager,
    )

    db_path = tmp_path / "groups.db"
    groups = GroupAccessManager(db_path)
    groups.log_audit(
        admin_id="example-admin",
        action_type="group_create",
        target_type="group",
        target_id="4",
        details={"name": "example-team", "description": _SECRET},
    )

    rows = [r for r in stored_audit_rows(db_path) if r["action_type"] == "group_create"]
    assert len(rows) == 1
    assert json.loads(rows[0]["details"]) == {"name": "example-team"}


@pytest.fixture()
def env(tmp_path: Path, monkeypatch) -> Iterator[DoorsEnv]:
    yield from front_door_env(tmp_path, monkeypatch)


def test_mcp_group_create_row_has_no_description(env: DoorsEnv) -> None:
    result = env.mcp("create_group", {"name": "example-team", "description": _SECRET})
    assert result["success"] is True
    row = env.only_row("group_create")
    assert row.details == {"name": "example-team"}
    assert _SECRET not in (row.raw_details or "")


def test_mcp_group_update_row_has_no_description(env: DoorsEnv) -> None:
    created = env.mcp("create_group", {"name": "example-team"})
    assert created["success"] is True
    result = env.mcp(
        "update_group",
        {"group_id": str(created["group_id"]), "description": _SECRET},
    )
    assert result["success"] is True
    row = env.only_row("group_update")
    assert row.details == {"name": "example-team"}
    assert _SECRET not in (row.raw_details or "")


def test_rows_stored_before_the_write_rule_still_read_through_the_projection(
    tmp_path: Path,
) -> None:
    """A row written unrestricted is shown only through the read projection."""
    store = AuditLogService(tmp_path / "audit.db")
    seed(
        store,
        [
            make_event(
                ts="2026-09-01T10:00:00+00:00",
                action_type="group_create",
                target_type="group",
                target_id="4",
                details_json=json.dumps(
                    {"name": "example-team", "description": _SECRET}
                ),
            ),
            make_event(
                ts="2026-09-01T10:01:00+00:00",
                action_type="pr_creation_success",
                target_type="auth",
                target_id="example-repo",
                actor="system",
                details_json=json.dumps(
                    {
                        "job_id": "job-1",
                        "pr_url": (
                            f"https://ci-bot:{_SECRET}@github.com/example-org/"
                            "example-repo/pull/7"
                        ),
                        "additional_context": {"remote": _TOKEN_URL},
                    }
                ),
            ),
        ],
    )

    rows, total = audit_logs(store)
    assert total == 2
    by_type = {r["action_type"]: json.loads(r["details"]) for r in rows}
    assert by_type["group_create"] == {
        "name": "example-team",
        "omitted_fields": ["description"],
    }
    assert by_type["pr_creation_success"] == {
        "job_id": "job-1",
        "pr_url": _PLAIN_PR_URL,
        "omitted_fields": ["additional_context"],
    }
    assert _SECRET not in json.dumps(rows)
