"""Tests for Story #1397: HNSW orphan-repair sweep operating-hours window.

Covers:
  - Pure window helper `_is_within_operating_window` (in/out/overnight/
    always/half-open boundary semantics) -- thread/clock-free.
  - `HNSWOrphanRepairSweepScheduler._read_cycle_config()` fail-open defaults
    on config-read failure (enabled=True, window=0,0/always-on).
  - `_read_cycle_config()` live re-read of tick_interval_minutes and the
    operating-hours window (Web UI changes must take effect without a
    restart -- no server restart required).
  - `_loop()` end-to-end gating: submits a tick when enabled+within window,
    stays idle (no tick submitted) when the window excludes the current
    hour.

No mocking of the scheduler under test itself. The injectable `now_fn`
constructor hook (mirrors the existing `process_fn` injection pattern) is
used to control "current UTC hour" deterministically -- this is a clock
seam, not business-logic mocking.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

from code_indexer.server.services.hnsw_orphan_sweep.scheduler import (
    HNSWOrphanRepairSweepScheduler,
    _is_within_operating_window,
)


# ---------------------------------------------------------------------------
# Pure window helper -- parameterized so every in/out/overnight/always/
# half-open boundary case lives in ONE test function (avoids an oversized
# test class).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "current_hour_utc, start, end, expected, case_id",
    [
        # start == end -> always on (24x7), for the default AND a
        # non-default value.
        *[(h, 0, 0, True, f"always_on_default_hour_{h}") for h in range(24)],
        *[(h, 9, 9, True, f"always_on_nonzero_hour_{h}") for h in range(24)],
        # Same-day window 9->17.
        (9, 9, 17, True, "same_day_start_inclusive"),
        (16, 9, 17, True, "same_day_inside"),
        (8, 9, 17, False, "same_day_before_start"),
        (17, 9, 17, False, "same_day_end_exclusive"),
        (23, 9, 17, False, "same_day_after_end"),
        # Overnight wrap-around window 22->6.
        (23, 22, 6, True, "overnight_hour_23"),
        (3, 22, 6, True, "overnight_hour_3"),
        (22, 22, 6, True, "overnight_start_inclusive"),
        (12, 22, 6, False, "overnight_outside_hour_12"),
        (6, 22, 6, False, "overnight_end_exclusive"),
        # Half-open boundary semantics (confirmed off-by-one-free per issue).
        (0, 0, 1, True, "half_open_0_to_1_inside"),
        (1, 0, 1, False, "half_open_0_to_1_outside"),
        (23, 23, 0, True, "half_open_23_to_0_only_hour_23"),
        (0, 23, 0, False, "half_open_23_to_0_excludes_hour_0"),
        (22, 23, 0, False, "half_open_23_to_0_excludes_hour_22"),
        (0, 0, 23, True, "half_open_0_to_23_inside"),
        (22, 0, 23, True, "half_open_0_to_23_still_inside"),
        (23, 0, 23, False, "half_open_0_to_23_excludes_23"),
    ],
)
def test_is_within_operating_window(
    current_hour_utc: int, start: int, end: int, expected: bool, case_id: str
) -> None:
    assert _is_within_operating_window(current_hour_utc, start, end) is expected, (
        case_id
    )


# ---------------------------------------------------------------------------
# Fakes shared by _read_cycle_config / _loop tests
# ---------------------------------------------------------------------------


class _WindowConfigService:
    """Controllable, mutable fake exposing hnsw_orphan_repair_sweep_config
    attributes -- a real test double, not a Mock (feedback_faithful_db_mocks
    spirit: attribute access mirrors the real HNSWOrphanRepairSweepConfig
    dataclass)."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        batch_size: int = 5,
        tick_interval_minutes: int = 7,
        operating_hours_start_utc: int = 0,
        operating_hours_end_utc: int = 0,
        raise_on_read: bool = False,
    ) -> None:
        self.enabled = enabled
        self.batch_size = batch_size
        self.tick_interval_minutes = tick_interval_minutes
        self.operating_hours_start_utc = operating_hours_start_utc
        self.operating_hours_end_utc = operating_hours_end_utc
        self._raise_on_read = raise_on_read

    def get_config(self):
        if self._raise_on_read:
            raise RuntimeError("simulated config-read failure")
        cfg = self

        class _Wrapper:
            hnsw_orphan_repair_sweep_config = cfg

        return _Wrapper()


class _EmptyGoldenRepoManager:
    def list_golden_repos(self) -> List[Dict[str, str]]:
        return []


class _EmptyActivatedRepoManager:
    def list_all_activated_repositories(self) -> List[Dict[str, Any]]:
        return []


