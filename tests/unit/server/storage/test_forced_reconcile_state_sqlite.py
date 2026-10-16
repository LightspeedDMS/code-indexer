"""Durable per-repo forced-reconcile state (SQLite backend).

The refresh scheduler counts consecutive forced reconciles that left the
SAME stale-index signal in place, so it can stop forcing after a small
bound. The count must survive a scheduler restart, restart at 1 when the
signal changes, and disappear with the repository. The scenario below is
shared with the PostgreSQL test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

ALIAS = "example-repo-global"
SIGNAL_A = "metadata-voyage-ai.json status=failed HEAD=abc"
SIGNAL_B = "metadata-voyage-ai.json status=failed HEAD=def"


def run_forced_reconcile_state_scenario(
    backend: Any, reopen: Callable[[], Any], register_repo: Callable[[], None]
) -> None:
    assert backend.get_forced_reconcile_state(ALIAS) is None

    assert backend.record_forced_reconcile(ALIAS, SIGNAL_A) == 1
    assert backend.record_forced_reconcile(ALIAS, SIGNAL_A) == 2
    assert backend.record_forced_reconcile(ALIAS, SIGNAL_A) == 3
    state = backend.get_forced_reconcile_state(ALIAS)
    assert state == {"signal": SIGNAL_A, "attempt_count": 3}

    # Survives a restart (a fresh backend over the same database).
    assert reopen().get_forced_reconcile_state(ALIAS) == state

    # A changed signal restarts the count.
    assert backend.record_forced_reconcile(ALIAS, SIGNAL_B) == 1
    assert backend.get_forced_reconcile_state(ALIAS) == {
        "signal": SIGNAL_B,
        "attempt_count": 1,
    }

    backend.clear_forced_reconcile_state(ALIAS)
    assert backend.get_forced_reconcile_state(ALIAS) is None

    # Removing the repository drops its state (bare alias removes -global).
    register_repo()
    backend.record_forced_reconcile(ALIAS, SIGNAL_A)
    assert backend.remove_repo("example-repo")
    assert backend.get_forced_reconcile_state(ALIAS) is None


def test_forced_reconcile_state_sqlite(tmp_path: Path) -> None:
    from code_indexer.server.storage.sqlite_backends import (
        GoldenRepoMetadataSqliteBackend,
    )

    db_path = str(tmp_path / "cidx_server.db")

    def open_backend() -> GoldenRepoMetadataSqliteBackend:
        backend = GoldenRepoMetadataSqliteBackend(db_path)
        backend.ensure_table_exists()
        return backend

    backend = open_backend()

    def register_repo() -> None:
        backend.add_repo(
            alias="example-repo",
            repo_url="https://example.com/example-repo.git",
            default_branch="main",
            clone_path=str(tmp_path / "example-repo"),
            created_at="2026-01-01T00:00:00+00:00",
        )

    run_forced_reconcile_state_scenario(backend, open_backend, register_repo)


def test_remove_repo_on_schema_initialized_database(tmp_path: Path) -> None:
    """A database created by DatabaseSchema alone (server startup) must
    hold the table remove_repo deletes from."""
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.sqlite_backends import (
        GoldenRepoMetadataSqliteBackend,
    )

    db_path = str(tmp_path / "cidx_server.db")
    DatabaseSchema(db_path).initialize_database()
    backend = GoldenRepoMetadataSqliteBackend(db_path)
    backend.add_repo(
        alias="example-repo",
        repo_url="https://example.com/example-repo.git",
        default_branch="main",
        clone_path=str(tmp_path / "example-repo"),
        created_at="2026-01-01T00:00:00+00:00",
    )
    assert backend.record_forced_reconcile(ALIAS, SIGNAL_A) == 1
    assert backend.remove_repo("example-repo")
    assert backend.get_forced_reconcile_state(ALIAS) is None
