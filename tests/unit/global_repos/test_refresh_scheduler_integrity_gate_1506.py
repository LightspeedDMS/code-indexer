"""
Unit tests wiring the Bug #1506 run-boundary durability-flush + integrity
gate into RefreshScheduler._execute_refresh().

Ordinary refresh writes chunks.db in place against the live master_path
with zero integrity gating before publish (_create_snapshot/swap_alias).
These tests prove:

1. A corrupt chunks.db (real corruption, same byte-flip technique as
   test_refresh_integrity_gate_1506.py) makes _execute_refresh skip
   _create_snapshot/swap_alias entirely -- the already-published alias
   keeps serving the last verified-good snapshot.
2. When a last-known-good snapshot IS available (current_target != master
   path), the corrupt master_path chunks.db is reflink-self-healed, but
   publish is STILL skipped for this cycle regardless.
3. A healthy chunks.db is unaffected -- _create_snapshot/swap_alias still
   run exactly as before (no regression).
4. Quarantine bookkeeping: a gate failure records a failure via
   golden_repo_metadata.record_refresh_integrity_failure(); a gate pass
   resets any prior quarantine state.
5. (Codex review Finding 1) The scheduler holds the SAME per-repo write
   lock external writers use across the whole index/gate/publish
   sequence via _held_write_lock_for_publish(), refusing a genuinely
   concurrent acquire attempt from another thread.
6. (Codex review Finding 2) A repo quarantined at/above the failure
   threshold skips indexing entirely instead of retrying forever.
7. (Codex review Finding 5) The legacy _create_new_index() delegator now
   runs the same integrity gate as _execute_refresh, instead of
   bypassing it.

Reuses the exact fixture/mocking pattern already established by
test_refresh_scheduler_cleanup_guard.py.
"""

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
    write_chunks_db_discriminator,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore


# ---------------------------------------------------------------------------
# Real chunks.db fixture helpers (shared technique with
# test_refresh_integrity_gate_1506.py)
# ---------------------------------------------------------------------------


def _real_records(count: int) -> list:
    records = []
    for i in range(count):
        records.append(
            {
                "id": f"point-{i:04d}",
                "vector": [float(i), float(i + 1), float(i + 2), float(i + 3)],
                "payload": {"path": f"src/file_{i}.py"},
                "chunk_text": f"def function_{i}(): pass  " + ("x" * 200),
            }
        )
    return records


def _make_chunks_db_collection(collection_dir: Path, records: list) -> Path:
    collection_dir.mkdir(parents=True, exist_ok=True)
    (collection_dir / "collection_meta.json").write_text(
        json.dumps({"name": collection_dir.name, "vector_size": 4})
    )
    chunks_db_path = collection_dir / "chunks.db"
    with ChunkStore(chunks_db_path) as store:
        store.write_batch(records)
    write_chunks_db_discriminator(collection_dir)
    assert resolve_chunk_layout(collection_dir) == ChunkLayout.CHUNKS_DB
    return chunks_db_path


