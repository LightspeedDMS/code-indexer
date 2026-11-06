"""The trigram index is an index like any other: a refresh builds it, and when it
is missing from a repository with nothing new upstream, the refresh must build it
once (a forced run), bounded like every other stale-index signal and paced so a
fleet-wide backlog drains gradually instead of in one burst.
"""

import pytest

from code_indexer.global_repos.stale_index_signal import (
    TRIGRAM_BACKFILL_ENV,
    TRIGRAM_SIGNAL,
    ForcedRunPacer,
    trigram_backfill_enabled,
    trigram_missing_signal,
)
from code_indexer.server.services.metadata_reader import IndexState


def _state(status="completed", source="metadata-voyage-ai.json"):
    return IndexState(source=source, status=status, current_commit="a" * 40)


class TestTrigramMissingSignal:
    def test_signal_when_every_provider_run_completed(self):
        signal = trigram_missing_signal([_state()], "a" * 40, "repo-global")

        assert signal is not None
        assert signal.kind == TRIGRAM_SIGNAL
        assert signal.all_completed is True
        assert "a" * 40 in signal.key  # a new commit gives a new key

    def test_no_signal_when_any_provider_run_is_incomplete(self):
        # An interrupted run is the status signal's concern, not this one.
        states = [_state(), _state(status="failed", source="metadata-cohere.json")]

        assert trigram_missing_signal(states, "a" * 40, "repo-global") is None

    def test_no_signal_without_any_recorded_index(self):
        assert trigram_missing_signal([], "a" * 40, "repo-global") is None

    def test_forced_run_is_incremental_and_always_publishes(self):
        signal = trigram_missing_signal([_state()], "a" * 40, "repo-global")

        assert signal is not None
        assert signal.needs_reconcile is False  # nothing to re-embed
        # The trigram index is not in the metadata the "nothing changed" check
        # reads, so skipping the publish would discard the build.
        assert signal.may_skip_unchanged_publish is False


class TestForcedRunPacer:
    def test_admits_up_to_the_limit_within_a_window(self):
        now = [0.0]
        pacer = ForcedRunPacer(max_runs=2, window_seconds=60.0, clock=lambda: now[0])

        assert [pacer.try_acquire() for _ in range(3)] == [True, True, False]

    def test_admits_again_once_the_window_has_passed(self):
        now = [0.0]
        pacer = ForcedRunPacer(max_runs=1, window_seconds=60.0, clock=lambda: now[0])
        assert pacer.try_acquire() is True
        assert pacer.try_acquire() is False

        now[0] = 61.0

        assert pacer.try_acquire() is True

    def test_a_refused_attempt_does_not_extend_the_window(self):
        now = [0.0]
        pacer = ForcedRunPacer(max_runs=1, window_seconds=60.0, clock=lambda: now[0])
        pacer.try_acquire()
        now[0] = 30.0
        assert pacer.try_acquire() is False

        now[0] = 61.0  # 61s after the ADMITTED run, only 31s after the refusal

        assert pacer.try_acquire() is True


class TestTrigramBackfillSwitch:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv(TRIGRAM_BACKFILL_ENV, raising=False)

        assert trigram_backfill_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "True"])
    def test_on_values(self, monkeypatch, value):
        monkeypatch.setenv(TRIGRAM_BACKFILL_ENV, value)

        assert trigram_backfill_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "", "yes"])
    def test_any_other_value_leaves_it_off(self, monkeypatch, value):
        monkeypatch.setenv(TRIGRAM_BACKFILL_ENV, value)

        assert trigram_backfill_enabled() is False
