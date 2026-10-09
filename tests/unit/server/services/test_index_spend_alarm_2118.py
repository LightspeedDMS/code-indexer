"""Story #2118 slice 1b: refresh embedding volume alarm.

Rule (fixed constants, owner decision): alias A alarms when its 24 h sum of
``item_count`` for purposes {refresh, temporal_refresh} is STRICTLY greater
than ``max(10_000, 0.25 * published_points(A))``; NULL published points
uses the floor. The alarm is "blind" (DEGRADED, no pause target) when the
aggregate read fails, when stats recording is disabled, or when an
index/refresh/temporal_refresh row of the last hour has no alias.

Real SQLite stats store and real SQLite golden-repo metadata store; time is
injected through the alarm's clock, never by sleeping.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, List, Optional

import pytest

from code_indexer.global_repos.refresh_failure_recovery import (
    RefreshDeferredError,
    active_backoff_until,
)
from code_indexer.server.services.embedding_call_stats import (
    EmbeddingCallRecord,
    EmbeddingCallStatsSqliteBackend,
)
from code_indexer.server.services.index_spend_alarm import (
    DEFERRAL_SECONDS,
    FLOOR_ITEMS,
    IndexSpendAlarm,
)
from tests.utils.golden_repo_metadata_stores import golden_repo_metadata_store

T0 = 1_900_000_000.0
BARE = "trace-repo"
ALIAS = "trace-repo-global"
OTHER = "other-repo-global"


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def metadata(tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store("sqlite", tmp_path) as backend:
        backend.add_repo(
            alias=BARE,
            repo_url=f"local://{BARE}",
            default_branch="main",
            clone_path=f"/data/golden-repos/{BARE}",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        yield backend


@pytest.fixture
def stats(tmp_path: Path) -> EmbeddingCallStatsSqliteBackend:
    return EmbeddingCallStatsSqliteBackend(str(tmp_path / "stats.db"))


def _add(stats, purpose: str, items: int, alias: Optional[str], at: float) -> None:
    stats.insert_batch(
        [
            EmbeddingCallRecord(
                provider="voyageai",
                call_type="embed",
                model="voyage-code-3",
                item_count=items,
                token_count=items,
                batch_size=1,
                purpose=purpose,
                success=True,
                latency_ms=1,
                occurred_at=at,
                golden_repo_alias=alias,
            )
        ]
    )


def _alarm(stats, metadata, clock, enabled=None, monotonic=None) -> IndexSpendAlarm:
    return IndexSpendAlarm(
        stats_backend=stats,
        metadata_backend=metadata,
        stats_enabled=enabled if enabled is not None else (lambda: True),
        clock=clock,
        monotonic=monotonic if monotonic is not None else clock,
    )


def _alarmed(report) -> List[str]:
    return [a.alias for a in report.alarms]


# --- d. threshold edges ------------------------------------------------------


def test_exactly_at_floor_does_not_alarm_and_above_does(stats, metadata):
    clock = Clock(T0)
    _add(stats, "refresh", FLOOR_ITEMS, ALIAS, T0 - 60)
    alarm = _alarm(stats, metadata, clock)

    assert FLOOR_ITEMS == 10_000
    assert _alarmed(alarm.evaluate()) == []

    _add(stats, "refresh", 1, ALIAS, T0 - 30)
    report = alarm.evaluate()

    assert _alarmed(report) == [ALIAS]
    assert report.alarms[0].items_24h == FLOOR_ITEMS + 1
    assert report.alarms[0].threshold == FLOOR_ITEMS  # NULL points: the floor


def test_published_points_raise_the_threshold_to_a_quarter(stats, metadata):
    clock = Clock(T0)
    metadata.set_published_points(BARE, 100_000)
    _add(stats, "refresh", 25_000, ALIAS, T0 - 60)
    alarm = _alarm(stats, metadata, clock)

    assert _alarmed(alarm.evaluate()) == []  # exactly 0.25 * 100_000

    _add(stats, "refresh", 1, ALIAS, T0 - 60)

    assert _alarmed(alarm.evaluate()) == [ALIAS]


def test_small_published_size_never_lowers_the_floor(stats, metadata):
    clock = Clock(T0)
    metadata.set_published_points(BARE, 400)  # 0.25 * 400 = 100 < floor
    _add(stats, "refresh", 5_000, ALIAS, T0 - 60)

    assert _alarmed(_alarm(stats, metadata, clock).evaluate()) == []


# --- b. which purposes count -------------------------------------------------


def test_temporal_refresh_counts_but_registration_spend_does_not(stats, metadata):
    clock = Clock(T0)
    _add(stats, "index", 50_000, ALIAS, T0 - 60)
    _add(stats, "temporal", 50_000, ALIAS, T0 - 60)
    _add(stats, "refresh", 6_000, ALIAS, T0 - 60)
    alarm = _alarm(stats, metadata, clock)

    assert _alarmed(alarm.evaluate()) == []

    _add(stats, "temporal_refresh", 4_001, ALIAS, T0 - 60)
    report = alarm.evaluate()

    assert _alarmed(report) == [ALIAS]
    assert report.alarms[0].items_24h == 10_001


def test_rows_older_than_24_hours_do_not_count(stats, metadata):
    clock = Clock(T0)
    _add(stats, "refresh", 50_000, ALIAS, T0 - 86_400 - 1)

    assert _alarmed(_alarm(stats, metadata, clock).evaluate()) == []


# --- c. blind -----------------------------------------------------------------


def test_read_failure_is_blind_never_zero_spend(stats, metadata):
    clock = Clock(T0)
    Path(stats._db_path).write_bytes(b"not a valid sqlite file")

    report = _alarm(stats, metadata, clock).evaluate()

    assert report.alarms == ()
    assert report.blind_reasons, "a failed read must make the alarm blind"
    assert any("blind" in reason for reason in report.health_reasons())


def test_disabled_stats_recording_is_blind(stats, metadata):
    clock = Clock(T0)
    _add(stats, "refresh", 50_000, ALIAS, T0 - 60)

    report = _alarm(stats, metadata, clock, enabled=lambda: False).evaluate()

    assert report.blind_reasons
    assert any("disabled" in reason for reason in report.blind_reasons)


def test_unattributed_row_in_the_last_hour_is_blind(stats, metadata):
    clock = Clock(T0)
    _add(stats, "index", 3, None, T0 - 7200)  # older than an hour: fine
    alarm = _alarm(stats, metadata, clock)

    assert alarm.evaluate().blind_reasons == ()

    _add(stats, "refresh", 3, None, T0 - 60)

    assert alarm.evaluate().blind_reasons


def test_unattributed_query_row_is_not_blind(stats, metadata):
    clock = Clock(T0)
    _add(stats, "query", 3, None, T0 - 60)

    assert _alarm(stats, metadata, clock).evaluate().blind_reasons == ()


def test_health_reasons_name_alias_and_sum(stats, metadata):
    clock = Clock(T0)
    _add(stats, "refresh", 12_345, ALIAS, T0 - 60)

    reasons = _alarm(stats, metadata, clock).evaluate().health_reasons()

    assert len(reasons) == 1
    assert ALIAS in reasons[0] and "12345" in reasons[0]


# --- pause gate ----------------------------------------------------------------


def test_gate_defers_an_alarming_alias_with_a_strike_free_pending_trigger(
    stats, metadata
):
    clock = Clock(T0)
    _add(stats, "refresh", 20_000, ALIAS, T0 - 60)
    alarm = _alarm(stats, metadata, clock)

    with pytest.raises(RefreshDeferredError) as exc_info:
        alarm.defer_if_alarming(ALIAS)

    assert exc_info.value.backoff_until == T0 + DEFERRAL_SECONDS
    state = metadata.get_refresh_failure_backoff_state(ALIAS)
    assert state["pending_trigger"] is True
    assert state["pending_due_at"] == T0 + DEFERRAL_SECONDS
    assert state["consecutive_failure_count"] == 0
    assert active_backoff_until(metadata, ALIAS) is None


def test_gate_lets_a_quiet_alias_through_and_records_nothing(stats, metadata):
    clock = Clock(T0)
    _add(stats, "refresh", 20_000, OTHER, T0 - 60)  # another repo's spend
    _add(stats, "refresh", 9_000, ALIAS, T0 - 60)

    _alarm(stats, metadata, clock).defer_if_alarming(ALIAS)

    assert metadata.get_refresh_failure_backoff_state(ALIAS) is None


def test_gate_re_checks_after_the_window_passes(stats, metadata):
    clock = Clock(T0)
    _add(stats, "refresh", 20_000, ALIAS, T0 - 60)
    alarm = _alarm(stats, metadata, clock)
    with pytest.raises(RefreshDeferredError):
        alarm.defer_if_alarming(ALIAS)

    clock.now = T0 + 86_400  # the spend leaves the 24 h window

    alarm.defer_if_alarming(ALIAS)  # no longer alarming: submits


def test_blind_gate_never_pauses(stats, metadata, caplog):
    clock = Clock(T0)
    _add(stats, "refresh", 20_000, ALIAS, T0 - 60)
    Path(stats._db_path).write_bytes(b"not a valid sqlite file")
    alarm = _alarm(stats, metadata, clock)

    with caplog.at_level(logging.ERROR):
        alarm.defer_if_alarming(ALIAS)

    assert metadata.get_refresh_failure_backoff_state(ALIAS) is None
    assert any("blind" in r.getMessage() for r in caplog.records)


def test_disabled_stats_gate_never_pauses(stats, metadata):
    clock = Clock(T0)
    _add(stats, "refresh", 20_000, ALIAS, T0 - 60)

    _alarm(stats, metadata, clock, enabled=lambda: False).defer_if_alarming(ALIAS)

    assert metadata.get_refresh_failure_backoff_state(ALIAS) is None


def test_pause_error_log_is_rate_limited_per_alias_per_hour(stats, metadata, caplog):
    clock = Clock(T0)
    _add(stats, "refresh", 20_000, ALIAS, T0 - 60)
    alarm = _alarm(stats, metadata, clock)

    def pauses_logged() -> int:
        return sum(
            1
            for r in caplog.records
            if r.levelno == logging.ERROR and ALIAS in r.getMessage()
        )

    with caplog.at_level(logging.ERROR):
        for _ in range(3):
            with pytest.raises(RefreshDeferredError):
                alarm.defer_if_alarming(ALIAS)
        assert pauses_logged() == 1

        clock.now = T0 + 3_600
        _add(stats, "refresh", 20_000, ALIAS, clock.now - 60)
        with pytest.raises(RefreshDeferredError):
            alarm.defer_if_alarming(ALIAS)
        assert pauses_logged() == 2


# --- cached report --------------------------------------------------------------


def test_report_is_cached_for_sixty_seconds(stats, metadata):
    clock = Clock(T0)
    alarm = _alarm(stats, metadata, clock)

    assert _alarmed(alarm.cached_report()) == []

    _add(stats, "refresh", 20_000, ALIAS, T0 - 60)
    clock.now = T0 + 59
    assert _alarmed(alarm.cached_report()) == []  # still the cached view

    clock.now = T0 + 60
    assert _alarmed(alarm.cached_report()) == [ALIAS]


# --- batched metadata read --------------------------------------------------------


class _CountingMetadata:
    """Delegates to the real metadata store, counting published-size reads."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.reads = 0

    def get_published_points(self, alias: str) -> Optional[int]:
        self.reads += 1
        return self._real.get_published_points(alias)  # type: ignore[no-any-return]

    def get_published_points_many(self, aliases: Any) -> Any:
        self.reads += 1
        return self._real.get_published_points_many(aliases)


