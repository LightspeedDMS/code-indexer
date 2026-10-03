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
from typing import Any, Callable, Iterator

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
)

META_ALIAS = "cidx-meta-global"
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


def test_debouncer_retries_a_deferred_trigger(tmp_path: Path, metadata: Any) -> None:
    harness, jobs = _harness_with_backed_off_meta(tmp_path, metadata)
    debouncer = CidxMetaRefreshDebouncer(
        harness.scheduler, debounce_seconds=DEBOUNCE_SECONDS
    )
    try:
        debouncer.signal_dirty()
        time.sleep(DEFERRED_OBSERVATION_SECONDS)
        assert jobs.submitted == [], "submitted while backed off"

        metadata.reset_refresh_failure_backoff(META_ALIAS)

        assert _wait_until(lambda: jobs.submitted == [META_ALIAS]), (
            "a deferred cidx-meta refresh was dropped instead of retried"
        )
    finally:
        debouncer.shutdown()


def test_writer_helper_hands_a_deferred_trigger_to_the_debouncer(
    tmp_path: Path, metadata: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, jobs = _harness_with_backed_off_meta(tmp_path, metadata)
    debouncer = CidxMetaRefreshDebouncer(
        harness.scheduler, debounce_seconds=DEBOUNCE_SECONDS
    )
    monkeypatch.setattr(meta_description_hook, "_debouncer", debouncer)
    try:
        meta_description_hook.request_cidx_meta_refresh(harness.scheduler)
        assert jobs.submitted == []

        metadata.reset_refresh_failure_backoff(META_ALIAS)

        assert _wait_until(lambda: jobs.submitted == [META_ALIAS])
    finally:
        debouncer.shutdown()
