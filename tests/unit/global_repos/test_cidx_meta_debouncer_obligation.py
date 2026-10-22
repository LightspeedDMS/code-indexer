"""The cidx-meta debouncer keeps a write's refresh obligation until a
refresh that started after the write actually covers it (publishes, or
finds nothing to do) -- never merely because a refresh job was submitted.

Real components: the real ``RefreshScheduler`` and its real
``BackgroundJobManager`` (SQLite job store), the real catalog write lock,
the real ``cidx index`` child against the local fake VoyageAI server, real
snapshots and aliases. Timers are never waited on: the debounce interval is
long and each step runs the pending timer's callback directly.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Set, Tuple

import pytest

from code_indexer.global_repos.meta_description_hook import CidxMetaRefreshDebouncer
from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from tests.utils.fatal_chunk_store_fixtures import COLLECTION_NAME
from tests.utils.golden_repo_metadata_stores import golden_repo_metadata_store
from tests.utils.refresh_fake_provider_env import (
    fake_provider_refresh_env,
    index_repo_for_real,
)
from tests.utils.refresh_fatal_store_harness import Harness, build_harness

pytestmark = pytest.mark.timeout(300)

META_ALIAS = "cidx-meta-global"
_FAKE_KEY = "debouncer-obligation-fake-key"
DEBOUNCE_SECONDS = 3600.0  # never elapses during a test
_JOB_WAIT_SECONDS = 120.0
_POLL_SECONDS = 0.05
_TERMINAL = {"completed", "completed_partial", "failed", "cancelled", "interrupted"}


@pytest.fixture
def metadata(tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store("sqlite", tmp_path) as backend:
        yield backend


@pytest.fixture
def provider_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Tuple[Any, Dict[str, str], Path]]:
    with fake_provider_refresh_env(tmp_path, monkeypatch, _FAKE_KEY) as env:
        yield env


def _published_catalog(harness: Harness, fake: Any, env: Dict[str, str]) -> Path:
    """A real, indexed ``local://cidx-meta`` catalog published by one real
    refresh (so its snapshot carries the source manifest)."""
    meta = harness.source.parent / "cidx-meta"
    (meta / "memories").mkdir(parents=True)
    (meta / "memories" / "seed.md").write_text("# seed\n\nseed memory body\n")
    index_repo_for_real(meta, fake, env, META_ALIAS)
    harness.registry.register_global_repo(
        "cidx-meta",
        META_ALIAS,
        "local://cidx-meta",
        index_path=str(meta),
        allow_reserved=True,
    )
    harness.scheduler.alias_manager.create_alias(
        META_ALIAS, str(meta), repo_name="cidx-meta"
    )
    first = harness.scheduler._execute_refresh(META_ALIAS)
    assert first["message"] == "Refresh complete", first
    return meta


def _stored_paths(harness: Harness) -> Set[str]:
    target = harness.scheduler.alias_manager.read_alias(META_ALIAS)
    assert target is not None
    db = Path(target) / ".code-indexer" / "index" / COLLECTION_NAME / "chunks.db"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {str(row[0]) for row in conn.execute("SELECT path FROM chunks")}
    finally:
        conn.close()


def _job_manager(harness: Harness) -> BackgroundJobManager:
    db_path = harness.source.parent.parent / "cidx_server.db"
    manager = BackgroundJobManager(use_sqlite=True, db_path=str(db_path))
    harness.scheduler.background_job_manager = manager
    return manager


def _wait_for_terminal(manager: BackgroundJobManager, job_id: str) -> Dict[str, Any]:
    """The job's terminal status, once the manager also released its
    dedup slot (a submission before that is rejected as a duplicate)."""
    deadline = time.monotonic() + _JOB_WAIT_SECONDS
    while time.monotonic() < deadline:
        status = manager.get_job_status(job_id, "system", is_admin=True)
        if (
            status is not None
            and status["status"] in _TERMINAL
            and job_id not in manager._running_jobs
        ):
            return status
        time.sleep(_POLL_SECONDS)
    raise AssertionError(f"refresh job {job_id} did not finish")


def _pending_timer(debouncer: CidxMetaRefreshDebouncer) -> threading.Timer:
    with debouncer._lock:
        pending = debouncer._timer
    assert pending is not None, "the refresh is owed but no timer is pending"
    return pending


def _expire_now(debouncer: CidxMetaRefreshDebouncer) -> None:
    pending = _pending_timer(debouncer)
    pending.cancel()
    pending.function(*pending.args, **pending.kwargs)


class _RecordingTrigger:
    """Pass-through recorder of the real trigger's returned job ids."""

    def __init__(self, harness: Harness) -> None:
        self._real = harness.scheduler.trigger_refresh_for_repo
        self.job_ids: list = []

    def __call__(self, alias_name: str) -> Optional[str]:
        job_id = self._real(alias_name)
        self.job_ids.append(job_id)
        return job_id


class TestSkippedRefreshKeepsTheObligation:
    def test_write_owed_under_a_held_lock_is_indexed_after_a_skipped_refresh(
        self,
        tmp_path: Path,
        metadata: Any,
        provider_env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake, env, _ = provider_env
        harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
        meta = _published_catalog(harness, fake, env)
        manager = _job_manager(harness)
        trigger = _RecordingTrigger(harness)
        monkeypatch.setattr(harness.scheduler, "trigger_refresh_for_repo", trigger)
        debouncer = CidxMetaRefreshDebouncer(
            harness.scheduler, debounce_seconds=DEBOUNCE_SECONDS
        )
        holder = "dependency_map_service"
        try:
            assert harness.scheduler.acquire_write_lock("cidx-meta", holder)
            # A piggybacked write owed while another owner holds the lock.
            (meta / "memories" / "owed.md").write_text("# owed\n\nowed body\n")
            debouncer.signal_dirty()

            _expire_now(debouncer)
            skipped = _wait_for_terminal(manager, trigger.job_ids[0])
            assert skipped["result"]["message"] == "Skipped, write lock held"

            # The holder finishes and releases, but never triggers a refresh.
            assert harness.scheduler.release_write_lock("cidx-meta", holder)

            _expire_now(debouncer)  # the skipped job settled nothing
            for _ in range(3):  # bounded: a pacing timer, then the refresh
                if len(trigger.job_ids) > 1:
                    break
                _expire_now(debouncer)
            assert len(trigger.job_ids) == 2, trigger.job_ids
            covered = _wait_for_terminal(manager, trigger.job_ids[1])
            assert covered["result"]["message"] == "Refresh complete", covered
            assert "memories/owed.md" in _stored_paths(harness)

            _expire_now(debouncer)  # the covering refresh settles the write
            with debouncer._lock:
                assert debouncer._dirty is False
                assert debouncer._timer is None
        finally:
            debouncer.shutdown()
            manager.shutdown()
