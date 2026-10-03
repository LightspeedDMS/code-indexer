"""Bug #2022 Gap 4, review round 1 item 5: a system refresh trigger that is
deferred by the persisted failure backoff must be retried later, exactly
like a trigger that hit an already-running job -- never silently dropped
(cidx-meta would stay stale until the next unrelated write).

Real scheduler, real metadata store, real debouncer; only the job
submission boundary is recorded.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Iterator, List

import pytest

from code_indexer.global_repos import meta_description_hook
from code_indexer.global_repos.meta_description_hook import CidxMetaRefreshDebouncer
from tests.utils.golden_repo_metadata_stores import (
    STORE_KINDS,
    golden_repo_metadata_store,
)
from tests.utils.refresh_fatal_store_harness import (
    Harness,
    RecordingJobManager,
    build_harness,
    run_one_scheduler_iteration,
)

META_ALIAS = "cidx-meta-global"
ONE_DAY_SECONDS = 24 * 3600
DEBOUNCE_SECONDS = 0.05
WAIT_LIMIT_SECONDS = 5.0
POLL_SECONDS = 0.02
DEFERRED_OBSERVATION_SECONDS = 0.5


@pytest.fixture(params=STORE_KINDS)
def metadata(request, tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store(request.param, tmp_path) as backend:
        yield backend


def _wait_until(condition: Callable[[], bool]) -> bool:
    deadline = time.monotonic() + WAIT_LIMIT_SECONDS
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(POLL_SECONDS)
    return condition()


def _harness_with_backed_off_meta(
    tmp_path: Path, metadata: Any
) -> "tuple[Harness, RecordingJobManager]":
    harness = build_harness(tmp_path, metadata, snapshot_mode="none")
    meta_dir = tmp_path / "data" / "golden-repos" / "cidx-meta"
    meta_dir.mkdir()
    harness.registry.register_global_repo(
        "cidx-meta",
        META_ALIAS,
        "local://cidx-meta",
        index_path=str(meta_dir),
        allow_reserved=True,
    )
    metadata.record_refresh_failure_backoff(META_ALIAS, "disk full")
    jobs = RecordingJobManager()
    harness.scheduler.background_job_manager = jobs  # type: ignore[assignment]
    return harness, jobs


def _pass_time_beyond_the_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + ONE_DAY_SECONDS)


def _count_triggers(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """Record every real trigger_refresh_for_repo call (pass-through)."""
    real_trigger = harness.scheduler.trigger_refresh_for_repo
    calls: List[str] = []

    def _counting_trigger(alias_name: str, *args: Any, **kwargs: Any) -> Any:
        calls.append(alias_name)
        return real_trigger(alias_name, *args, **kwargs)

    monkeypatch.setattr(
        harness.scheduler, "trigger_refresh_for_repo", _counting_trigger
    )
    return calls


def test_debouncer_defers_without_spinning_and_the_scheduler_fires_it(
    tmp_path: Path, metadata: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, jobs = _harness_with_backed_off_meta(tmp_path, metadata)
    trigger_calls = _count_triggers(harness, monkeypatch)
    debouncer = CidxMetaRefreshDebouncer(
        harness.scheduler, debounce_seconds=DEBOUNCE_SECONDS
    )
    try:
        debouncer.signal_dirty()
        assert _wait_until(lambda: trigger_calls == [META_ALIAS])
        time.sleep(DEFERRED_OBSERVATION_SECONDS)  # ~10 debounce intervals
        assert trigger_calls == [META_ALIAS], "debouncer spins during the backoff"
        assert jobs.submitted == [], "submitted while backed off"
    finally:
        debouncer.shutdown()

    _pass_time_beyond_the_backoff(monkeypatch)
    run_one_scheduler_iteration(harness)

    assert jobs.submitted == [META_ALIAS], "the deferred cidx-meta refresh was dropped"


def test_writer_helper_leaves_a_deferred_trigger_to_the_scheduler(
    tmp_path: Path, metadata: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, jobs = _harness_with_backed_off_meta(tmp_path, metadata)
    trigger_calls = _count_triggers(harness, monkeypatch)
    debouncer = CidxMetaRefreshDebouncer(
        harness.scheduler, debounce_seconds=DEBOUNCE_SECONDS
    )
    monkeypatch.setattr(meta_description_hook, "_debouncer", debouncer)
    try:
        meta_description_hook.request_cidx_meta_refresh(harness.scheduler)
        time.sleep(DEFERRED_OBSERVATION_SECONDS)  # ~10 debounce intervals
        assert trigger_calls == [META_ALIAS], "deferral re-tried by the debouncer"
        assert jobs.submitted == []
    finally:
        debouncer.shutdown()

    _pass_time_beyond_the_backoff(monkeypatch)
    run_one_scheduler_iteration(harness)

    assert jobs.submitted == [META_ALIAS], "the writer's deferred refresh was dropped"
