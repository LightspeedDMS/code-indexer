"""A repository whose trigram index was never built is given one by the refresh.

The trigram index is built in a refresh's indexing step, which an unchanged
repository never reaches, so regex search on it full-scans forever. A missing
trigram index on an otherwise completed index is a stale-index signal: the
refresh forces ONE incremental run (nothing re-embedded), publishes it, and the
signal is bounded and paced like the others. Runs through the real
`RefreshScheduler._execute_refresh()` (see the convergence harness).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from code_indexer.global_repos.stale_index_signal import (
    MAX_FORCED_RECONCILES_PER_SIGNAL,
    TRIGRAM_BACKFILL_ENV,
    ForcedRunPacer,
)
from tests.unit.global_repos.test_refresh_forced_reconcile_convergence import Harness

@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    h = Harness(tmp_path, monkeypatch)
    h.index()  # the registration-time index
    h.embedder.embedded_texts.clear()
    return h


@pytest.fixture
def trigram(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Trigram indexes can be built here; one exists once a run has built it.

    The scheduler's trigram seams are replaced on the INSTANCE under test, not
    on a module, so the test does not depend on which module object the
    scheduler class was imported from.
    """
    state = SimpleNamespace(
        built=False,
        builds_succeed=True,
        available=True,
        clock=0.0,
        pacer=None,
    )
    monkeypatch.setenv(TRIGRAM_BACKFILL_ENV, "1")
    scheduler = harness.scheduler
    monkeypatch.setattr(
        scheduler, "_trigram_prefilter_available", lambda: state.available
    )
    monkeypatch.setattr(scheduler, "_trigram_index_present", lambda _path: state.built)
    state.pacer = ForcedRunPacer(
        max_runs=100, window_seconds=60.0, clock=lambda: state.clock
    )
    monkeypatch.setattr(scheduler, "_trigram_run_pacer", state.pacer)
    index_source = scheduler._index_source

    def index_then_build(**kwargs):
        result = index_source(**kwargs)
        if state.builds_succeed:
            state.built = True  # refresh step 1b
        return result

    monkeypatch.setattr(scheduler, "_index_source", index_then_build)
    return state


def test_unchanged_repository_without_a_trigram_index_gets_one_published(
    harness: Harness, trigram: SimpleNamespace
) -> None:
    harness.refresh()

    assert harness.index_calls == [False], "one incremental run, no reconcile"
    assert harness.embedder.embedded_texts == [], "nothing re-embedded"
    assert len(harness.snapshots) == 1, "the build is published with the index"
    assert trigram.built

    assert harness.refresh()["message"] == "No changes detected"
    assert harness.index_calls == [False], "built: the signal is gone"
    assert len(harness.snapshots) == 1


def test_repository_that_cannot_build_one_is_forced_at_most_n_times(
    harness: Harness, trigram: SimpleNamespace
) -> None:
    trigram.builds_succeed = False

    for _ in range(MAX_FORCED_RECONCILES_PER_SIGNAL + 3):
        harness.refresh()

    assert len(harness.index_calls) == MAX_FORCED_RECONCILES_PER_SIGNAL


def test_pacing_defers_runs_without_spending_or_resetting_the_bound(
    harness: Harness, trigram: SimpleNamespace
) -> None:
    trigram.builds_succeed = False
    trigram.pacer.max_runs = 1

    assert harness.refresh()["message"] != "No changes detected"  # admitted
    assert len(harness.index_calls) == 1
    assert harness.refresh()["message"] == "No changes detected"  # paced out
    assert len(harness.index_calls) == 1

    # Many windows later the bound -- not the pacer -- is what stops it, and
    # the deferred cycles did not clear the count (which would loop forever).
    for _ in range(10):
        trigram.clock += 61.0
        harness.refresh()
    assert len(harness.index_calls) == MAX_FORCED_RECONCILES_PER_SIGNAL


def test_nothing_is_forced_where_a_trigram_index_cannot_be_built(
    harness: Harness, trigram: SimpleNamespace
) -> None:
    trigram.available = False

    assert harness.refresh()["message"] == "No changes detected"
    assert harness.index_calls == []


def test_nothing_is_forced_unless_the_backfill_is_switched_on(
    harness: Harness, trigram: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(TRIGRAM_BACKFILL_ENV)

    assert harness.refresh()["message"] == "No changes detected"
    assert harness.index_calls == []