class _NullStateBackend:
    def get_state(self) -> Dict[str, Any]:
        return {"last_completed_key": None}

    def record_item_processed(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def complete_pass(self) -> None:
        pass


class _RecordingBackgroundJobManager:
    """Records submit_job calls; executes the tick function synchronously,
    exactly like the real per-tick job. Signals an Event when a tick is
    submitted so tests can wait deterministically without polling sleeps."""

    def __init__(self) -> None:
        self.submissions: List[str] = []
        self.triggered = threading.Event()

    def submit_job(
        self,
        operation_type: str,
        func,
        *args: Any,
        submitter_username: str,
        is_admin: bool = False,
        repo_alias: Any = None,
        **kwargs: Any,
    ) -> str:
        self.submissions.append(operation_type)
        job_id = f"job-{len(self.submissions)}"
        func(*args, **kwargs)
        self.triggered.set()
        return job_id


def _make_scheduler(
    config_service: _WindowConfigService,
    *,
    background_job_manager: Optional[_RecordingBackgroundJobManager] = None,
    now_fn=None,
) -> HNSWOrphanRepairSweepScheduler:
    kwargs: Dict[str, Any] = dict(
        golden_repo_manager=_EmptyGoldenRepoManager(),
        activated_repo_manager=_EmptyActivatedRepoManager(),
        state_backend=_NullStateBackend(),
        background_job_manager=background_job_manager,
        config_service=config_service,
    )
    if now_fn is not None:
        kwargs["now_fn"] = now_fn
    return HNSWOrphanRepairSweepScheduler(**kwargs)


# ---------------------------------------------------------------------------
# _read_cycle_config: fail-open + live re-read
# ---------------------------------------------------------------------------


class TestReadCycleConfigFailOpen:
    def test_config_read_failure_defaults_enabled_and_always_on_window(self) -> None:
        """Gotcha #2: a config-read failure must default to enabled=True and
        an always-on window (0, 0) -- never silently stop the sweep."""
        config_service = _WindowConfigService(raise_on_read=True)
        scheduler = _make_scheduler(config_service)

        cycle_cfg = scheduler._read_cycle_config()

        assert cycle_cfg["enabled"] is True
        assert cycle_cfg["window_start"] == 0
        assert cycle_cfg["window_end"] == 0
        assert cycle_cfg["interval_minutes"] == 7  # default fallback cadence


class TestReadCycleConfigLiveReread:
    def test_tick_interval_minutes_change_is_read_live(self) -> None:
        config_service = _WindowConfigService(tick_interval_minutes=7)
        scheduler = _make_scheduler(config_service)

        assert scheduler._read_cycle_config()["interval_minutes"] == 7

        config_service.tick_interval_minutes = 20
        assert scheduler._read_cycle_config()["interval_minutes"] == 20

    def test_operating_window_change_is_read_live(self) -> None:
        config_service = _WindowConfigService(
            operating_hours_start_utc=0, operating_hours_end_utc=0
        )
        scheduler = _make_scheduler(config_service)

        first = scheduler._read_cycle_config()
        assert (first["window_start"], first["window_end"]) == (0, 0)

        config_service.operating_hours_start_utc = 22
        config_service.operating_hours_end_utc = 6
        second = scheduler._read_cycle_config()
        assert (second["window_start"], second["window_end"]) == (22, 6)


# ---------------------------------------------------------------------------
# _loop() end-to-end gating (real background thread via start()/stop())
# ---------------------------------------------------------------------------


class TestLoopGatesOnOperatingWindow:
    def test_loop_submits_tick_when_enabled_and_within_window(self) -> None:
        config_service = _WindowConfigService(
            enabled=True, operating_hours_start_utc=0, operating_hours_end_utc=0
        )
        bg_manager = _RecordingBackgroundJobManager()
        scheduler = _make_scheduler(
            config_service,
            background_job_manager=bg_manager,
            now_fn=lambda: datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        )

        scheduler.start()
        try:
            assert bg_manager.triggered.wait(timeout=5), (
                "Expected a tick job to be submitted when enabled and within "
                "an always-on (0,0) window"
            )
        finally:
            scheduler.stop()

        assert bg_manager.submissions == [HNSWOrphanRepairSweepScheduler.OPERATION_TYPE]

    def test_loop_idles_when_window_excludes_current_hour(self) -> None:
        # Window is 9->17 (business hours); current hour is 3 (outside).
        config_service = _WindowConfigService(
            enabled=True, operating_hours_start_utc=9, operating_hours_end_utc=17
        )
        bg_manager = _RecordingBackgroundJobManager()
        scheduler = _make_scheduler(
            config_service,
            background_job_manager=bg_manager,
            now_fn=lambda: datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc),
        )

        scheduler.start()
        try:
            # Give the loop's first cycle a chance to run; it must NOT
            # submit a tick since the current hour is outside the window.
            time.sleep(0.2)
            assert not bg_manager.triggered.is_set(), (
                "No tick job should be submitted while the current UTC hour "
                "is outside the configured operating-hours window"
            )
            assert bg_manager.submissions == []
        finally:
            scheduler.stop()

    def test_loop_falls_back_to_always_on_when_config_read_fails(self) -> None:
        """Fail-open: even if config reads raise every cycle, the sweep must
        keep submitting ticks (never silently stop)."""
        config_service = _WindowConfigService(raise_on_read=True)
        bg_manager = _RecordingBackgroundJobManager()
        scheduler = _make_scheduler(
            config_service,
            background_job_manager=bg_manager,
            now_fn=lambda: datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        )

        scheduler.start()
        try:
            assert bg_manager.triggered.wait(timeout=5), (
                "Fail-open must still submit a tick job on config-read failure"
            )
        finally:
            scheduler.stop()