def _flip_bytes_at_midpoint(path: Path, span: int = 200) -> None:
    size = path.stat().st_size
    with open(path, "r+b") as f:
        f.seek(size // 2)
        data = f.read(span)
        f.seek(size // 2)
        f.write(bytes(b ^ 0xFF for b in data))


# ---------------------------------------------------------------------------
# Fixtures (mirrors test_refresh_scheduler_cleanup_guard.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def golden_repos_dir(tmp_path):
    golden_dir = tmp_path / "golden-repos"
    golden_dir.mkdir(parents=True)
    return golden_dir


@pytest.fixture
def mock_query_tracker():
    return Mock(spec=QueryTracker)


@pytest.fixture
def mock_cleanup_manager():
    return Mock(spec=CleanupManager)


@pytest.fixture
def mock_config_source():
    config = Mock()
    config.get_global_refresh_interval.return_value = 3600
    return config


@pytest.fixture
def mock_registry():
    registry = Mock()
    registry.get_global_repo.return_value = {
        "alias_name": "my-repo-global",
        "repo_url": "git@github.com:org/my-repo.git",
    }
    registry.list_global_repos.return_value = []
    registry.update_refresh_timestamp.return_value = None
    return registry


@pytest.fixture
def mock_golden_repo_metadata():
    backend = Mock()
    backend.record_refresh_integrity_failure.return_value = 1
    backend.get_refresh_integrity_failure_state.return_value = None
    return backend


def _make_scheduler(
    golden_repos_dir,
    mock_config_source,
    mock_query_tracker,
    mock_cleanup_manager,
    mock_registry,
    mock_golden_repo_metadata,
    snapshot_manager=None,
):
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=mock_config_source,
        query_tracker=mock_query_tracker,
        cleanup_manager=mock_cleanup_manager,
        registry=mock_registry,
        snapshot_manager=snapshot_manager,
        golden_repo_metadata_backend=mock_golden_repo_metadata,
    )


@pytest.fixture
def scheduler(
    golden_repos_dir,
    mock_config_source,
    mock_query_tracker,
    mock_cleanup_manager,
    mock_registry,
    mock_golden_repo_metadata,
):
    return _make_scheduler(
        golden_repos_dir,
        mock_config_source,
        mock_query_tracker,
        mock_cleanup_manager,
        mock_registry,
        mock_golden_repo_metadata,
    )


def _run_refresh(scheduler, golden_repos_dir, current_target, new_versioned_path):
    alias_name = "my-repo-global"
    master_path = str(golden_repos_dir / "my-repo")
    (golden_repos_dir / "my-repo").mkdir(parents=True, exist_ok=True)

    scheduler.registry.get_global_repo.return_value = {
        "alias_name": alias_name,
        "repo_url": "git@github.com:org/my-repo.git",
    }

    with (
        patch.object(
            scheduler.alias_manager, "read_alias", return_value=current_target
        ),
        patch.object(scheduler.alias_manager, "swap_alias") as mock_swap_alias,
        patch.object(scheduler, "_detect_existing_indexes", return_value={}),
        patch.object(scheduler, "_reconcile_registry_with_filesystem"),
        patch.object(scheduler, "_index_source"),
        patch.object(
            scheduler, "_create_snapshot", return_value=new_versioned_path
        ) as mock_create_snapshot,
        patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
        ) as mock_git_updater_cls,
    ):
        mock_updater = Mock()
        mock_updater.has_changes.return_value = True
        mock_updater.get_source_path.return_value = master_path
        mock_git_updater_cls.return_value = mock_updater

        result = scheduler._execute_refresh(alias_name)

    return result, mock_create_snapshot, mock_swap_alias


class TestIntegrityGateFailureSkipsPublish:
    def test_corrupt_chunks_db_with_no_prior_snapshot_skips_publish(
        self, scheduler, golden_repos_dir, mock_golden_repo_metadata
    ):
        """First-ever refresh: current_target IS master_path, so there is
        no separate last-known-good snapshot to self-heal from. The gate
        must still refuse to publish."""
        master_path = golden_repos_dir / "my-repo"
        chunks_db = _make_chunks_db_collection(
            master_path / ".code-indexer" / "index" / "coll", _real_records(200)
        )
        _flip_bytes_at_midpoint(chunks_db)

        new_versioned = str(golden_repos_dir / ".versioned" / "my-repo" / "v_2000000")
        result, mock_create_snapshot, mock_swap_alias = _run_refresh(
            scheduler, golden_repos_dir, str(master_path), new_versioned
        )

        mock_create_snapshot.assert_not_called()
        mock_swap_alias.assert_not_called()
        assert result["success"] is False
        mock_golden_repo_metadata.record_refresh_integrity_failure.assert_called_once()

    def test_corrupt_chunks_db_self_heals_but_still_skips_publish_this_cycle(
        self,
        golden_repos_dir,
        mock_config_source,
        mock_query_tracker,
        mock_cleanup_manager,
        mock_registry,
        mock_golden_repo_metadata,
    ):
        """A prior real snapshot exists with healthy data -- the corrupt
        master_path chunks.db is reflink-restored from it, but this
        cycle's publish is STILL skipped (no new data was safely
        produced this cycle)."""
        from code_indexer.server.storage.shared.clone_backend import (
            LocalCloneBackend,
        )
        from code_indexer.server.storage.shared.snapshot_manager import (
            VersionedSnapshotManager,
        )

        snapshot_manager = VersionedSnapshotManager(clone_backend=LocalCloneBackend())
        scheduler = _make_scheduler(
            golden_repos_dir,
            mock_config_source,
            mock_query_tracker,
            mock_cleanup_manager,
            mock_registry,
            mock_golden_repo_metadata,
            snapshot_manager=snapshot_manager,
        )

        master_path = golden_repos_dir / "my-repo"
        master_chunks_db = _make_chunks_db_collection(
            master_path / ".code-indexer" / "index" / "coll", _real_records(200)
        )
        _flip_bytes_at_midpoint(master_chunks_db)

        prior_snapshot = golden_repos_dir / ".versioned" / "my-repo" / "v_1000000"
        _make_chunks_db_collection(
            prior_snapshot / ".code-indexer" / "index" / "coll", _real_records(200)
        )

        new_versioned = str(golden_repos_dir / ".versioned" / "my-repo" / "v_2000000")
        result, mock_create_snapshot, mock_swap_alias = _run_refresh(
            scheduler, golden_repos_dir, str(prior_snapshot), new_versioned
        )

        mock_create_snapshot.assert_not_called()
        mock_swap_alias.assert_not_called()
        assert result["success"] is False

        # master_path's chunks.db must now be genuinely readable again.
        with ChunkStore(master_chunks_db) as store:
            assert store.count() == 200


