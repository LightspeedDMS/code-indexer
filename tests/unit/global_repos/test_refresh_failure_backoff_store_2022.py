"""Bug #2022 Gap 4, review round 1 item 2: the persisted refresh failure
backoff belongs to the repo, not to its name. Removing a golden repo must
delete its backoff in the same transaction, so a repo later registered
under the same alias starts clean. Real stores: SQLite always, PostgreSQL
when ``TEST_POSTGRES_DSN`` is set."""

from __future__ import annotations

import time
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


# A system trigger deferred by the backoff is remembered durably. A
# scheduler LEASES a due trigger (exclusive, and the trigger stays pending
# so a crash before the refresh completes loses nothing); only a verified
# publish resolves it -- unless a new trigger arrived during that cycle.


def _due(metadata: Any, now: float) -> list:
    return [s["golden_alias"] for s in metadata.list_due_refresh_triggers(now)]


def test_new_failure_keeps_a_pending_trigger(metadata: Any) -> None:
    metadata.record_refresh_failure_backoff(GLOBAL, "disk full")
    metadata.mark_refresh_trigger_pending(GLOBAL, time.time() + 60)

    assert metadata.record_refresh_failure_backoff(GLOBAL, "disk full") == 2

    state = metadata.get_refresh_failure_backoff_state(GLOBAL)
    assert state["pending_trigger"] is True
    assert state["consecutive_failure_count"] == 2


def test_mark_reports_whether_a_backoff_row_exists(metadata: Any) -> None:
    metadata.record_refresh_failure_backoff(GLOBAL, "disk full")

    assert metadata.mark_refresh_trigger_pending(GLOBAL, time.time()) is True
    assert metadata.mark_refresh_trigger_pending("never-failed-global", 0.0) is False
    assert metadata.get_refresh_failure_backoff_state("never-failed-global") is None


def test_only_due_triggers_are_listed(metadata: Any) -> None:
    now = time.time()
    for alias in (GLOBAL, OTHER_GLOBAL, BARE):
        metadata.record_refresh_failure_backoff(alias, "disk full")
    metadata.mark_refresh_trigger_pending(GLOBAL, now - 1)
    metadata.mark_refresh_trigger_pending(OTHER_GLOBAL, now + 600)  # not yet due

    assert _due(metadata, now) == [GLOBAL]  # BARE carries no trigger
    assert sorted(_due(metadata, now + 601)) == sorted([GLOBAL, OTHER_GLOBAL])


def test_lease_is_exclusive_and_keeps_the_trigger_pending(metadata: Any) -> None:
    now = time.time()
    metadata.record_refresh_failure_backoff(GLOBAL, "disk full")
    metadata.mark_refresh_trigger_pending(GLOBAL, now - 1)

    assert metadata.lease_pending_refresh_trigger(GLOBAL, now, now + 300) is True
    assert metadata.lease_pending_refresh_trigger(GLOBAL, now, now + 300) is False

    state = metadata.get_refresh_failure_backoff_state(GLOBAL)
    assert state["pending_trigger"] is True, "a lease must not consume the trigger"
    assert _due(metadata, now) == []
    assert _due(metadata, now + 300) == [GLOBAL], "lease end makes it due again"


def _generation(metadata: Any) -> int:
    state = metadata.get_refresh_failure_backoff_state(GLOBAL)
    return 0 if state is None else int(state["trigger_generation"])


def test_mark_advances_the_trigger_generation(metadata: Any) -> None:
    metadata.record_refresh_failure_backoff(GLOBAL, "disk full")
    before = _generation(metadata)

    metadata.mark_refresh_trigger_pending(GLOBAL, time.time())
    metadata.mark_refresh_trigger_pending(GLOBAL, time.time())

    assert _generation(metadata) == before + 2


def test_publish_deletes_a_trigger_its_cycle_covered(metadata: Any) -> None:
    metadata.record_refresh_failure_backoff(GLOBAL, "disk full")
    metadata.mark_refresh_trigger_pending(GLOBAL, time.time())
    covered = _generation(metadata)  # the cycle began after the trigger

    metadata.resolve_refresh_failure_backoff(GLOBAL, covered)

    assert metadata.get_refresh_failure_backoff_state(GLOBAL) is None


#: Cross-node clock skew observed on a staging VM.
CLOCK_LAG_SECONDS = 70.0


def test_publish_rearms_a_trigger_marked_after_the_cycle_began_whatever_the_clocks(
    metadata: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata.record_refresh_failure_backoff(GLOBAL, "disk full")
    covered = _generation(metadata)  # captured at cycle start
    real_time = time.time
    with monkeypatch.context() as lagging_node:
        lagging_node.setattr(time, "time", lambda: real_time() - CLOCK_LAG_SECONDS)
        metadata.mark_refresh_trigger_pending(GLOBAL, time.time() + 600)

    metadata.resolve_refresh_failure_backoff(GLOBAL, covered)

    state = metadata.get_refresh_failure_backoff_state(GLOBAL)
    assert state is not None, "a trigger that arrived during the cycle was lost"
    assert state["pending_trigger"] is True
    assert state["consecutive_failure_count"] == 0, "the failure is resolved"
    assert _due(metadata, time.time()) == [GLOBAL], "it fires without waiting"


def _legacy_round3_table(path: Path) -> Any:
    """A table as created before the pending-trigger columns existed."""
    import sqlite3

    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE refresh_failure_backoff_state (golden_alias TEXT "
        "PRIMARY KEY NOT NULL, consecutive_failure_count INTEGER NOT NULL "
        "DEFAULT 0, last_detail TEXT, last_failed_at REAL NOT NULL, "
        "updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO refresh_failure_backoff_state VALUES (?, 1, 'x', 5.0, NULL)",
        (GLOBAL,),
    )
    return conn


def test_legacy_sqlite_table_is_upgraded_and_backfilled(tmp_path: Path) -> None:
    from code_indexer.server.storage.sqlite_backends._refresh_failure_backoff_mixin import (
        create_refresh_failure_backoff_table,
    )

    conn = _legacy_round3_table(tmp_path / "legacy.db")
    try:
        create_refresh_failure_backoff_table(conn)
        conn.execute("UPDATE refresh_failure_backoff_state SET pending_due_at = NULL")
        conn.execute("UPDATE refresh_failure_backoff_state SET pending_trigger = 1")
        create_refresh_failure_backoff_table(conn)  # idempotent, backfills
        create_refresh_failure_backoff_table(conn)

        row = conn.execute(
            "SELECT pending_trigger, pending_due_at, pending_marked_at "
            "FROM refresh_failure_backoff_state"
        ).fetchone()
        assert row == (1, 5.0, None), "a pre-existing trigger must become due"
    finally:
        conn.close()


def test_due_trigger_query_uses_its_index(tmp_path: Path) -> None:
    from code_indexer.server.storage.sqlite_backends._refresh_failure_backoff_mixin import (
        DUE_TRIGGERS_SQL,
        create_refresh_failure_backoff_table,
    )

    conn = _legacy_round3_table(tmp_path / "plan.db")
    try:
        create_refresh_failure_backoff_table(conn)
        plan = " ".join(
            str(row[-1])
            for row in conn.execute(f"EXPLAIN QUERY PLAN {DUE_TRIGGERS_SQL}", (1.0,))
        )
        assert "idx_refresh_failure_backoff_due" in plan, plan
    finally:
        conn.close()
