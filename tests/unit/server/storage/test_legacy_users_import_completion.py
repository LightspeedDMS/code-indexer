"""The legacy users.json import completes only when every entry is resolved.

Real ``MigrationService`` over a real, migrated ``cidx_server.db``.  A stop
of the process is simulated by raising a ``BaseException`` from the step
after which the process "stops"; the next start then runs the import again.
Whatever the stopping point, the store either holds the imported rows or the
import is still pending -- never neither (start-up would then seed the
initial administrator although legacy accounts were meant to exist).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from code_indexer.server.auth.password_manager import PasswordManager
from code_indexer.server.auth.user_manager import UserManager
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.migration_service import MigrationService
from tests.unit.server._account_rows import PASSWORD

ACCOUNT = "example-alice"
BROKEN = "example-broken"


@pytest.fixture
def server_dir(tmp_path: Path) -> Path:
    server = tmp_path / "server"
    (server / "data").mkdir(parents=True)
    DatabaseSchema(str(server / "data" / "cidx_server.db")).initialize_database()
    return server


def _migration(server: Path) -> MigrationService:
    return MigrationService(str(server), str(server / "data" / "cidx_server.db"))


def _state(server: Path) -> Tuple[List[str], bool, bool]:
    """(account names, import pending, completion recorded)."""
    users = UserManager(
        use_sqlite=True, db_path=str(server / "data" / "cidx_server.db")
    )
    names = sorted(u.username for u in users.get_all_users())
    migration = _migration(server)
    return (
        names,
        migration.has_pending_users_import(),
        migration._users_import_recorded(),
    )


def _write_legacy(server: Path, names: List[str]) -> None:
    password_hash = PasswordManager().hash_password(PASSWORD)
    entries: Dict[str, Any] = {
        name: (
            "not-an-entry"
            if name == BROKEN
            else {"role": "admin", "password_hash": password_hash}
        )
        for name in names
    }
    (server / "users.json").write_text(json.dumps(entries))


def test_successful_import_completes_and_renames(server_dir: Path) -> None:
    _write_legacy(server_dir, [ACCOUNT])

    _migration(server_dir).migrate_users()

    assert _state(server_dir) == ([ACCOUNT], False, True)
    assert not (server_dir / "users.json").exists()
    assert (server_dir / "users.json.migrated").exists()


def test_partial_failure_keeps_only_failed_entries_pending(server_dir: Path) -> None:
    _write_legacy(server_dir, [ACCOUNT, BROKEN])

    _migration(server_dir).migrate_users()
    _migration(server_dir).migrate_users()

    assert _state(server_dir) == ([ACCOUNT], True, False)
    remaining = json.loads((server_dir / "users.json").read_text())
    assert list(remaining) == [BROKEN]


class _Stop(BaseException):
    """The process stopping at this point."""


def _stop(*_args: Any) -> None:
    raise _Stop()


@pytest.mark.parametrize(
    "step, names, final",
    [
        ("_record_users_import", [ACCOUNT], ([ACCOUNT], False, True)),
        ("_rename_imported", [ACCOUNT], ([ACCOUNT], False, True)),
        ("_keep_pending_entries", [ACCOUNT, BROKEN], ([ACCOUNT], True, False)),
    ],
)
def test_stop_after_any_step_never_leaves_no_rows_and_not_pending(
    server_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    step: str,
    names: List[str],
    final: Tuple[List[str], bool, bool],
) -> None:
    _write_legacy(server_dir, names)

    with monkeypatch.context() as patch:
        patch.setattr(MigrationService, step, staticmethod(_stop))
        with pytest.raises(_Stop):
            _migration(server_dir).migrate_users()
    stored, pending, _recorded = _state(server_dir)
    assert stored or pending

    _migration(server_dir).migrate_users()  # the next start

    assert _state(server_dir) == final