def test_evaluation_reads_published_sizes_once(stats, metadata):
    clock = Clock(T0)
    metadata.set_published_points(BARE, FLOOR_ITEMS * 40)  # threshold 100_000
    for alias in (ALIAS, OTHER, "third-repo-global"):
        _add(stats, "refresh", FLOOR_ITEMS * 2, alias, T0 - 60)
    counting = _CountingMetadata(metadata)

    report = _alarm(stats, counting, clock).evaluate()

    assert counting.reads == 1
    assert sorted(_alarmed(report)) == sorted([OTHER, "third-repo-global"])


# --- error paths ------------------------------------------------------------------

OVERFLOW = 3


def test_health_reasons_cap_the_alias_list():
    from code_indexer.server.services.index_spend_alarm import (
        MAX_ALIASES_IN_REASON,
        AliasSpend,
        VolumeAlarmReport,
    )

    report = VolumeAlarmReport(
        alarms=tuple(
            AliasSpend(f"r{i}-global", FLOOR_ITEMS * 2, FLOOR_ITEMS)
            for i in range(MAX_ALIASES_IN_REASON + OVERFLOW)
        )
    )

    (reason,) = report.health_reasons()
    assert f"r{MAX_ALIASES_IN_REASON - 1}-global" in reason
    assert f"r{MAX_ALIASES_IN_REASON}-global" not in reason
    assert f"+{OVERFLOW} more" in reason


