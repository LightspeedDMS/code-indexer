"""Stored-secret rows (CI tokens, git credentials) on SQLite: rewrites are
compare-and-set, unreadable rows are marked (never deleted) and listed
without their secret value."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Tuple

import pytest

from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends.ci_tokens_backend import (
    CITokensSqliteBackend,
)
from code_indexer.server.storage.sqlite_backends.git_credentials_backend import (
    GitCredentialsSqliteBackend,
)


def _db(tmp_path: Path) -> str:
    path = str(tmp_path / "server.db")
    DatabaseSchema(path).initialize_database()
    return path


def _ci(tmp_path: Path) -> Tuple[Any, str]:
    backend = CITokensSqliteBackend(_db(tmp_path))
    backend.save_token("github", "old-value", None)
    return backend, "github"


def _git(tmp_path: Path) -> Tuple[Any, str]:
    backend = GitCredentialsSqliteBackend(_db(tmp_path))
    backend.upsert_credential(
        credential_id="cred-1",
        username="alice",
        forge_type="github",
        forge_host="git.example.com",
        encrypted_token="old-value",
    )
    return backend, "cred-1"


@pytest.fixture(params=["ci", "git"])
def rows(request: Any, tmp_path: Path) -> Tuple[Any, str]:
    return _ci(tmp_path) if request.param == "ci" else _git(tmp_path)


def test_list_secret_rows_returns_id_value_and_mark(rows: Tuple[Any, str]) -> None:
    backend, row_id = rows
    listed = backend.list_secret_rows()
    assert [(r["id"], r["encrypted_token"], r["needs_reentry_at"]) for r in listed] == [
        (row_id, "old-value", None)
    ]


def test_cas_replaces_only_an_unchanged_value(rows: Tuple[Any, str]) -> None:
    backend, row_id = rows
    assert (
        backend.replace_encrypted_token_if_unchanged(row_id, "stale", "v2:x") is False
    )
    assert backend.list_secret_rows()[0]["encrypted_token"] == "old-value"
    assert backend.replace_encrypted_token_if_unchanged(row_id, "old-value", "v2:x")
    assert backend.list_secret_rows()[0]["encrypted_token"] == "v2:x"


def test_mark_is_compare_and_set_and_listed_without_secret(
    rows: Tuple[Any, str],
) -> None:
    backend, row_id = rows
    assert backend.mark_needs_reentry(row_id, "changed-meanwhile") is False
    assert backend.list_needs_reentry() == []
    assert backend.mark_needs_reentry(row_id, "old-value") is True
    marked = backend.list_needs_reentry()
    assert [m["id"] for m in marked] == [row_id]
    assert "encrypted_token" not in marked[0]
    assert "old-value" not in repr(marked)


def test_rewrite_and_clear_remove_the_mark(rows: Tuple[Any, str]) -> None:
    backend, row_id = rows
    backend.mark_needs_reentry(row_id, "old-value")
    assert backend.clear_needs_reentry(row_id, "old-value") is True
    assert backend.list_needs_reentry() == []
    backend.mark_needs_reentry(row_id, "old-value")
    backend.replace_encrypted_token_if_unchanged(row_id, "old-value", "v2:y")
    assert backend.list_needs_reentry() == []


def test_saving_a_new_value_clears_the_mark(tmp_path: Path) -> None:
    ci, _ = _ci(tmp_path / "a")
    ci.mark_needs_reentry("github", "old-value")
    ci.save_token("github", "v2:new", None)
    assert ci.list_needs_reentry() == []
    git, _ = _git(tmp_path / "b")
    git.mark_needs_reentry("cred-1", "old-value")
    git.upsert_credential(
        credential_id="cred-2",
        username="alice",
        forge_type="github",
        forge_host="git.example.com",
        encrypted_token="v2:new",
    )
    assert git.list_needs_reentry() == []


def test_mark_column_is_added_to_a_pre_existing_table(tmp_path: Path) -> None:
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE ci_tokens (platform TEXT PRIMARY KEY,"
        " encrypted_token TEXT NOT NULL, base_url TEXT)"
    )
    conn.execute("INSERT INTO ci_tokens VALUES ('gitlab', 'legacy', NULL)")
    conn.commit()
    conn.close()
    backend = CITokensSqliteBackend(path)
    assert backend.mark_needs_reentry("gitlab", "legacy") is True
    assert [m["id"] for m in backend.list_needs_reentry()] == ["gitlab"]
