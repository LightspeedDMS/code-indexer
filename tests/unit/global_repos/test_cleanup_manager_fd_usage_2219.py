"""Bug #2219: high descriptor usage must not flood the log nor disable cleanup.

Seams: ``_measure_fd_usage`` (the process-wide /proc reading, which a test
cannot push to 80% safely) and ``_clock``. Deletion itself is real (rmtree of
real temp directories) through a real QueryTracker.
"""

import logging
import os
from pathlib import Path
from typing import Any, List, Tuple

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker

SOFT_LIMIT = 1000
HIGH_USED = 900
LOW_USED = 100
LOGGER_NAME = "code_indexer.global_repos.cleanup_manager"
FD_LOG_MARKER = "File descriptor usage"


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _manager(fd_used: List[int], clock: _Clock) -> CleanupManager:
    manager = CleanupManager(query_tracker=QueryTracker(), min_retention_age_seconds=0)
    manager._measure_fd_usage = lambda: (fd_used[0], SOFT_LIMIT)  # type: ignore[method-assign]
    manager._clock = clock  # type: ignore[method-assign]
    return manager


def _schedule_dirs(manager: CleanupManager, tmp_path: Path, count: int) -> List[Path]:
    dirs = []
    for i in range(count):
        d = tmp_path / f"v_{i}"
        (d / "sub").mkdir(parents=True)
        (d / "sub" / "f.txt").write_text("x")
        manager.schedule_cleanup(str(d))
        dirs.append(d)
    return dirs


def _fd_records(caplog: pytest.LogCaptureFixture) -> List[Tuple[int, str]]:
    return [
        (r.levelno, r.getMessage())
        for r in caplog.records
        if r.name == LOGGER_NAME and FD_LOG_MARKER in r.getMessage()
    ]


