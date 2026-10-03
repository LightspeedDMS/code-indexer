"""Bug #2022 Gap 4, review round 1 item 2: the persisted refresh failure
backoff belongs to the repo, not to its name. Removing a golden repo must
delete its backoff in the same transaction, so a repo later registered
under the same alias starts clean. Real stores: SQLite always, PostgreSQL
when ``TEST_POSTGRES_DSN`` is set."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

from tests.utils.golden_repo_metadata_stores import (
    STORE_KINDS,
    golden_repo_metadata_store,
)

BARE = "repo-x"
GLOBAL = "repo-x-global"
OTHER_GLOBAL = "repo-y-global"


@pytest.fixture(params=STORE_KINDS)
def metadata(request, tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store(request.param, tmp_path) as backend:
        yield backend


def _register(metadata: Any, repo_url: str) -> None:
    metadata.add_repo(
        alias=BARE,
        repo_url=repo_url,
        default_branch="main",
        clone_path=f"/data/golden-repos/{BARE}",
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def test_removed_alias_does_not_bequeath_its_backoff(metadata: Any) -> None:
    _register(metadata, "https://git.example.com/org/first.git")
    metadata.record_refresh_failure_backoff(GLOBAL, "disk full")
    metadata.record_refresh_failure_backoff(BARE, "disk full")
    metadata.record_refresh_failure_backoff(OTHER_GLOBAL, "disk full")

    assert metadata.remove_repo(BARE) is True
    _register(metadata, "https://git.example.com/org/second.git")

    assert metadata.get_refresh_failure_backoff_state(GLOBAL) is None
    assert metadata.get_refresh_failure_backoff_state(BARE) is None
    assert metadata.get_refresh_failure_backoff_state(OTHER_GLOBAL) is not None


def test_removing_unknown_alias_keeps_other_backoff(metadata: Any) -> None:
    metadata.record_refresh_failure_backoff(OTHER_GLOBAL, "disk full")

    assert metadata.remove_repo(BARE) is False

    assert metadata.get_refresh_failure_backoff_state(OTHER_GLOBAL) is not None


# Round 3 item 4: a system trigger deferred by the backoff is remembered
# durably (pending_trigger) and claimed by exactly one scheduler.


def test_new_failure_keeps_a_pending_trigger(metadata: Any) -> None:
    metadata.record_refresh_failure_backoff(GLOBAL, "disk full")
    metadata.mark_refresh_trigger_pending(GLOBAL)

    assert metadata.record_refresh_failure_backoff(GLOBAL, "disk full") == 2

    state = metadata.get_refresh_failure_backoff_state(GLOBAL)
    assert state["pending_trigger"] is True
    assert state["consecutive_failure_count"] == 2


def test_pending_trigger_is_claimed_exactly_once(metadata: Any) -> None:
    metadata.record_refresh_failure_backoff(GLOBAL, "disk full")
    metadata.record_refresh_failure_backoff(OTHER_GLOBAL, "disk full")
    metadata.mark_refresh_trigger_pending(GLOBAL)
    metadata.mark_refresh_trigger_pending("never-failed-global")  # no row: no-op

    pending = metadata.list_pending_refresh_triggers()
    assert [s["golden_alias"] for s in pending] == [GLOBAL]

    assert metadata.claim_pending_refresh_trigger(GLOBAL) is True
    assert metadata.claim_pending_refresh_trigger(GLOBAL) is False
    assert metadata.claim_pending_refresh_trigger(OTHER_GLOBAL) is False
    assert metadata.list_pending_refresh_triggers() == []
    assert metadata.get_refresh_failure_backoff_state(GLOBAL) is not None


def test_verified_success_drops_the_pending_trigger(metadata: Any) -> None:
    metadata.record_refresh_failure_backoff(GLOBAL, "disk full")
    metadata.mark_refresh_trigger_pending(GLOBAL)

    metadata.reset_refresh_failure_backoff(GLOBAL)

    assert metadata.list_pending_refresh_triggers() == []


def test_legacy_sqlite_table_gains_pending_trigger_column(tmp_path: Path) -> None:
    import sqlite3

    from code_indexer.server.storage.sqlite_backends._refresh_failure_backoff_mixin import (
        create_refresh_failure_backoff_table,
    )

    conn = sqlite3.connect(str(tmp_path / "legacy.db"))
    try:
        conn.execute(
            "CREATE TABLE refresh_failure_backoff_state (golden_alias TEXT "
            "PRIMARY KEY NOT NULL, consecutive_failure_count INTEGER NOT NULL "
            "DEFAULT 0, last_detail TEXT, last_failed_at REAL NOT NULL, "
            "updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO refresh_failure_backoff_state VALUES (?, 1, 'x', 1.0, NULL)",
            (GLOBAL,),
        )

        create_refresh_failure_backoff_table(conn)
        create_refresh_failure_backoff_table(conn)  # idempotent

        row = conn.execute(
            "SELECT pending_trigger FROM refresh_failure_backoff_state"
        ).fetchone()
        assert row == (0,)
    finally:
        conn.close()
