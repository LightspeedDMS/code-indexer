"""Bug #1996: the flat-file password audit log is created on the first record,
never at construction.

``auth/audit_logger.py`` builds a module-level ``password_audit_logger`` at
import, so a construction-time ``FileHandler`` open (plus ``mkdir``) made every
process that merely imports the server package -- the CLI included -- create
``~/.cidx-server/password_audit.log``.

Real logger, real files -- no mocks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.server.auth.audit_logger import PasswordChangeAuditLogger


def test_construction_creates_neither_file_nor_directory(tmp_path: Path) -> None:
    log_path = tmp_path / "missing" / "nested" / "audit.log"

    PasswordChangeAuditLogger(log_file_path=str(log_path))

    assert not log_path.exists()
    assert not (tmp_path / "missing").exists()


def test_default_path_construction_does_not_create_server_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server_dir = tmp_path / "server-data"
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(server_dir))

    logger = PasswordChangeAuditLogger()

    assert logger.log_file_path == str(server_dir / "password_audit.log")
    assert not server_dir.exists()


def test_first_record_creates_parents_and_file(tmp_path: Path) -> None:
    log_path = tmp_path / "missing" / "nested" / "audit.log"
    logger = PasswordChangeAuditLogger(log_file_path=str(log_path))

    logger.log_token_refresh_success(
        username="example-user",
        ip_address="192.0.2.10",
        family_id="example-family",
        user_agent="example-agent",
    )
    for handler in logger.audit_logger.handlers:
        handler.flush()

    assert log_path.is_file()
    assert "example-user" in log_path.read_text()