class TestIntegrityGatePassProceedsAsBefore:
    def test_healthy_chunks_db_still_publishes(
        self, scheduler, golden_repos_dir, mock_golden_repo_metadata
    ):
        master_path = golden_repos_dir / "my-repo"
        _make_chunks_db_collection(
            master_path / ".code-indexer" / "index" / "coll", _real_records(20)
        )

        new_versioned = str(golden_repos_dir / ".versioned" / "my-repo" / "v_2000000")
        result, mock_create_snapshot, mock_swap_alias = _run_refresh(
            scheduler, golden_repos_dir, str(master_path), new_versioned
        )

        mock_create_snapshot.assert_called_once()
        mock_swap_alias.assert_called_once()
        mock_golden_repo_metadata.reset_refresh_integrity_failure.assert_called_once()

    def test_no_chunks_db_collections_still_publishes(
        self, scheduler, golden_repos_dir, mock_golden_repo_metadata
    ):
        """A SHARDED_JSON-only master_path (nothing for the gate to check)
        must publish exactly as before -- no regression."""
        master_path = golden_repos_dir / "my-repo"
        legacy_coll = master_path / ".code-indexer" / "index" / "legacy_coll"
        legacy_coll.mkdir(parents=True)
        (legacy_coll / "collection_meta.json").write_text(
            json.dumps({"name": "legacy_coll", "vector_size": 4})
        )

        new_versioned = str(golden_repos_dir / ".versioned" / "my-repo" / "v_2000000")
        result, mock_create_snapshot, mock_swap_alias = _run_refresh(
            scheduler, golden_repos_dir, str(master_path), new_versioned
        )

        mock_create_snapshot.assert_called_once()
        mock_swap_alias.assert_called_once()


