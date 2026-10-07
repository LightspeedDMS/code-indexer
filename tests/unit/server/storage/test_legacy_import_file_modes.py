"""Legacy files holding credentials stay owner-only through the import.

The import rewrites users.json (entries still to import) or renames it to
``.migrated``, and renames ci_tokens.json to ``.migrated``.  Whatever the
process umask or the original file mode, the result is owner-only, and a
stricter original mode is never loosened.  Real files, real SQLite store.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Iterator

import pytest

from code_indexer.server.auth.password_manager import PasswordManager
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.migration_service import MigrationService
from tests.unit.server._account_rows import PASSWORD


@pytest.fixture
def permissive_umask() -> Iterator[None]:
    previous = os.umask(0o000)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture
def server_dir(tmp_path: Path) -> Path:
    server = tmp_path / "server"
    (server / "data").mkdir(parents=True)
    DatabaseSchema(str(server / "data" / "cidx_server.db")).initialize_database()
    return server


def _migration(server: Path) -> MigrationService:
    return MigrationService(str(server), str(server / "data" / "cidx_server.db"))


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _write_users(server: Path, entries: dict, mode: int) -> Path:
    path = server / "users.json"
    path.write_text(json.dumps(entries))
    path.chmod(mode)
    return path


def _valid_entry() -> dict:
    return {"role": "admin", "password_hash": PasswordManager().hash_password(PASSWORD)}


def test_rewritten_users_json_is_owner_only(
    server_dir: Path, permissive_umask: None
) -> None:
    users_json = _write_users(
        server_dir,
        {"example-alice": _valid_entry(), "example-broken": "not-an-entry"},
        0o644,
    )

    _migration(server_dir).migrate_users()

    assert users_json.exists()
    assert _mode(users_json) == 0o600


def test_imported_users_json_is_kept_owner_only(
    server_dir: Path, permissive_umask: None
) -> None:
    _write_users(server_dir, {"example-alice": _valid_entry()}, 0o644)

    _migration(server_dir).migrate_users()

    assert _mode(server_dir / "users.json.migrated") == 0o600


def test_imported_ci_tokens_json_is_kept_owner_only(
    server_dir: Path, permissive_umask: None
) -> None:
    tokens = server_dir / "ci_tokens.json"
    tokens.write_text(json.dumps({"github": {"token": "example-encrypted-token"}}))
    tokens.chmod(0o644)

    _migration(server_dir).migrate_ci_tokens()

    assert _mode(server_dir / "ci_tokens.json.migrated") == 0o600


def test_stricter_original_mode_is_never_loosened(
    server_dir: Path, permissive_umask: None
) -> None:
    _write_users(server_dir, {"example-alice": _valid_entry()}, 0o400)

    _migration(server_dir).migrate_users()

    assert _mode(server_dir / "users.json.migrated") == 0o400


def test_pending_users_json_is_owner_only_when_rewrite_fails(
    server_dir: Path, permissive_umask: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.storage import migration_service

    users_json = _write_users(server_dir, {"example-broken": "not-an-entry"}, 0o644)

    def _refuse_replace(src: str, dst: str) -> None:
        raise OSError("example replace failure")

    with monkeypatch.context() as patch:
        patch.setattr(migration_service.os, "replace", _refuse_replace)
        _migration(server_dir).migrate_users()

    assert users_json.exists()
    assert _mode(users_json) == 0o600
