"""Login outcome entry point: one row per attempt, issuance failures included.

Uses a REAL AuditLogService on a real temporary SQLite file, bound as the
process audit sink.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest

from code_indexer.server.auth.login_outcome import (
    complete_login,
    login_actor,
    reject_login,
)
from code_indexer.server.services import audit_capture
from code_indexer.server.services.audit_log_service import AuditLogService


@pytest.fixture()
def db_path(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "groups.db"
    service = AuditLogService(path)
    service.start()
    audit_capture.mark_server_process()
    audit_capture.bind_audit_service(service, node_id=None)
    try:
        yield path
    finally:
        service.stop()


def _rows(db_path: Path) -> List[Tuple]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT action_type, admin_id, outcome, auth_method, details "
            "FROM audit_logs ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def test_issuance_failure_is_recorded_and_propagates(db_path: Path) -> None:
    def _broken_issue() -> None:
        raise RuntimeError("token store unavailable")

    with pytest.raises(RuntimeError):
        complete_login(
            "alice",
            method="password",
            mfa="not_enrolled",
            flow="rest_token",
            issue=_broken_issue,
        )
    rows = _rows(db_path)
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
        ("authentication_failure", "alice", "failure", "none")
    ]
    assert json.loads(rows[0][4]) == {
        "method": "password",
        "stage": "issuance",
        "reason": "server_error",
    }


def test_successful_issuance_returns_its_result(db_path: Path) -> None:
    result = complete_login(
        "alice",
        method="password",
        mfa="totp",
        flow="web_session",
        issue=lambda: {"token": "issued"},
    )
    assert result == {"token": "issued"}
    assert [(r[0], r[2], r[3]) for r in _rows(db_path)] == [
        ("authentication_success", "success", "web_session")
    ]


def test_reject_login_with_no_username_uses_the_placeholder(db_path: Path) -> None:
    reject_login(
        None,
        account_exists=False,
        method="password",
        stage="challenge",
        reason="challenge_invalid_or_expired",
    )
    assert [r[1] for r in _rows(db_path)] == ["(unknown)"]


def test_reject_login_for_no_such_account_never_stores_the_typed_text(
    db_path: Path,
) -> None:
    reject_login(
        "Tr0ub4dor&3-horse",
        account_exists=False,
        method="password",
        stage="credentials",
        reason="bad_credentials",
    )
    assert [(r[0], r[1]) for r in _rows(db_path)] == [
        ("authentication_failure", "(unknown)")
    ]


@pytest.mark.parametrize(
    "typed, exists, recorded",
    [
        ("alice", True, "alice"),
        ("first last", True, "first last"),
        ("alice", False, "(unknown)"),
        ("bad/name", True, "(unknown)"),
        ("", True, "(unknown)"),
        (None, True, "(unknown)"),
        ("x" * 256, True, "(unknown)"),
    ],
)
def test_login_actor(typed, exists, recorded) -> None:
    assert login_actor(typed, account_exists=exists) == recorded