class TestWriteLockHeldAcrossPublishSequence:
    """Codex review Finding 1: is_write_locked() alone only CHECKS for an
    external writer before starting -- it never HOLDS the lock while
    _index_source/gate/_create_snapshot/swap_alias run, so a writer could
    start mutating chunks.db concurrently with the gate's fresh-connection
    integrity_check. RefreshScheduler._held_write_lock_for_publish(repo_name)
    is the new context manager that wraps that whole sequence in
    _execute_refresh, acquiring the SAME per-repo lock external writers
    use and releasing it on every exit path (including exceptions).

    This test exercises ONLY that context manager directly, against the
    REAL (unmocked) WriteLockManager -- no scheduler-internal method is
    mocked -- using a genuine background thread to prove a concurrent
    acquire from another thread is refused while the context is held,
    and succeeds again once it exits. This is the actual concurrency
    primitive _execute_refresh's index -> gate -> snapshot -> swap
    sequence is wrapped in; separate tests in this module already prove
    (via the established mocking convention) that a gate failure inside
    that sequence still results in a clean early return with no leaked
    resources."""

    def test_holds_lock_across_context_blocking_concurrent_acquire(
        self, scheduler, golden_repos_dir
    ):
        import threading

        ready = threading.Event()
        release_now = threading.Event()
        results: dict = {}

        def _run():
            with scheduler._held_write_lock_for_publish("my-repo") as acquired:
                results["acquired"] = acquired
                ready.set()
                assert release_now.wait(timeout=5), (
                    "test failed to signal release in time"
                )

        holder_thread = threading.Thread(target=_run)
        holder_thread.start()

        assert ready.wait(timeout=5), (
            "holder thread never entered _held_write_lock_for_publish"
        )

        # A REAL concurrent acquire from THIS thread, against the SAME
        # live WriteLockManager, WHILE the holder thread's context is
        # still active. Must be refused.
        concurrent_acquire_result = scheduler.acquire_write_lock(
            "my-repo", owner_name="external_writer"
        )

        release_now.set()
        holder_thread.join(timeout=5)
        assert not holder_thread.is_alive(), "holder thread never completed"

        assert results["acquired"] is True
        assert concurrent_acquire_result is False, (
            "A genuinely concurrent acquire attempt from another thread "
            "must be refused while _held_write_lock_for_publish's context "
            "is active."
        )

        # After the context exits, the REAL lock file must be released --
        # proven by a genuine acquire succeeding now.
        try:
            assert (
                scheduler.acquire_write_lock("my-repo", owner_name="external_writer")
                is True
            )
        finally:
            scheduler.release_write_lock("my-repo", owner_name="external_writer")

    def test_yields_false_without_acquiring_when_already_held(
        self, scheduler, golden_repos_dir
    ):
        """If another owner already holds the lock, the context manager
        must yield False (never raise, never silently proceed as if it
        held the lock) so the caller can skip this cycle gracefully --
        mirrors the existing is_write_locked() pre-check's skip
        semantics."""
        assert scheduler.acquire_write_lock("my-repo", owner_name="external_writer")
        try:
            with scheduler._held_write_lock_for_publish("my-repo") as acquired:
                assert acquired is False
        finally:
            scheduler.release_write_lock("my-repo", owner_name="external_writer")


class TestRefreshIntegrityQuarantineSkipsIndexing:
    """Codex review Finding 2: record_refresh_integrity_failure/
    reset_refresh_integrity_failure were called correctly, but nothing
    ever READ the quarantine state to make a skip decision -- a
    repeatedly-corrupting repo just kept retrying indexing forever."""

    def test_quarantined_repo_skips_publish_and_does_not_record_again(
        self, scheduler, golden_repos_dir, mock_golden_repo_metadata
    ):
        mock_golden_repo_metadata.get_refresh_integrity_failure_state.return_value = {
            "golden_alias": "my-repo-global",
            "consecutive_failure_count": 3,
            "last_detail": "prior corruption",
            "first_failed_at": "2026-01-01T00:00:00+00:00",
            "last_failed_at": "2026-01-02T00:00:00+00:00",
        }
        master_path = golden_repos_dir / "my-repo"

        new_versioned = str(golden_repos_dir / ".versioned" / "my-repo" / "v_2000000")
        result, mock_create_snapshot, mock_swap_alias = _run_refresh(
            scheduler, golden_repos_dir, str(master_path), new_versioned
        )

        mock_create_snapshot.assert_not_called()
        mock_swap_alias.assert_not_called()
        assert result["success"] is False
        assert result.get("skipped") == "integrity_quarantined"
        # A quarantine SKIP must never itself record another failure --
        # the count is already at/above threshold.
        mock_golden_repo_metadata.record_refresh_integrity_failure.assert_not_called()

    def test_non_quarantined_repo_indexes_normally(
        self, scheduler, golden_repos_dir, mock_golden_repo_metadata
    ):
        """Below-threshold failure count must NOT block indexing."""
        mock_golden_repo_metadata.get_refresh_integrity_failure_state.return_value = {
            "golden_alias": "my-repo-global",
            "consecutive_failure_count": 2,
            "last_detail": "prior corruption",
            "first_failed_at": "2026-01-01T00:00:00+00:00",
            "last_failed_at": "2026-01-02T00:00:00+00:00",
        }
        master_path = golden_repos_dir / "my-repo"

        new_versioned = str(golden_repos_dir / ".versioned" / "my-repo" / "v_2000000")
        result, mock_create_snapshot, mock_swap_alias = _run_refresh(
            scheduler, golden_repos_dir, str(master_path), new_versioned
        )

        mock_create_snapshot.assert_called_once()
        mock_swap_alias.assert_called_once()
        assert result["success"] is True