class TestFdUsageLogging:
    def test_logs_on_state_changes_and_every_ten_minutes_only(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        fd_used = [HIGH_USED]
        clock = _Clock()
        manager = _manager(fd_used, clock)
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

        # one cycle per second for just under ten minutes
        for second in range(int(manager.FD_HIGH_LOG_INTERVAL_SECONDS)):
            clock.now = float(second)
            manager._process_cleanup_queue()
        assert [lvl for lvl, _ in _fd_records(caplog)] == [logging.WARNING]

        clock.now = manager.FD_HIGH_LOG_INTERVAL_SECONDS
        manager._process_cleanup_queue()
        assert [lvl for lvl, _ in _fd_records(caplog)] == [
            logging.WARNING,
            logging.WARNING,
        ]

        fd_used[0] = LOW_USED
        clock.now += 1.0
        manager._process_cleanup_queue()
        manager._process_cleanup_queue()
        records = _fd_records(caplog)
        assert [lvl for lvl, _ in records] == [
            logging.WARNING,
            logging.WARNING,
            logging.INFO,
        ]

        fd_used[0] = HIGH_USED
        clock.now += 1.0
        manager._process_cleanup_queue()
        assert len(_fd_records(caplog)) == 4
        assert f"{HIGH_USED}" in _fd_records(caplog)[-1][1]
        assert f"{SOFT_LIMIT}" in _fd_records(caplog)[-1][1]

    def test_hovering_at_the_threshold_logs_one_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        fd_used = [810]
        clock = _Clock()
        manager = _manager(fd_used, clock)
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

        for cycle in range(20):
            fd_used[0] = 810 if cycle % 2 == 0 else 790
            clock.now = float(cycle)
            manager._process_cleanup_queue()
        assert [lvl for lvl, _ in _fd_records(caplog)] == [logging.WARNING]
        assert manager.fd_usage_health_reasons() != []

        fd_used[0] = 740
        manager._process_cleanup_queue()
        assert [lvl for lvl, _ in _fd_records(caplog)] == [
            logging.WARNING,
            logging.INFO,
        ]
        assert manager.fd_usage_health_reasons() == []


class TestCleanupKeepsRunningWhileHigh:
    def test_deletes_one_path_per_cycle_while_usage_is_high(
        self, tmp_path: Path
    ) -> None:
        manager = _manager([HIGH_USED], _Clock())
        dirs = _schedule_dirs(manager, tmp_path, 3)

        manager._process_cleanup_queue()
        assert sum(1 for d in dirs if d.exists()) == 2
        assert len(manager.get_pending_cleanups()) == 2

        manager._process_cleanup_queue()
        manager._process_cleanup_queue()
        assert not any(d.exists() for d in dirs)
        assert manager.get_pending_cleanups() == set()

    def test_deletes_every_eligible_path_in_one_cycle_when_usage_is_normal(
        self, tmp_path: Path
    ) -> None:
        manager = _manager([LOW_USED], _Clock())
        dirs = _schedule_dirs(manager, tmp_path, 3)

        manager._process_cleanup_queue()

        assert not any(d.exists() for d in dirs)

    def test_not_applicable_usage_is_treated_as_normal(self, tmp_path: Path) -> None:
        manager = CleanupManager(
            query_tracker=QueryTracker(), min_retention_age_seconds=0
        )
        manager._measure_fd_usage = lambda: None  # type: ignore[method-assign]
        dirs = _schedule_dirs(manager, tmp_path, 2)

        manager._process_cleanup_queue()

        assert not any(d.exists() for d in dirs)
        assert manager.fd_usage_health_reasons() == []


class TestFdUsageHealthReasons:
    def test_reason_reported_while_high_and_cleared_after(self) -> None:
        fd_used = [HIGH_USED]
        manager = _manager(fd_used, _Clock())
        assert manager.fd_usage_health_reasons() == []

        manager._process_cleanup_queue()
        reasons = manager.fd_usage_health_reasons()
        assert len(reasons) == 1
        assert f"{HIGH_USED}" in reasons[0] and f"{SOFT_LIMIT}" in reasons[0]

        fd_used[0] = LOW_USED
        manager._process_cleanup_queue()
        assert manager.fd_usage_health_reasons() == []


FD_DIR = "/proc/self/fd"
needs_proc = pytest.mark.skipif(not os.path.isdir(FD_DIR), reason="Linux /proc only")


def _fail_fd_dir_reads(monkeypatch: pytest.MonkeyPatch) -> List[bool]:
    failing = [True]
    real_listdir = os.listdir

    def _listdir(path: Any = ".") -> List[str]:
        if failing[0] and path == FD_DIR:
            raise PermissionError(path)
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", _listdir)
    return failing


@needs_proc
class TestFdUsageUnknown:
    def test_failed_read_keeps_throttle_and_reports_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = CleanupManager(
            query_tracker=QueryTracker(), min_retention_age_seconds=0
        )
        dirs = _schedule_dirs(manager, tmp_path, 3)
        _fail_fd_dir_reads(monkeypatch)

        manager._process_cleanup_queue()

        assert sum(1 for d in dirs if d.exists()) == 2
        reasons = manager.fd_usage_health_reasons()
        assert len(reasons) == 1
        assert "unknown" in reasons[0] and "PermissionError" in reasons[0]

    def test_logs_once_per_state_change_through_unknown(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        manager = CleanupManager(query_tracker=QueryTracker())
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        manager._process_cleanup_queue()

        failing = _fail_fd_dir_reads(monkeypatch)
        for _ in range(10):
            manager._process_cleanup_queue()
        records = _fd_records(caplog)
        assert [lvl for lvl, _ in records] == [logging.WARNING]
        assert "unknown" in records[0][1]

        failing[0] = False
        manager._process_cleanup_queue()
        manager._process_cleanup_queue()
        assert [lvl for lvl, _ in _fd_records(caplog)] == [
            logging.WARNING,
            logging.INFO,
        ]
        assert manager.fd_usage_health_reasons() == []


@needs_proc
def test_real_measurement_reads_this_process() -> None:
    import resource

    manager = CleanupManager(query_tracker=QueryTracker())
    sample = manager._measure_fd_usage()

    assert sample is not None
    used, limit = sample
    assert used > 0
    assert limit == resource.getrlimit(resource.RLIMIT_NOFILE)[0]