class _UnreadableSizes:
    """Metadata double whose published-size read fails."""

    def get_published_points(self, alias: str) -> Optional[int]:
        raise OSError("metadata store unavailable")

    def get_published_points_many(self, aliases: List[str]) -> "dict[str, int]":
        raise OSError("metadata store unavailable")


def test_unreadable_published_size_is_blind_not_an_alarm(stats):
    clock = Clock(T0)
    _add(stats, "refresh", FLOOR_ITEMS * 5, ALIAS, T0 - 60)

    report = _alarm(stats, _UnreadableSizes(), clock).evaluate()

    assert report.alarms == ()
    assert any(ALIAS in r and "OSError" in r for r in report.blind_reasons)


SNAPSHOT_DIM = 8
SENTINEL_POINTS = 777


def _snapshot_with_points(root: Path, points: int) -> Path:
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    index_dir = root / ".code-indexer" / "index"
    store = FilesystemVectorStore(
        base_path=index_dir, project_root=root, use_chunks_db_for_new_collections=True
    )
    assert store.create_collection("voyage-code-3", SNAPSHOT_DIM)
    store.begin_indexing("voyage-code-3")
    store.upsert_points(
        "voyage-code-3",
        [
            {"id": f"p{i}", "vector": [0.1] * SNAPSHOT_DIM, "payload": {"path": "a.py"}}
            for i in range(points)
        ],
    )
    store.end_indexing("voyage-code-3")
    return root