def _run_create_new_index(
    golden_repos_dir,
    mock_config_source,
    mock_query_tracker,
    mock_cleanup_manager,
    mock_registry,
    mock_golden_repo_metadata,
    alias_name,
    source_path,
):
    """Exercises the REAL, unmocked _create_new_index() -> _index_source()
    -> (gate) -> _create_snapshot() orchestration (Codex review Finding 5).
    Only genuine external-process boundaries are stubbed -- mirrors the
    EXACT convention already established and merged in
    test_refresh_scheduler_local_skip.py's
    test_create_new_index_uses_correct_timestamp: an injected MagicMock
    snapshot_manager (external collaborator, constructor-injected -- not
    a patch.object on any scheduler method) plus subprocess.run /
    run_with_popen_progress / gather_repo_metrics (real subprocess and
    progress-reporting boundaries). No scheduler-internal method
    (_index_source, _create_snapshot) is ever mocked here."""
    import shutil

    def _mock_create_snapshot(repo_name, source_path_arg):
        versioned_path = Path(golden_repos_dir) / ".versioned" / repo_name / "v_test"
        versioned_path.mkdir(parents=True, exist_ok=True)
        for item in Path(source_path_arg).iterdir():
            dest = versioned_path / item.name
            if item.is_dir():
                shutil.copytree(str(item), str(dest))
            else:
                shutil.copy2(str(item), str(dest))
        return str(versioned_path)

    mock_snapshot_manager = MagicMock()
    mock_snapshot_manager.create_snapshot.side_effect = _mock_create_snapshot

    scheduler = _make_scheduler(
        golden_repos_dir,
        mock_config_source,
        mock_query_tracker,
        mock_cleanup_manager,
        mock_registry,
        mock_golden_repo_metadata,
        snapshot_manager=mock_snapshot_manager,
    )

    raised = None
    result_path = None
    with (
        patch(
            "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
            return_value=(0, 0),
        ),
        patch(
            "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
            return_value=50,
        ),
        patch(
            "subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ),
    ):
        try:
            result_path = scheduler._create_new_index(alias_name, source_path)
        except RuntimeError as exc:
            raised = exc

    return scheduler, result_path, raised


class TestCreateNewIndexGoesThroughIntegrityGate:
    """Codex review Finding 5: the legacy _create_new_index() delegator
    (_index_source() + _create_snapshot() back-to-back) completely
    bypassed the integrity gate, publishing an unchecked database."""

    def test_corrupt_chunks_db_raises_instead_of_publishing(
        self,
        golden_repos_dir,
        mock_config_source,
        mock_query_tracker,
        mock_cleanup_manager,
        mock_registry,
        mock_golden_repo_metadata,
    ):
        alias_name = "my-repo-global"
        source_path = golden_repos_dir / "my-repo"
        chunks_db = _make_chunks_db_collection(
            source_path / ".code-indexer" / "index" / "coll", _real_records(200)
        )
        _flip_bytes_at_midpoint(chunks_db)

        scheduler, result_path, raised = _run_create_new_index(
            golden_repos_dir,
            mock_config_source,
            mock_query_tracker,
            mock_cleanup_manager,
            mock_registry,
            mock_golden_repo_metadata,
            alias_name,
            str(source_path),
        )

        assert result_path is None
        assert raised is not None
        assert "integrity gate" in str(raised)
        mock_golden_repo_metadata.record_refresh_integrity_failure.assert_called_once()

    def test_healthy_chunks_db_still_creates_snapshot(
        self,
        golden_repos_dir,
        mock_config_source,
        mock_query_tracker,
        mock_cleanup_manager,
        mock_registry,
        mock_golden_repo_metadata,
    ):
        alias_name = "my-repo-global"
        source_path = golden_repos_dir / "my-repo"
        _make_chunks_db_collection(
            source_path / ".code-indexer" / "index" / "coll", _real_records(10)
        )

        scheduler, result_path, raised = _run_create_new_index(
            golden_repos_dir,
            mock_config_source,
            mock_query_tracker,
            mock_cleanup_manager,
            mock_registry,
            mock_golden_repo_metadata,
            alias_name,
            str(source_path),
        )

        assert raised is None
        assert result_path is not None
        cloned_chunks_db = (
            Path(result_path) / ".code-indexer" / "index" / "coll" / "chunks.db"
        )
        assert cloned_chunks_db.exists()
        with ChunkStore(cloned_chunks_db) as store:
            assert store.count() == 10

    def test_create_new_index_acquires_and_releases_write_lock_for_publish(
        self,
        golden_repos_dir,
        mock_config_source,
        mock_query_tracker,
        mock_cleanup_manager,
        mock_registry,
        mock_golden_repo_metadata,
    ):
        """Bug #1506 third-pass review Item 3 (Codex NEW MEDIUM): the
        legacy _create_new_index() delegator now runs the same integrity
        gate as _execute_refresh (Finding 5), but was never wrapped in
        _held_write_lock_for_publish -- unlike _execute_refresh's publish
        sequence. Defense-in-depth: wrap _create_new_index's
        index -> gate -> snapshot sequence in the SAME write lock.

        Proven via call-order instrumentation: each spied call appends a
        marker to a shared list and then delegates to the REAL bound
        method (never replacing behavior), so the recorded order proves
        acquire happens before indexing and release happens after
        snapshot creation -- not merely that both were called once each,
        in any order."""
        import shutil

        alias_name = "my-repo-global"
        source_path = golden_repos_dir / "my-repo"
        _make_chunks_db_collection(
            source_path / ".code-indexer" / "index" / "coll", _real_records(10)
        )

        order: list = []

        mock_snapshot_manager = MagicMock()

        def _mock_create_snapshot(repo_name, source_path_arg):
            order.append("create_snapshot")
            versioned_path = (
                Path(golden_repos_dir) / ".versioned" / repo_name / "v_test"
            )
            versioned_path.mkdir(parents=True, exist_ok=True)
            for item in Path(source_path_arg).iterdir():
                dest = versioned_path / item.name
                if item.is_dir():
                    shutil.copytree(str(item), str(dest))
                else:
                    shutil.copy2(str(item), str(dest))
            return str(versioned_path)

        mock_snapshot_manager.create_snapshot.side_effect = _mock_create_snapshot

        scheduler = _make_scheduler(
            golden_repos_dir,
            mock_config_source,
            mock_query_tracker,
            mock_cleanup_manager,
            mock_registry,
            mock_golden_repo_metadata,
            snapshot_manager=mock_snapshot_manager,
        )

        # Bug #1506 4th-pass review Item 1: _held_write_lock_for_publish now
        # calls scheduler.write_lock_manager.acquire()/.release() directly
        # (bypassing the acquire_write_lock/release_write_lock wrapper) so
        # it can pass an explicit long TTL -- the spy must target the
        # actual call site, not the now-bypassed wrapper methods.
        real_acquire = scheduler.write_lock_manager.acquire
        real_release = scheduler.write_lock_manager.release
        real_index_source = scheduler._index_source

        def _acquire_spy(*args, **kwargs):
            order.append("acquire")
            return real_acquire(*args, **kwargs)

        def _release_spy(*args, **kwargs):
            order.append("release")
            return real_release(*args, **kwargs)

        def _index_source_spy(*args, **kwargs):
            order.append("index_source")
            return real_index_source(*args, **kwargs)

        with (
            patch.object(
                scheduler.write_lock_manager, "acquire", side_effect=_acquire_spy
            ),
            patch.object(
                scheduler.write_lock_manager, "release", side_effect=_release_spy
            ),
            patch.object(scheduler, "_index_source", side_effect=_index_source_spy),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(0, 0),
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                return_value=50,
            ),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout="", stderr=""),
            ),
        ):
            result_path = scheduler._create_new_index(alias_name, str(source_path))

        assert result_path is not None
        assert order == ["acquire", "index_source", "create_snapshot", "release"], (
            f"Expected write lock to be acquired before indexing and "
            f"released after snapshot creation, got order: {order}"
        )

    def test_create_new_index_releases_write_lock_even_on_integrity_gate_failure(
        self,
        golden_repos_dir,
        mock_config_source,
        mock_query_tracker,
        mock_cleanup_manager,
        mock_registry,
        mock_golden_repo_metadata,
    ):
        """The write lock acquired around _create_new_index's publish
        sequence must be released even when the integrity gate fails and
        a RuntimeError is raised -- never leaked."""
        alias_name = "my-repo-global"
        source_path = golden_repos_dir / "my-repo"
        chunks_db = _make_chunks_db_collection(
            source_path / ".code-indexer" / "index" / "coll", _real_records(200)
        )
        _flip_bytes_at_midpoint(chunks_db)

        scheduler, result_path, raised = _run_create_new_index(
            golden_repos_dir,
            mock_config_source,
            mock_query_tracker,
            mock_cleanup_manager,
            mock_registry,
            mock_golden_repo_metadata,
            alias_name,
            str(source_path),
        )

        assert raised is not None
        assert result_path is None
        # Lock must be free after the raise -- a fresh acquire succeeds.
        try:
            assert (
                scheduler.acquire_write_lock("my-repo", owner_name="external_writer")
                is True
            )
        finally:
            scheduler.release_write_lock("my-repo", owner_name="external_writer")


