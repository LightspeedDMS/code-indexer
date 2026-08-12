"""Tests for Bug #1565: the "temporal legacy migration: skipping '<alias>'
this pass" WARNING must not fire on EVERY tick forever.

``TemporalLegacyMigrationScheduler._migrate_one_candidate`` correctly and
by-design returns ``None`` (never raises) when the repo's write lock is
held or a refresh is currently in flight for that alias -- the migration
pass simply moves on to the next candidate, per its own docstring
("Returns None (never raises) ... the pass simply moves on"). Measured on
staging: 122 occurrences of this exact message in 24h -- almost all of
them re-reporting the SAME unresolved lock contention, not a new fact.

Per Bug #1565's requirement #3, this must log at WARNING the FIRST time a
given alias's skip is observed, DEBUG for repeat observations within the
bounded window, and WARNING again once the window elapses (a periodic
reminder, not permanent silence) -- tracked independently PER ALIAS.
Control flow (``run_once()``'s FULL result shape: nothing published, no
collisions/failures, empty ``per_repo``) must be byte-identical
throughout.

Time control uses a constructor-injectable clock attribute
(``scheduler._clock``) -- the identical pattern this same bug fix already
uses for ``FleetMigrationScheduler``'s analogous quarantine-skip cadence
throttle -- rather than patching the ``time`` module.

Real TemporalLegacyMigrationScheduler; the write-lock/refresh-in-progress
fakes reused directly from test_scheduler_1548.py's own established
harness -- no mocking of the scheduler's own skip-handling logic.
"""

from __future__ import annotations

import logging
from pathlib import Path

from code_indexer.server.services.temporal_legacy_migration.scheduler import (
    TemporalLegacyMigrationScheduler,
)
from code_indexer.server.utils.config_manager import TemporalLegacyMigrationConfig

from tests.unit.server.services.temporal_legacy_migration.test_scheduler_1548 import (
    _FakeConfigService,
    _FakeGoldenRepoManager,
    _FakeRefreshScheduler,
)

_LOGGER_NAME = "code_indexer.server.services.temporal_legacy_migration.scheduler"

# Arbitrary, deterministic starting point for the injected fake clock.
_INITIAL_FAKE_TIME_SECONDS = 1_000_000.0

# Comfortably inside the bounded reminder window.
_WITHIN_WINDOW_ADVANCE_SECONDS = 10.0

# Margin added on top of the module's own bounded-window constant to
# guarantee the window has elapsed.
_PAST_WINDOW_MARGIN_SECONDS = 1.0

# The full run_once() result shape when nothing was migrated -- every
# field asserted, not just a convenient subset.
_NOTHING_MIGRATED_RESULT = {
    "published": 0,
    "already_complete": 0,
    "deleted": 0,
    "collisions": 0,
    "failed": 0,
    "per_repo": {},
}


def _make_scheduler_with_refresh_in_progress(tmp_path: Path, *aliases: str):
    repos = {}
    for alias in aliases:
        repo = tmp_path / alias
        (repo / ".code-indexer" / "index").mkdir(parents=True)
        repos[alias] = repo
    manager = _FakeGoldenRepoManager(repos)
    settings = TemporalLegacyMigrationConfig(relocation_enabled=True)
    refresh_scheduler = _FakeRefreshScheduler(refresh_in_progress_aliases=set(aliases))
    return TemporalLegacyMigrationScheduler(
        golden_repo_manager=manager,
        config_service=_FakeConfigService(settings),
        refresh_scheduler=refresh_scheduler,
    )


def _skip_records_for(caplog, alias: str):
    return [
        r
        for r in caplog.records
        if "skipping" in r.getMessage() and alias in r.getMessage()
    ]


