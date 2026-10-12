"""Story #2118 slice 1b: golden-repo metadata store support for the refresh
volume alarm. Real stores: SQLite always, PostgreSQL when
``TEST_POSTGRES_DSN`` is set (migrated by the real MigrationRunner).

- ``published_points``: the reference size of a golden repo (points in its
  last verified published snapshot). NULL until the first publish records
  it; the alarm then uses its floor.
- ``mark_volume_alarm_deferral``: a system refresh deferred by the volume
  alarm is persisted as a pending trigger WITHOUT strikes, so the existing
  deferred-trigger loop fires it later and no failure backoff is implied.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

from code_indexer.global_repos import refresh_failure_recovery as recovery
from tests.utils.golden_repo_metadata_stores import (
    STORE_KINDS,
    golden_repo_metadata_store,
)

BARE = "repo-v"
GLOBAL = "repo-v-global"


@pytest.fixture(params=STORE_KINDS)
def metadata(request, tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store(request.param, tmp_path) as backend:
        yield backend


def _register(metadata: Any) -> None:
    metadata.add_repo(
        alias=BARE,
        repo_url=f"local://{BARE}",
        default_branch="main",
        clone_path=f"/data/golden-repos/{BARE}",
        created_at=datetime.now(timezone.utc).isoformat(),
    )


# --- published points ------------------------------------------------------


def test_published_points_are_null_until_recorded(metadata: Any) -> None:
    _register(metadata)

    assert metadata.get_published_points(BARE) is None

    assert metadata.set_published_points(BARE, 123_456) is True
    assert metadata.get_published_points(BARE) == 123_456

    assert metadata.set_published_points(BARE, 7) is True
    assert metadata.get_published_points(BARE) == 7


def test_published_points_of_unregistered_alias(metadata: Any) -> None:
    assert metadata.set_published_points("never-registered", 5) is False
    assert metadata.get_published_points("never-registered") is None


def test_published_points_leave_with_the_repo(metadata: Any) -> None:
    _register(metadata)
    metadata.set_published_points(BARE, 999)

    assert metadata.remove_repo(BARE) is True
    _register(metadata)

    assert metadata.get_published_points(BARE) is None


def test_published_points_are_read_in_one_batch(metadata: Any) -> None:
    _register(metadata)
    metadata.add_repo(
        alias="repo-w",
        repo_url="local://repo-w",
        default_branch="main",
        clone_path="/data/golden-repos/repo-w",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    metadata.set_published_points(BARE, 4_321)

    points = metadata.get_published_points_many([BARE, "repo-w", "never-registered"])

    assert points == {BARE: 4_321, "repo-w": None, "never-registered": None}
    assert metadata.get_published_points_many([]) == {}


def test_negative_published_points_are_rejected(metadata: Any) -> None:
    _register(metadata)
    with pytest.raises(ValueError):
        metadata.set_published_points(BARE, -1)


# --- volume-alarm deferral --------------------------------------------------


def test_deferral_records_a_pending_trigger_without_strikes(metadata: Any) -> None:
    due = time.time() + 3600

    metadata.mark_volume_alarm_deferral(GLOBAL, due)

    state = metadata.get_refresh_failure_backoff_state(GLOBAL)
    assert state is not None
    assert state["pending_trigger"] is True
    assert state["consecutive_failure_count"] == 0
    assert state["pending_due_at"] == pytest.approx(due)
    # No strike => no failure backoff gates later submissions.
    assert recovery.active_backoff_until(metadata, GLOBAL) is None
    # Due only when its time comes; then the deferred-trigger loop sees it.
    assert metadata.list_due_refresh_triggers(due - 1) == []
    assert [s["golden_alias"] for s in metadata.list_due_refresh_triggers(due)] == [
        GLOBAL
    ]


def test_each_deferral_advances_the_trigger_generation(metadata: Any) -> None:
    metadata.mark_volume_alarm_deferral(GLOBAL, time.time() + 10)
    first = metadata.get_refresh_failure_backoff_state(GLOBAL)["trigger_generation"]

    metadata.mark_volume_alarm_deferral(GLOBAL, time.time() + 20)
    second = metadata.get_refresh_failure_backoff_state(GLOBAL)

    assert second["trigger_generation"] > first > 0
    assert second["pending_due_at"] == pytest.approx(time.time() + 20, abs=5)


def test_deferral_keeps_existing_failure_strikes(metadata: Any) -> None:
    metadata.record_refresh_failure_backoff(GLOBAL, "disk full")
    metadata.record_refresh_failure_backoff(GLOBAL, "disk full")

    metadata.mark_volume_alarm_deferral(GLOBAL, time.time() + 3600)

    state = metadata.get_refresh_failure_backoff_state(GLOBAL)
    assert state["consecutive_failure_count"] == 2
    assert state["last_detail"] == "disk full"
    assert state["pending_trigger"] is True


def test_publish_covering_the_deferral_resolves_it(metadata: Any) -> None:
    metadata.mark_volume_alarm_deferral(GLOBAL, time.time())
    regate, covered = recovery.begin_refresh_cycle(metadata, GLOBAL)

    assert regate is True  # a pending trigger forces a publish, never a skip
    recovery.resolve_after_publish(metadata, GLOBAL, covered)

    assert metadata.get_refresh_failure_backoff_state(GLOBAL) is None