class TestQuarantineCheckedBeforeGitOperations:
    """Bug #1506 third-pass review Item 1 (Codex Finding 2, confirmed
    still open by independent code reading): the quarantine skip check
    previously ran AFTER GitPullUpdater construction, branch-verification
    subprocess calls (including a real `git checkout` branch reset), and
    updater.update()/has_changes() -- so a quarantined repo (which should
    be fully skipped) still performed real git-pull mutation work on the
    actual golden repo clone before being skipped. The check must now run
    at the earliest safe point in _execute_refresh -- before repo_info,
    current_target, or the updater are ever touched -- so this test needs
    no mocking of any internal scheduler method at all; only the external
    GitPullUpdater collaborator and the subprocess.run OS boundary are
    instrumented, to prove they are never invoked."""

    def test_quarantined_repo_skips_before_git_pull_updater_construction(
        self, scheduler, golden_repos_dir, mock_golden_repo_metadata
    ):
        mock_golden_repo_metadata.get_refresh_integrity_failure_state.return_value = {
            "golden_alias": "my-repo-global",
            "consecutive_failure_count": 3,
            "last_detail": "prior corruption",
            "first_failed_at": "2026-01-01T00:00:00+00:00",
            "last_failed_at": "2026-01-02T00:00:00+00:00",
        }
        alias_name = "my-repo-global"

        with (
            patch(
                "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
            ) as mock_git_updater_cls,
            patch(
                "code_indexer.global_repos.refresh_scheduler.subprocess.run"
            ) as mock_subprocess_run,
        ):
            result = scheduler._execute_refresh(alias_name)

        mock_git_updater_cls.assert_not_called()
        mock_subprocess_run.assert_not_called()
        assert result["success"] is False
        assert result.get("skipped") == "integrity_quarantined"