class TestSkipWarningBoundedCadence:
    def test_first_observation_logs_at_warning(self, tmp_path: Path, caplog) -> None:
        scheduler = _make_scheduler_with_refresh_in_progress(tmp_path, "demo")

        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            result = scheduler.run_once()

        assert result == _NOTHING_MIGRATED_RESULT
        records = _skip_records_for(caplog, "demo")
        assert records, (
            "Expected a skip log record on the first observation. All "
            f"records: {[r.getMessage() for r in caplog.records]}"
        )
        assert any(r.levelno == logging.WARNING for r in records), (
            "The FIRST observation of a skip must still log at WARNING, "
            f"but found: {[r.levelname for r in records]}"
        )

    def test_immediate_repeat_observation_is_demoted_below_warning(
        self, tmp_path: Path, caplog
    ) -> None:
        scheduler = _make_scheduler_with_refresh_in_progress(tmp_path, "demo")

        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            first_result = scheduler.run_once()
            caplog.clear()
            second_result = scheduler.run_once()

        assert first_result == _NOTHING_MIGRATED_RESULT
        assert second_result == _NOTHING_MIGRATED_RESULT, (
            "Control flow must be unchanged across repeat observations."
        )

        records = _skip_records_for(caplog, "demo")
        assert records, (
            "Expected a (demoted) skip log record on the immediate repeat "
            f"call. All records: {[r.getMessage() for r in caplog.records]}"
        )
        offending = [r for r in records if r.levelno >= logging.WARNING]
        assert not offending, (
            "Bug #1565 AC3: re-observing the SAME unresolved skip on the "
            "very next pass must be demoted BELOW WARNING, but found: "
            f"{[r.levelname for r in offending]}"
        )

    def test_warning_reappears_once_the_bounded_window_elapses(
        self, tmp_path: Path, caplog
    ) -> None:
        """Time control: ``scheduler._clock`` is a constructor-supported
        (documented on ``__init__``) swappable dependency, defaulting to
        ``time.monotonic`` -- tests override it directly for determinism,
        never by patching the ``time`` module. Mirrors
        FleetMigrationScheduler's identical Bug #1565 clock-injection
        pattern."""
        import code_indexer.server.services.temporal_legacy_migration.scheduler as sched_mod

        scheduler = _make_scheduler_with_refresh_in_progress(tmp_path, "demo")
        fake_now = [_INITIAL_FAKE_TIME_SECONDS]
        scheduler._clock = lambda: fake_now[0]

        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            primed_result = scheduler.run_once()  # primes last-logged-at
            caplog.clear()

            fake_now[0] += _WITHIN_WINDOW_ADVANCE_SECONDS
            within_result = scheduler.run_once()
            within_window_records = _skip_records_for(caplog, "demo")
            caplog.clear()

            fake_now[0] += (
                sched_mod._SKIP_WARNING_MIN_INTERVAL_SECONDS
                + _PAST_WINDOW_MARGIN_SECONDS
            )
            past_result = scheduler.run_once()
            past_window_records = _skip_records_for(caplog, "demo")

        for result in (primed_result, within_result, past_result):
            assert result == _NOTHING_MIGRATED_RESULT, (
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
            "Bug #1565 AC3: once the bounded window elapses and the skip "
            "is STILL unresolved, a fresh WARNING reminder must fire, but "
            f"found: {[r.levelname for r in past_window_records]}"
        )


class TestSkipWarningPerAliasIndependence:
    def test_two_aliases_in_the_same_pass_each_get_their_own_first_warning(
        self, tmp_path: Path, caplog
    ) -> None:
        """Discriminates a single shared last-logged timestamp (WRONG --
        the second alias processed in this SAME pass, mere microseconds
        after the first, would wrongly ride in on the first alias's very
        recent WARNING and get demoted) from genuinely PER-ALIAS tracking
        (correct: both are equally first-ever observations)."""
        scheduler = _make_scheduler_with_refresh_in_progress(
            tmp_path, "aaa-demo", "bbb-demo"
        )

        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            result = scheduler.run_once()

        assert result == _NOTHING_MIGRATED_RESULT

        for alias in ("aaa-demo", "bbb-demo"):
            records = _skip_records_for(caplog, alias)
            assert records and any(r.levelno == logging.WARNING for r in records), (
                f"{alias}'s first-ever observation in this pass must log "
                f"at WARNING independently of the other alias: "
                f"{[r.levelname for r in records]}"
            )
