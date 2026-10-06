"""Forced reconciles of the refresh scheduler's stale-index self-heal
converge, are bounded, and never publish a snapshot that changes nothing.

The real `RefreshScheduler._execute_refresh()` runs against a real bare
origin, a real golden clone, the real git pull, the real stale check and the
real SQLite golden-repo metadata store. Two collaborators are replaced:
`_index_source` (which spawns `cidx index` against a live embedding
provider) runs the real `SmartIndexer` in-process with a counting embedder
and the same reconcile decision, and `_create_snapshot` (a CoW clone plus a
`cidx fix-config` subprocess) records each publish.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from code_indexer.config import Config
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.stale_index_signal import (
    MAX_FORCED_RECONCILES_PER_SIGNAL,
)
from code_indexer.server.services.metadata_reader import read_index_states
from code_indexer.server.utils.cancellable_subprocess import SubprocessCancelledError
from code_indexer.services.smart_indexer import SmartIndexer
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from tests.unit.global_repos.test_refresh_git_cancel_2012 import (
    _git,
    make_git_repo_scheduler,
    make_origin_and_clone,
)
from tests.unit.global_repos.test_refresh_scheduler_cancel_2012 import ALIAS
from tests.unit.services.test_reconcile_non_git_content_id_2013 import (
    _CountingEmbeddingProvider,
)

PROVIDER_METADATA = "metadata-counting-test-provider.json"
REPO_NAME = "example-repo"  # the write-lock name of ALIAS
WRITER = "dependency_map_service"  # an external writer of the repo


class Harness:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.origin, self.master = make_origin_and_clone(tmp_path)
        self.embedder = _CountingEmbeddingProvider()
        self.index_calls: List[bool] = []
        self.snapshots: List[str] = []
        self.fail_after_index: Optional[str] = None
        # Raised by _index_source before indexing (embedder outage, cancel).
        self.fail_before_index: Optional[BaseException] = None
        # An external writer takes the repo's write lock right after the
        # stale signal is read: after admission, before the publish lock.
        self.lock_out_after_signal = False
        self.monkeypatch = monkeypatch
        self.scheduler = self.new_scheduler()

    def new_scheduler(self) -> RefreshScheduler:
        """A fresh scheduler over the same golden repos and database (a
        server restart)."""
        scheduler, _ = make_git_repo_scheduler(self.tmp_path, self.origin, self.master)
        self.monkeypatch.setattr(scheduler, "_index_source", self._index_source)
        self.monkeypatch.setattr(scheduler, "_create_snapshot", self._create_snapshot)
        read_signal = scheduler._stale_index_signal

        def read_signal_then_lock_out(*args: Any, **kwargs: Any) -> Any:
            signal = read_signal(*args, **kwargs)
            if self.lock_out_after_signal:
                assert scheduler.acquire_write_lock(REPO_NAME, owner_name=WRITER)
            return signal

        self.monkeypatch.setattr(
            scheduler, "_stale_index_signal", read_signal_then_lock_out
        )
        return scheduler

    def _index_source(
        self, *, source_path: str, force_reconcile: bool = False, **_: Any
    ) -> None:
        states = read_index_states(source_path)
        interrupted = any(s.status in ("in_progress", "failed") for s in states)
        self.index_calls.append(force_reconcile)
        if self.fail_before_index is not None:
            raise self.fail_before_index
        self.index(reconcile=force_reconcile or interrupted)
        if self.fail_after_index is not None:
            # A run that processed every file, then failed before the
            # publish (e.g. a restart during finalize): status left behind.
            meta_path = self.master / ".code-indexer" / PROVIDER_METADATA
            meta = json.loads(meta_path.read_text())
            meta["status"] = self.fail_after_index
            meta_path.write_text(json.dumps(meta))
            raise RuntimeError("indexing failed after processing every file")

    def _create_snapshot(self, *, alias_name: str, source_path: str, **_: Any) -> str:
        path = self.tmp_path / "snapshots" / f"v_{len(self.snapshots)}"
        path.mkdir(parents=True)
        self.snapshots.append(str(path))
        return str(path)

    def index(self, reconcile: bool = False) -> None:
        config = Config(codebase_dir=self.master)
        store = FilesystemVectorStore(base_path=self.master / ".code-indexer" / "index")
        store.ensure_provider_aware_collection(config, self.embedder)
        SmartIndexer(
            config=config,
            embedding_provider=self.embedder,
            vector_store_client=store,
            metadata_path=self.master / ".code-indexer" / PROVIDER_METADATA,
        ).smart_index(
            reconcile_with_database=reconcile, quiet=True, trust_resume_state=False
        )

    def refresh(self) -> Dict[str, Any]:
        return self.scheduler._execute_refresh(ALIAS)

    def push_commit(self, name: str, content: str) -> None:
        other = self.tmp_path / f"other-{name}"
        _git("clone", str(self.origin), str(other), cwd=self.tmp_path)
        (other / name).write_text(content)
        _git("add", "-A", cwd=other)
        _git("commit", "-m", f"add {name}", cwd=other)
        _git("push", "origin", "main", cwd=other)

    def write_stale_provider(self, **fields: str) -> None:
        (self.master / ".code-indexer" / "metadata-cohere.json").write_text(
            json.dumps(fields)
        )


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    h = Harness(tmp_path, monkeypatch)
    h.index()  # the registration-time index
    assert h.embedder.embedded_texts, "the initial index embeds the source file"
    h.embedder.embedded_texts.clear()
    return h


def test_commit_touching_only_non_indexed_file_converges(harness: Harness) -> None:
    harness.push_commit("LICENSE", "Example license text\n")

    harness.refresh()  # pulls the commit and indexes (nothing eligible)
    assert harness.index_calls == [False]

    result = harness.refresh()
    assert result["message"] == "No changes detected"
    assert harness.index_calls == [False], "the drift signal must be clear"
    assert harness.embedder.embedded_texts == []


def test_empty_repository_indexes_completed_and_is_not_reforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path, monkeypatch)
    _git("rm", "-q", "main.py", cwd=h.master)
    _git("commit", "-q", "-m", "empty the repository", cwd=h.master)
    _git("push", "-q", "origin", "main", cwd=h.master)
    h.index()
    states = read_index_states(h.master)
    assert [s.status for s in states] == ["completed"]

    for _ in range(2):
        assert h.refresh()["message"] == "No changes detected"
    assert h.index_calls == []
    assert h.embedder.embedded_texts == []


def test_persistent_signal_is_forced_at_most_n_times(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    harness.write_stale_provider(status="failed", current_commit="0" * 40)

    for _ in range(MAX_FORCED_RECONCILES_PER_SIGNAL):
        harness.refresh()
    assert harness.index_calls == [True] * MAX_FORCED_RECONCILES_PER_SIGNAL
    assert len(harness.snapshots) == MAX_FORCED_RECONCILES_PER_SIGNAL, (
        "a status-signal forced reconcile always publishes"
    )

    with caplog.at_level(logging.WARNING):
        assert harness.refresh()["message"] == "No changes detected"
        harness.refresh()
    stopped = [r for r in caplog.records if "stopped forcing" in r.getMessage()]
    assert len(stopped) == 1, [r.getMessage() for r in caplog.records]
    assert stopped[0].levelno == logging.ERROR
    assert "metadata-cohere.json status=failed" in stopped[0].getMessage()

    harness.scheduler = harness.new_scheduler()  # restart
    harness.refresh()
    assert harness.index_calls == [True] * MAX_FORCED_RECONCILES_PER_SIGNAL
    assert harness.embedder.embedded_texts == []

    # A new commit changes the signal: forcing resumes for the new signal.
    harness.push_commit("NOTES", "notes\n")
    harness.refresh()  # pull + index
    harness.refresh()  # same failed provider, new HEAD -> forced again
    assert harness.index_calls[-1] is True
    assert len(harness.index_calls) == MAX_FORCED_RECONCILES_PER_SIGNAL + 2


def test_forced_reconcile_that_indexes_publishes(harness: Harness) -> None:
    meta_path = harness.master / ".code-indexer" / PROVIDER_METADATA
    meta = json.loads(meta_path.read_text())
    meta["status"] = "failed"
    meta_path.write_text(json.dumps(meta))
    (harness.master / "extra.py").write_text("def extra():\n    return 1\n")

    harness.refresh()

    assert harness.index_calls == [True]
    assert harness.embedder.embedded_texts, "the missing file is indexed"
    assert len(harness.snapshots) == 1, (
        "a forced reconcile that changed the index publishes"
    )
    assert harness.refresh()["message"] == "No changes detected"
    assert harness.index_calls == [True]


def test_status_signal_reconcile_after_failed_finalize_publishes(
    harness: Harness,
) -> None:
    """A run that processed every file of a new commit but failed before
    the publish leaves status in_progress. The next cycle's reconcile (forced
    by that status) finds nothing left to index, yet must publish: the
    commit's content was never published.

    index_calls holds the force_reconcile flag of each _index_source call; a
    cycle answering "No changes detected" never calls it (appends nothing).
    """
    harness.push_commit("feature.py", "def feature():\n    return 2\n")
    harness.fail_after_index = "in_progress"
    with pytest.raises(Exception):
        harness.refresh()  # pull + index (flag False), then the failure
    harness.fail_after_index = None
    assert harness.embedder.embedded_texts, "the failed run indexed the commit"
    assert harness.snapshots == []

    result = harness.refresh()  # status-forced reconcile (flag True)
    assert harness.index_calls == [False, True]
    assert len(harness.snapshots) == 1, (
        f"the status-signal reconcile must publish the unpublished run: {result}"
    )

    assert harness.refresh()["message"] == "No changes detected"
    assert harness.index_calls == [False, True], "one-time recovery: signal gone"
    assert len(harness.snapshots) == 1


def test_persistent_commit_drift_signal_publishes_nothing(harness: Harness) -> None:
    """Commit drift with every provider's run completed: a forced reconcile
    that changed nothing publishes nothing (the published snapshot already
    holds that content).

    index_calls grows ONLY when a cycle reaches _index_source; a capped cycle
    answers "No changes detected" without indexing. A reconcile over files
    already indexed embeds nothing, so embedded_texts stays empty."""
    harness.write_stale_provider(status="completed", current_commit="0" * 40)

    messages = [
        harness.refresh()["message"] for _ in range(MAX_FORCED_RECONCILES_PER_SIGNAL)
    ]
    assert messages == ["No changes detected"] * MAX_FORCED_RECONCILES_PER_SIGNAL
    assert harness.index_calls == [True] * MAX_FORCED_RECONCILES_PER_SIGNAL
    assert harness.snapshots == []
    assert harness.embedder.embedded_texts == []

    capped_message = harness.refresh()["message"]  # capped: no _index_source
    assert capped_message == "No changes detected"
    assert len(harness.index_calls) == MAX_FORCED_RECONCILES_PER_SIGNAL


def _run_unsuccessful_cycle(harness: Harness, outcome: str) -> None:
    """One admitted forced reconcile that never completes successfully."""
    if outcome == "write_lock_skip":
        harness.lock_out_after_signal = True
        try:
            assert harness.refresh()["message"] == "Skipped, write lock held"
        finally:
            harness.lock_out_after_signal = False
            assert harness.scheduler.release_write_lock(REPO_NAME, owner_name=WRITER)
        return
    errors = {
        "indexing_failure": RuntimeError("embedding provider unavailable"),
        "cancellation": SubprocessCancelledError("refresh job cancelled"),
    }
    harness.fail_before_index = errors[outcome]
    try:
        with pytest.raises(RuntimeError):
            harness.refresh()
    finally:
        harness.fail_before_index = None


@pytest.mark.parametrize(
    "outcome", ["indexing_failure", "write_lock_skip", "cancellation"]
)
def test_unsuccessful_forced_reconciles_do_not_count(
    harness: Harness, outcome: str
) -> None:
    """Only a forced reconcile that completed and left the same signal
    counts toward the cap; failed, skipped and cancelled ones never do."""
    harness.write_stale_provider(status="completed", current_commit="0" * 40)

    for _ in range(MAX_FORCED_RECONCILES_PER_SIGNAL + 1):
        _run_unsuccessful_cycle(harness, outcome)

    store = harness.scheduler.golden_repo_metadata
    assert store.get_forced_reconcile_state(ALIAS) is None
    calls_before = len(harness.index_calls)
    harness.refresh()
    assert harness.index_calls[calls_before:] == [True], (
        f"{outcome} attempts must not exhaust the forced-reconcile budget"
    )