class TestQuarantineReadFailureFailsClosed:
    """Bug #1506 third-pass review Item 2 (Codex NEW HIGH): the quarantine
    read previously caught EVERY exception from
    get_refresh_integrity_failure_state and returned None ("not
    quarantined, proceed") on ANY read failure -- including a metadata-
    backend outage. If the backend read genuinely fails for an alias that
    IS actively quarantined, the failure made it look clean and the
    scheduler indexed it anyway -- exactly backwards for a mechanism whose
    purpose is to stop retrying a repeatedly-corrupting repo. A read
    failure must now fail this refresh cycle CLOSED instead, before
    GitPullUpdater or any indexing work is ever touched."""

    def test_metadata_backend_read_failure_skips_indexing_fails_closed(
        self, scheduler, golden_repos_dir, mock_golden_repo_metadata
    ):
        mock_golden_repo_metadata.get_refresh_integrity_failure_state.side_effect = (
            RuntimeError("metadata backend outage")
        )
        alias_name = "my-repo-global"

        with (
            patch(
                "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
            ) as mock_git_updater_cls,
        ):
            result = scheduler._execute_refresh(alias_name)

        mock_git_updater_cls.assert_not_called()
        assert result["success"] is False
        # Must be a DISTINCT skip reason from the confirmed-quarantine case
        # so operators/logs can tell "uncertain" apart from "confirmed".
        assert result.get("skipped") == "quarantine_check_failed"
        assert result.get("skipped") != "integrity_quarantined"