def test_record_without_metadata_row_warns_and_writes_nothing(
    tmp_path, metadata, caplog
):
    from code_indexer.server.services.index_spend_alarm import (
        record_published_points,
    )

    snapshot = _snapshot_with_points(tmp_path / "snap", 3)
    with caplog.at_level(logging.WARNING):
        record_published_points(metadata, "unregistered-global", snapshot)

    assert metadata.get_published_points("unregistered") is None
    assert any("floor" in r.getMessage() for r in caplog.records)


def test_record_failure_is_logged_never_raised(tmp_path, metadata, caplog):
    from code_indexer.server.services.index_spend_alarm import (
        record_published_points,
    )

    metadata.set_published_points(BARE, SENTINEL_POINTS)
    with caplog.at_level(logging.ERROR):
        record_published_points(metadata, ALIAS, tmp_path / "no-such-snapshot")

    assert metadata.get_published_points(BARE) == SENTINEL_POINTS  # unchanged
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_record_with_nothing_countable_keeps_the_value(tmp_path, metadata):
    from code_indexer.server.services.index_spend_alarm import (
        record_published_points,
    )

    snapshot = tmp_path / "snap"
    (snapshot / ".code-indexer" / "index" / "tantivy_index").mkdir(parents=True)
    metadata.set_published_points(BARE, SENTINEL_POINTS)

    record_published_points(metadata, ALIAS, snapshot)

    assert metadata.get_published_points(BARE) == SENTINEL_POINTS
