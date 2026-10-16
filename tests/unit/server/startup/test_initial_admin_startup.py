"""Start-up seeds the initial administrator only into an empty user store.

Drives the real start-up steps in their start-up order: the seeding step
(``seed_initial_admin_at_startup``, run under the bootstrap lock) and then
the legacy users.json import (``_legacy_json_migration(...).migrate_all()``),
over a real ``cidx_server.db`` in a scratch server directory.
"""

from __future__ import annotations

import json
import logging
from contextlib import ExitStack
from pathlib import Path
from typing import List, Tuple
from unittest.mock import patch

import pytest

from code_indexer.server.auth.password_manager import PasswordManager
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.startup.lifespan import _legacy_json_migration
from code_indexer.server.startup.service_init import seed_initial_admin_at_startup
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.migration_service import MigrationService
from tests.unit.server._account_rows import PASSWORD as OPERATOR_PASSWORD

OPERATOR = "example-operator"
SERVICE_INIT_LOGGER = "code_indexer.server.startup.service_init"
# Installer steps that download external tools (skipped in these tests).
EXTERNAL_TOOL_STEPS = (
    "install_claude_cli",
    "install_scip_indexers",
    "install_ripgrep",
    "install_coursier",
)


@pytest.fixture
def server_dir(tmp_path: Path) -> Path:
    server = tmp_path / "server"
    (server / "data").mkdir(parents=True)
    DatabaseSchema(str(_db(server))).initialize_database()
    return server


def _db(server: Path) -> Path:
    return server / "data" / "cidx_server.db"


def _users(server: Path) -> UserManager:
    return UserManager(use_sqlite=True, db_path=str(_db(server)))


def _write_legacy_users(server: Path, names: List[str]) -> None:
    password_hash = PasswordManager().hash_password(OPERATOR_PASSWORD)
    entries = {
        name: {"role": "admin", "password_hash": password_hash} for name in names
    }
    (server / "users.json").write_text(json.dumps(entries))


def _start_up(server: Path, storage_mode: str = "sqlite") -> bool:
    """The start-up seeding step, then the start-up users.json import."""
    users = _users(server)
    seeded = seed_initial_admin_at_startup(
        users, str(server), str(_db(server)), storage_mode
    )
    migration = _legacy_json_migration(str(server), str(_db(server)), users, "sqlite")
    if migration.is_migration_needed():
        migration.migrate_all()
    return seeded


def _names(server: Path) -> List[str]:
    return sorted(u.username for u in _users(server).get_all_users())


def _record_users_import(server: Path) -> None:
    MigrationService(str(server), str(_db(server)))._record_users_import()


def _seed_messages(caplog: pytest.LogCaptureFixture) -> List[Tuple[int, str]]:
    return [
        (r.levelno, r.getMessage())
        for r in caplog.records
        if r.getMessage().startswith("Initial admin")
    ]


def test_pending_legacy_users_import_prevents_seeding(server_dir: Path) -> None:
    _write_legacy_users(server_dir, [OPERATOR])

    seeded = _start_up(server_dir)

    assert _names(server_dir) == [OPERATOR]
    assert seeded is False


def test_startup_logs_seeding_outcome(
    server_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger=SERVICE_INIT_LOGGER):
        assert _start_up(server_dir) is True
        assert _start_up(server_dir) is False

    assert _names(server_dir) == ["admin"]
    assert _users(server_dir).authenticate_user("admin", "admin") is not None
    assert _seed_messages(caplog) == [
        (logging.INFO, "Initial admin seeded into the empty user store"),
        (logging.INFO, "Initial admin not seeded: the user store already has users"),
    ]


def test_startup_logs_skip_when_legacy_import_pending(
    server_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write_legacy_users(server_dir, [OPERATOR])

    with caplog.at_level(logging.INFO, logger=SERVICE_INIT_LOGGER):
        _start_up(server_dir)

    assert _seed_messages(caplog) == [
        (logging.INFO, "Initial admin not seeded: legacy users.json import pending")
    ]


@pytest.mark.parametrize("recorded", [False, True])
def test_empty_or_recorded_legacy_file_does_not_block_seeding(
    server_dir: Path, recorded: bool
) -> None:
    """An empty users.json, or one whose import already ran, holds no
    accounts still to import: an empty store is seeded."""
    if recorded:
        _write_legacy_users(server_dir, [OPERATOR])
        _record_users_import(server_dir)
    else:
        (server_dir / "users.json").write_text("{}")

    assert _start_up(server_dir) is True

    assert _names(server_dir) == ["admin"]


def test_cluster_mode_does_not_wait_for_local_users_json(server_dir: Path) -> None:
    """Cluster nodes never import a local users.json."""
    _write_legacy_users(server_dir, [OPERATOR])

    seeded = seed_initial_admin_at_startup(
        _users(server_dir), str(server_dir), str(_db(server_dir)), "postgres"
    )

    assert seeded is True
    assert _names(server_dir) == ["admin"]


@pytest.fixture
def installed_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The installer's home and server directory, in a scratch location."""
    monkeypatch.setenv("HOME", str(tmp_path))
    server = tmp_path / ".cidx-server"
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(server))
    return server


def _run_installer() -> None:
    """The real installer; only its external tool downloads are skipped."""
    from code_indexer.server.installer import ServerInstaller

    with ExitStack() as stack:
        for step in EXTERNAL_TOOL_STEPS:
            stack.enter_context(patch.object(ServerInstaller, step))
        ServerInstaller().install()


def test_installer_then_startup_keeps_existing_users(installed_home: Path) -> None:
    (installed_home / "data").mkdir(parents=True)
    DatabaseSchema(str(_db(installed_home))).initialize_database()
    _users(installed_home).create_user(OPERATOR, OPERATOR_PASSWORD, UserRole.ADMIN)

    _run_installer()
    _start_up(installed_home)

    assert _names(installed_home) == [OPERATOR]


def test_fresh_install_then_first_start_has_working_initial_admin(
    installed_home: Path,
) -> None:
    _run_installer()
    assert not (installed_home / "users.json").exists()

    (installed_home / "data").mkdir(parents=True, exist_ok=True)
    DatabaseSchema(str(_db(installed_home))).initialize_database()
    assert _start_up(installed_home) is True

    assert _names(installed_home) == ["admin"]
    assert _users(installed_home).authenticate_user("admin", "admin") is not None


def _import_pending(server: Path) -> bool:
    return MigrationService(str(server), str(_db(server))).has_pending_users_import()


def test_failed_legacy_import_stays_pending_and_blocks_seeding(
    server_dir: Path,
) -> None:
    (server_dir / "users.json").write_text(
        json.dumps({"example-broken": "not-an-entry"})
    )

    _start_up(server_dir)
    _start_up(server_dir)

    assert _names(server_dir) == []
    assert (server_dir / "users.json").exists()
    assert _import_pending(server_dir) is True
