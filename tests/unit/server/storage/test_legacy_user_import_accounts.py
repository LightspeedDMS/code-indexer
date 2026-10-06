"""The legacy users.json import never overwrites, augments or resurrects an
account.

Runs ``MigrationService`` exactly as server start-up does (``data/`` layout,
``is_migration_needed`` then ``migrate_all``) against a real SQLite accounts
store and real ``UserManager``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.migration_service import MigrationService
from tests.unit.server._account_rows import (
    PASSWORD,
    Stores,
    assert_account_has_no_inherited_rows,
    build_stores,
    seed_account_rows,
)

MIGRATION_LOGGER = "code_indexer.server.storage.migration_service"
CURRENT_PASSWORD = "Current-Example-Passw0rd!"
LEGACY_PASSWORD = "Legacy-Example-Passw0rd!"


@pytest.fixture
def server_dir(tmp_path: Path) -> Path:
    server = tmp_path / "server"
    (server / "data").mkdir(parents=True)
    DatabaseSchema(str(server / "data" / "cidx_server.db")).initialize_database()
    return server


def _users(server_dir: Path) -> UserManager:
    return UserManager(
        use_sqlite=True, db_path=str(server_dir / "data" / "cidx_server.db")
    )


def _legacy_entry(users: UserManager, password: str) -> Dict[str, Any]:
    return {
        "role": "admin",
        "password_hash": users.password_manager.hash_password(password),
        "created_at": "2024-01-01T00:00:00+00:00",
        "api_keys": [
            {
                "key_id": "legacy-key",
                "hash": "example-legacy-key-hash",
                "key_prefix": "cidx_sk_lega",
                "name": "legacy",
            }
        ],
    }


def _write_legacy(server_dir: Path, data: Dict[str, Any]) -> None:
    (server_dir / "users.json").write_text(json.dumps(data))


def _start_up(server_dir: Path) -> None:
    """The legacy-import step of server start-up."""
    migration = MigrationService(
        str(server_dir), str(server_dir / "data" / "cidx_server.db")
    )
    if migration.is_migration_needed():
        migration.migrate_all()


def test_existing_account_is_never_overwritten_or_augmented(server_dir: Path) -> None:
    users = _users(server_dir)
    users.create_user("alice", CURRENT_PASSWORD, UserRole.NORMAL_USER)
    _write_legacy(server_dir, {"alice": _legacy_entry(users, LEGACY_PASSWORD)})

    _start_up(server_dir)

    assert users.authenticate_user("alice", CURRENT_PASSWORD) is not None
    assert users.authenticate_user("alice", LEGACY_PASSWORD) is None
    assert users.get_api_keys("alice") == []


def test_removed_account_is_not_imported_again(server_dir: Path) -> None:
    """Once the import has run, a later start never re-imports a user, even
    when the first pass could not import every entry."""
    users = _users(server_dir)
    _write_legacy(
        server_dir,
        {"alice": _legacy_entry(users, LEGACY_PASSWORD), "broken": "not-an-entry"},
    )
    _start_up(server_dir)
    assert users.get_user("alice") is not None
    assert users.delete_user("alice")

    _start_up(server_dir)

    assert users.get_user("alice") is None


def test_entries_not_imported_are_reported_for_recreation(
    server_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The import runs once: every entry it could not import is reported at
    ERROR by name, with the reason, for an administrator to re-create."""
    users = _users(server_dir)
    _write_legacy(
        server_dir,
        {"alice": _legacy_entry(users, LEGACY_PASSWORD), "broken": "not-an-entry"},
    )

    with caplog.at_level(logging.ERROR, logger=MIGRATION_LOGGER):
        _start_up(server_dir)

    reports = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.ERROR and "re-create" in r.getMessage()
    ]
    assert len(reports) == 1
    assert "broken" in reports[0] and "alice" not in reports[0]
    assert "attribute" in reports[0]  # the reason the entry failed


