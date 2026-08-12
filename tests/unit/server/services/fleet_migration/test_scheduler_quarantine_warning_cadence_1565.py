"""Tests for Bug #1565: the "repo 'X' is quarantined -- skipping" WARNING
must not fire on EVERY tick forever once a repo is quarantined.

Issue #1477's quarantine-skip mechanism (FleetMigrationScheduler.
_run_next_candidate) is correctly-designed and correctly-handled -- it lets
the fleet-wide queue advance past a genuinely, permanently-failing repo
rather than starving every alphabetically-later candidate. But re-reporting
the SAME unchanged fact at WARNING on every single tick (measured on
staging: 78+28 = 106 of ~2,223 WARNING entries in 24h, from just two
quarantined repos) buries genuine anomalies under a self-inflicted flood.

Per Bug #1565's requirement #3 ("use a bounded cadence -- log on state
CHANGE, or at most once per hour per alias -- rather than once per tick"),
this must log at WARNING the FIRST time a given alias is observed
quarantined, then DEBUG for repeat observations within the bounded window,
then WARNING again once the window elapses (a periodic reminder, not
silence forever) -- tracked INDEPENDENTLY PER ALIAS, not via one shared
clock that a second repo's first-ever observation could ride in on. The
skip's CONTROL FLOW (still `continue`s past the quarantined candidate, the
tick's result is unaffected) must be byte-identical throughout -- only
this one log call's severity/cadence changes.

Time control uses a constructor-injected clock attribute
(``scheduler._clock``) rather than patching the ``time`` module, so the
production dependency stays a plain, swappable callable.

Real corrupt on-disk data, real GoldenRepoMetadataSqliteBackend
persistence, real FleetMigrationScheduler -- reusing the exact harness
test_scheduler_1458.py's own TestFleetMigrationFailureQuarantine class
already established, never a mock of the scheduler's/quarantine's own
decision logic.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.fleet_migration.quarantine import (
    FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD,
)
from code_indexer.storage.id_index_manager import DuplicateSourceIdError

from tests.unit.server.services.fleet_migration.test_scheduler_1458 import (
    _FakeGoldenRepoManager,
    _RecordingConfigService,
    _build_corrupt_repo_with_duplicate_point_id,
    _make_refresh_scheduler,
    _make_scheduler,
)

_LOGGER_NAME = "code_indexer.server.services.fleet_migration.scheduler"

# Arbitrary, deterministic starting point for the injected fake clock --
# far from zero so a bug that accidentally treats an unset/zero last-logged
# timestamp as "always due" would not go unnoticed.
_INITIAL_FAKE_TIME_SECONDS = 1_000_000.0

# Comfortably inside the bounded reminder window (must stay far below
# _QUARANTINE_WARNING_MIN_INTERVAL_SECONDS).
_WITHIN_WINDOW_ADVANCE_SECONDS = 10.0

# Amount added on top of the production module's own bounded-window
# constant to guarantee the window has elapsed.
_PAST_WINDOW_MARGIN_SECONDS = 1.0


def _make_quarantine_backend(tmp_path: Path):
    from code_indexer.server.storage.sqlite_backends import (
        GoldenRepoMetadataSqliteBackend,
    )

    db_path = str(tmp_path / "golden_repo_metadata.db")
    backend = GoldenRepoMetadataSqliteBackend(db_path)
    backend.ensure_table_exists()
    return backend


def _quarantine_click(tmp_path: Path):
    """Build a single genuinely-corrupt golden repo ("click"), drive it
    past the quarantine threshold, and return the ready-to-use scheduler
    plus its backend. No second candidate exists, so every subsequent
    _run_next_candidate() call re-observes "click" quarantined and returns
    {"status": "nothing_to_migrate"}."""
    refresh_scheduler = _make_refresh_scheduler(tmp_path)
    golden_repos_dir = tmp_path / "golden-repos"
    corrupt_base = _build_corrupt_repo_with_duplicate_point_id(
        golden_repos_dir, "click"
    )
    backend = _make_quarantine_backend(tmp_path)
    golden = _FakeGoldenRepoManager({"click": corrupt_base}, sqlite_backend=backend)
    scheduler = _make_scheduler(
        tmp_path,
        golden,
        refresh_scheduler,
        background_job_manager=MagicMock(),
        config_service=_RecordingConfigService(enabled=True),
    )

    for _ in range(FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD):
        with pytest.raises(DuplicateSourceIdError):
            scheduler._run_next_candidate()

    return scheduler, backend


def _quarantine_two_repos(tmp_path: Path):
    """Build TWO independently-corrupt golden repos, "aaa-click" and
    "bbb-django" (alphabetically ordered so "aaa-click" is always attempted
    first), and drive "aaa-click" alone past the quarantine threshold.
    "bbb-django" is corrupt but NOT yet quarantined -- the caller drives it
    to quarantine separately, interleaved with re-observations of the
    already-quarantined "aaa-click"."""
    refresh_scheduler = _make_refresh_scheduler(tmp_path)
    golden_repos_dir = tmp_path / "golden-repos"
    corrupt_a = _build_corrupt_repo_with_duplicate_point_id(
        golden_repos_dir, "aaa-click"
    )
    corrupt_b = _build_corrupt_repo_with_duplicate_point_id(
        golden_repos_dir, "bbb-django"
    )
    backend = _make_quarantine_backend(tmp_path)
    golden = _FakeGoldenRepoManager(
        {"aaa-click": corrupt_a, "bbb-django": corrupt_b}, sqlite_backend=backend
    )
    scheduler = _make_scheduler(
        tmp_path,
        golden,
        refresh_scheduler,
        background_job_manager=MagicMock(),
        config_service=_RecordingConfigService(enabled=True),
    )

    for _ in range(FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD):
        with pytest.raises(DuplicateSourceIdError):
            scheduler._run_next_candidate()

    return scheduler, backend


def _quarantine_records_for(caplog, alias: str):
    return [
        r
        for r in caplog.records
        if "is quarantined" in r.getMessage() and alias in r.getMessage()
    ]


class TestQuarantineSkipWarningBoundedCadence:
    def test_first_observation_after_quarantine_logs_at_warning(
        self, tmp_path: Path, caplog
    ) -> None:
        scheduler, _backend = _quarantine_click(tmp_path)

        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            result = scheduler._run_next_candidate()

        assert result == {"status": "nothing_to_migrate"}
        records = _quarantine_records_for(caplog, "click")
        assert records, (
            "Expected a quarantine-skip log record on the first "
            f"post-quarantine observation. All records: "
            f"{[r.getMessage() for r in caplog.records]}"
        )
        assert any(r.levelno == logging.WARNING for r in records), (
            "The FIRST observation of a newly-quarantined repo must still "
            f"log at WARNING, but found: {[r.levelname for r in records]}"
        )

    def test_immediate_repeat_observation_is_demoted_below_warning(
        self, tmp_path: Path, caplog
    ) -> None:
        scheduler, _backend = _quarantine_click(tmp_path)

        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            first_result = scheduler._run_next_candidate()
            caplog.clear()
            second_result = scheduler._run_next_candidate()

        assert first_result == {"status": "nothing_to_migrate"}
        assert second_result == {"status": "nothing_to_migrate"}, (
            "Control flow must be unchanged -- the quarantined candidate "
            "is still skipped and the tick still reports "
            "'nothing_to_migrate', identically to before this fix."
        )

        records = _quarantine_records_for(caplog, "click")
        assert records, (
            "Expected a (demoted) quarantine-skip log record on the "
            f"immediate repeat call. All records: "
            f"{[r.getMessage() for r in caplog.records]}"
        )
        offending = [r for r in records if r.levelno >= logging.WARNING]
        assert not offending, (
            "Bug #1565 AC3: re-observing the SAME unchanged quarantine "
            "fact on the very next tick must be demoted BELOW WARNING "
            f"(bounded cadence), but found: {[r.levelname for r in offending]}"
        )

    def test_warning_reappears_once_the_bounded_window_elapses(
        self, tmp_path: Path, caplog
    ) -> None:
        """A quarantine that is STILL unresolved after the bounded
        interval must produce a fresh WARNING reminder -- this is a bounded
        cadence, not permanent silence after the first occurrence. Time is
        controlled via a constructor-injected clock attribute, never by
        patching the ``time`` module."""
        import code_indexer.server.services.fleet_migration.scheduler as sched_mod

        scheduler, _backend = _quarantine_click(tmp_path)

        fake_now = [_INITIAL_FAKE_TIME_SECONDS]
        scheduler._clock = lambda: fake_now[0]

        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            primed_result = scheduler._run_next_candidate()  # primes last-logged-at
            caplog.clear()

            # Still well within the bounded window -- demoted.
            fake_now[0] += _WITHIN_WINDOW_ADVANCE_SECONDS
            within_result = scheduler._run_next_candidate()
            within_window_records = _quarantine_records_for(caplog, "click")
            caplog.clear()

            # Past the bounded window -- a fresh reminder at WARNING.
            fake_now[0] += (
                sched_mod._QUARANTINE_WARNING_MIN_INTERVAL_SECONDS
                + _PAST_WINDOW_MARGIN_SECONDS
            )
            past_result = scheduler._run_next_candidate()
            past_window_records = _quarantine_records_for(caplog, "click")

        for result in (primed_result, within_result, past_result):
            assert result == {"status": "nothing_to_migrate"}, (
                "Control flow must stay unchanged across every observation "
                f"regardless of log cadence, got: {result}"
            )

        assert within_window_records and all(
            r.levelno < logging.WARNING for r in within_window_records
        ), (
            "Within the bounded window, the repeat observation must stay "
            f"below WARNING: {[r.levelname for r in within_window_records]}"
        )
        assert past_window_records and any(
            r.levelno == logging.WARNING for r in past_window_records
        ), (
            "Bug #1565 AC3: once the bounded window elapses and the "
            "quarantine is STILL unresolved, a fresh WARNING reminder "
            f"must fire, but found: {[r.levelname for r in past_window_records]}"
        )


class TestQuarantineSkipPerAliasIndependence:
    def test_two_independently_quarantined_repos_each_get_their_own_first_warning(
        self, tmp_path: Path, caplog
    ) -> None:
        """Discriminates a single shared last-logged timestamp (WRONG --
        would let "bbb-django"'s first-ever observation ride in on
        "aaa-click"'s very recent one and get wrongly demoted) from
        genuinely PER-ALIAS tracking (correct)."""
        scheduler, _backend = _quarantine_two_repos(tmp_path)
        # "aaa-click" alone is quarantined so far; "bbb-django" is corrupt
        # but has not yet reached ITS OWN failure threshold.

        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            # First re-observation of "aaa-click" post-quarantine (WARNING
            # expected) interleaved with the FIRST of several failing
            # attempts against "bbb-django" (not yet quarantined).
            with pytest.raises(DuplicateSourceIdError):
                scheduler._run_next_candidate()
            aaa_first_records = _quarantine_records_for(caplog, "aaa-click")
            assert aaa_first_records and any(
                r.levelno == logging.WARNING for r in aaa_first_records
            ), (
                "aaa-click's first post-quarantine observation must log at "
                f"WARNING: {[r.levelname for r in aaa_first_records]}"
            )
            caplog.clear()

            # Drive "bbb-django" through its remaining failures up to (but
            # not including) its own quarantine threshold. Every one of
            # these calls ALSO re-observes "aaa-click" -- which must stay
            # demoted throughout, since it is well within its own window.
            for _ in range(FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD - 1):
                with pytest.raises(DuplicateSourceIdError):
                    scheduler._run_next_candidate()
            interleaved_aaa_records = _quarantine_records_for(caplog, "aaa-click")
            assert interleaved_aaa_records and all(
                r.levelno < logging.WARNING for r in interleaved_aaa_records
            ), (
                "aaa-click's repeat observations while bbb-django is being "
                "driven to quarantine must stay demoted: "
                f"{[r.levelname for r in interleaved_aaa_records]}"
            )
            caplog.clear()

            # "bbb-django" has now reached its own quarantine threshold.
            # The NEXT call is bbb-django's OWN first-ever quarantine
            # observation -- it must log at WARNING even though
            # aaa-click's shared-clock-adjacent observations were all
            # very recently demoted.
            final_result = scheduler._run_next_candidate()

        assert final_result == {"status": "nothing_to_migrate"}
        bbb_first_records = _quarantine_records_for(caplog, "bbb-django")
        assert bbb_first_records and any(
            r.levelno == logging.WARNING for r in bbb_first_records
        ), (
            "bbb-django's OWN first-ever quarantine observation must log "
            "at WARNING independently of aaa-click's unrelated, already- "
            f"demoted cadence: {[r.levelname for r in bbb_first_records]}"
        )
        aaa_final_records = _quarantine_records_for(caplog, "aaa-click")
        assert aaa_final_records and all(
            r.levelno < logging.WARNING for r in aaa_final_records
        ), (
            "aaa-click stays demoted on this same call: "
            f"{[r.levelname for r in aaa_final_records]}"
        )


class TestGenuineAnomalySeverityUnchanged:
    """Bug #1565 requirement #4: a genuinely anomalous condition on the
    SAME method must keep its original severity -- this fix must not
    weaken any real signal."""

    def test_quarantine_backend_read_failure_still_logs_at_error(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        import code_indexer.server.services.fleet_migration.scheduler as sched_mod
        from code_indexer.server.services.fleet_migration.quarantine import (
            QuarantineStateUnavailableError,
        )

        scheduler, _backend = _quarantine_click(tmp_path)

        def _raise_unavailable(*_args, **_kwargs):
            raise QuarantineStateUnavailableError("backend read failed (simulated)")

        monkeypatch.setattr(sched_mod, "is_quarantined", _raise_unavailable)

        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            result = scheduler._run_next_candidate()

        assert result["status"] == "quarantine_state_unavailable"
        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records, (
            "A genuine backend read failure must still log at ERROR "
            "(unchanged) -- this fix demotes only the by-design "
            "unchanged-fact reminder, never a real anomaly."
        )