class TestQuarantineBookkeepingFailureLogging:
    """Bug #1506 4th-pass review Item 3 (MEDIUM): quarantine bookkeeping
    failures (record-on-failure / reset-on-pass) must never be silently
    swallowed. If persisting the failure count or clearing prior
    quarantine state itself raises, that failure must be logged at ERROR
    (not DEBUG/WARNING) with enough detail (alias, exception) to
    diagnose -- otherwise a repeatedly-failing bookkeeping write would
    make the consecutive-failure-count-based circuit breaker unreliable
    forever (the count never actually gets persisted, so the threshold is
    never reached even though the repo keeps failing the integrity gate).

    This is purely an observability fix: the CURRENT refresh cycle already
    correctly refuses to publish regardless of whether bookkeeping
    succeeds (proven by the other test classes in this file) -- only
    FUTURE cycles' quarantine-threshold tracking depends on bookkeeping
    actually landing, so these tests assert log level/content only, never
    that the bookkeeping failure changes this cycle's publish outcome.
    """

    def test_record_failure_bookkeeping_error_is_logged_at_error(
        self, scheduler, golden_repos_dir, mock_golden_repo_metadata, caplog
    ):
        """Regression lock-in: record_refresh_integrity_failure() raising
        must be logged at ERROR (already true before this review round --
        this test locks it in so a future refactor cannot silently
        downgrade it)."""
        mock_golden_repo_metadata.record_refresh_integrity_failure.side_effect = (
            RuntimeError("metadata backend write failure")
        )
        master_path = golden_repos_dir / "my-repo"
        chunks_db = _make_chunks_db_collection(
            master_path / ".code-indexer" / "index" / "coll", _real_records(200)
        )
        _flip_bytes_at_midpoint(chunks_db)

        new_versioned = str(golden_repos_dir / ".versioned" / "my-repo" / "v_2000000")
        with caplog.at_level(
            logging.ERROR, logger="code_indexer.global_repos.refresh_scheduler"
        ):
            _run_refresh(scheduler, golden_repos_dir, str(master_path), new_versioned)

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert any(
            "my-repo-global" in r.getMessage()
            and "metadata backend write failure" in r.getMessage()
            for r in error_records
        ), (
            "Swallowed record-failure bookkeeping exception must be logged "
            "at ERROR with alias + exception detail"
        )

    def test_reset_failure_bookkeeping_error_is_logged_at_error(
        self, scheduler, golden_repos_dir, mock_golden_repo_metadata, caplog
    ):
        """Prior to this fix, _reset_integrity_gate_quarantine() logged a
        swallowed reset failure at WARNING -- bumped to ERROR for
        consistency with the record-failure path above and so operators
        cannot miss it in an ERROR-only alerting setup."""
        mock_golden_repo_metadata.reset_refresh_integrity_failure.side_effect = (
            RuntimeError("metadata backend write failure")
        )
        master_path = golden_repos_dir / "my-repo"
        _make_chunks_db_collection(
            master_path / ".code-indexer" / "index" / "coll", _real_records(20)
        )

        new_versioned = str(golden_repos_dir / ".versioned" / "my-repo" / "v_2000000")
        with caplog.at_level(
            logging.ERROR, logger="code_indexer.global_repos.refresh_scheduler"
        ):
            _run_refresh(scheduler, golden_repos_dir, str(master_path), new_versioned)

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert any(
            "my-repo-global" in r.getMessage()
            and "metadata backend write failure" in r.getMessage()
            for r in error_records
        ), (
            "Swallowed reset-failure bookkeeping exception must be logged "
            "at ERROR (was WARNING) with alias + exception detail"
        )