def test_failed_rename_still_prevents_reimport(
    server_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Completion is recorded in the database, so a users.json that could
    not be renamed is never imported again."""
    from code_indexer.server.storage import migration_service

    users = _users(server_dir)
    _write_legacy(server_dir, {"alice": _legacy_entry(users, LEGACY_PASSWORD)})

    def _refuse_rename(src: str, dst: str) -> None:
        raise OSError("example rename failure")

    with monkeypatch.context() as patch:
        patch.setattr(migration_service.os, "rename", _refuse_rename)
        _start_up(server_dir)
    assert (server_dir / "users.json").exists()
    assert users.delete_user("alice")

    _start_up(server_dir)

    assert users.get_user("alice") is None


def _stores_with_legacy_alice(tmp_path: Path) -> Tuple[Stores, MigrationService]:
    stores = build_stores(tmp_path)
    entry = _legacy_entry(stores.user_manager, LEGACY_PASSWORD)
    entry.pop("api_keys")
    _write_legacy(stores.server_dir, {"alice": entry})
    migration = MigrationService(
        str(stores.server_dir),
        str(stores.server_dir / "data" / "cidx_server.db"),
        prepare_new_account=stores.user_manager.prepare_name_for_new_account,
    )
    return stores, migration


def test_imported_account_starts_clean(tmp_path: Path) -> None:
    """Rows left keyed to a name are removed before the import creates it."""
    stores, migration = _stores_with_legacy_alice(tmp_path)
    stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
    leftovers = seed_account_rows(stores, "alice")
    assert stores.user_manager.delete_user("alice")  # rows left behind

    migration.migrate_users()

    assert stores.user_manager.get_user("alice") is not None
    assert_account_has_no_inherited_rows(stores, "alice", leftovers)


def test_import_refused_while_previous_repositories_remain(tmp_path: Path) -> None:
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )
    from code_indexer.server.services.account_activations import (
        AccountActivations,
    )

    stores, migration = _stores_with_legacy_alice(tmp_path)
    arm = ActivatedRepoManager(data_dir=str(stores.server_dir / "data"))
    stores.user_manager.set_account_activations(AccountActivations(arm))
    clone = Path(arm.activated_repos_dir) / "alice" / "example-repo"
    clone.mkdir(parents=True)

    result = migration.migrate_users()

    assert result["errors"] == 1
    assert stores.user_manager.get_user("alice") is None


def test_bootstrap_admin_on_default_password_adopts_legacy_password(
    server_dir: Path,
) -> None:
    """The seeded admin (still on its default password) takes the legacy
    account's password, so an upgraded server never keeps the default."""
    users = _users(server_dir)
    users.seed_initial_admin()
    _write_legacy(server_dir, {"admin": _legacy_entry(users, LEGACY_PASSWORD)})

    _start_up(server_dir)

    assert users.authenticate_user("admin", LEGACY_PASSWORD) is not None
    assert users.authenticate_user("admin", "admin") is None


class _RecordingAccounts:
    """User-manager double recording the pre-creation step's calls."""

    def __init__(self) -> None:
        self.prepared: List[str] = []

    def prepare_name_for_new_account(self, username: str) -> None:
        self.prepared.append(username)


class TestStartupLegacyImportByStorageMode:
    """Start-up runs the local legacy users import only in solo (SQLite) mode:
    in cluster mode accounts live in PostgreSQL, so a local users.json is
    ignored and the shared pre-creation step (which purges shared rows) is
    never reached from it."""

    def test_cluster_mode_never_runs_the_legacy_users_import(
        self, server_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from code_indexer.server.startup.lifespan import _legacy_json_migration

        users = _users(server_dir)
        _write_legacy(server_dir, {"alice": _legacy_entry(users, LEGACY_PASSWORD)})
        accounts = _RecordingAccounts()
        migration = _legacy_json_migration(
            str(server_dir),
            str(server_dir / "data" / "cidx_server.db"),
            accounts,
            "postgres",
        )

        with caplog.at_level(logging.INFO, logger=MIGRATION_LOGGER):
            migration.migrate_all()

        assert accounts.prepared == []
        assert users.get_user("alice") is None
        assert (server_dir / "users.json").exists()
        assert any(
            r.levelno == logging.WARNING and "users.json" in r.getMessage()
            for r in caplog.records
        )

    def test_solo_mode_imports_through_the_pre_creation_step(
        self, server_dir: Path
    ) -> None:
        from code_indexer.server.startup.lifespan import _legacy_json_migration

        users = _users(server_dir)
        _write_legacy(server_dir, {"alice": _legacy_entry(users, LEGACY_PASSWORD)})
        accounts = _RecordingAccounts()
        migration = _legacy_json_migration(
            str(server_dir),
            str(server_dir / "data" / "cidx_server.db"),
            accounts,
            "sqlite",
        )

        migration.migrate_all()

        assert accounts.prepared == ["alice"]
        assert users.get_user("alice") is not None
