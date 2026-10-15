"""Stored credentials that cannot be decrypted are reported on health
(DEGRADED) by name, never by value."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from code_indexer.server.services.stored_credentials_health import (
    stored_credential_health_reasons,
)
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends.ci_tokens_backend import (
    CITokensSqliteBackend,
)
from code_indexer.server.storage.sqlite_backends.git_credentials_backend import (
    GitCredentialsSqliteBackend,
)

SECRET_VALUE = "undecryptable-ciphertext-value"


def _registry(tmp_path: Path, mark: bool) -> SimpleNamespace:
    db = str(tmp_path / "server.db")
    DatabaseSchema(db).initialize_database()
    ci = CITokensSqliteBackend(db)
    git = GitCredentialsSqliteBackend(db)
    ci.save_token("gitlab", SECRET_VALUE, None)
    git.upsert_credential(
        credential_id="cred-7",
        username="alice",
        forge_type="github",
        forge_host="git.example.com",
        encrypted_token=SECRET_VALUE,
    )
    if mark:
        ci.mark_needs_reentry("gitlab", SECRET_VALUE)
        git.mark_needs_reentry("cred-7", SECRET_VALUE)
    return SimpleNamespace(ci_tokens=ci, git_credentials=git)


def test_health_reason_is_count_only_without_identifying_detail(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path, mark=False)
    registry.ci_tokens.mark_needs_reentry("gitlab", SECRET_VALUE, node_id="node-a")
    registry.git_credentials.mark_needs_reentry("cred-7", SECRET_VALUE, "node-a")
    reasons = stored_credential_health_reasons(registry)
    assert reasons == ["2 stored credentials need re-entry (see the credentials pages)"]
    text = " ".join(reasons)
    for detail in ("alice", "git.example.com", "cred-7", "gitlab", "node-a"):
        assert detail not in text
    assert SECRET_VALUE not in text


def test_no_marks_no_reasons(tmp_path: Path) -> None:
    assert stored_credential_health_reasons(_registry(tmp_path, mark=False)) == []


def test_unreadable_state_is_reported() -> None:
    reasons = stored_credential_health_reasons(SimpleNamespace())
    assert len(reasons) == 1 and "unreadable" in reasons[0]


def test_collector_is_degraded_only_and_reads_app_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.app import app as app_module
    from code_indexer.server.services.health_service import HealthCheckService

    service: Any = HealthCheckService.__new__(HealthCheckService)
    monkeypatch.setattr(
        app_module.state, "backend_registry", _registry(tmp_path, True), raising=False
    )
    warn, err, reasons = service._collect_stored_credential_failures()
    assert (warn, err, len(reasons)) == (True, False, 1)
    monkeypatch.setattr(app_module.state, "backend_registry", None, raising=False)
    assert service._collect_stored_credential_failures() == (False, False, [])
