"""RefreshScheduler._execute_refresh() ownership-loss checkpoint before
publish (Issue #1546 Fix 3, Codex round review).

Codex finding: the write lock acquired at the top of
``_held_write_lock_for_publish()`` is held across indexing and the
integrity gate, but ``_execute_refresh()`` never re-verified ownership
before the destructive/publishing work (``_create_snapshot()`` +
``alias_manager.swap_alias()``) that follows. A DB connection death (or
any other ownership loss) during indexing/the gate previously went
undetected -- the code would still create and publish a snapshot as if it
still held exclusive ownership.

This test proves the checkpoint is now in place: ownership is severed
(simulated by releasing the write lock as a side effect of
``_index_source()``, mirroring what a dead DB connection's automatic
rollback would do in DB-backed mode) and the snapshot/swap must never
run.

Mocking boundary: ``_execute_refresh()`` is a pure ORCHESTRATOR -- its
own logic is "call index -> gate -> snapshot -> swap, in order, under a
lock, checking ownership between phases." The bug under test lives
entirely in that orchestration sequence, never inside
``_index_source``/``_create_snapshot``/``GitPullUpdater``, which are
independently-tested, heavy-I/O collaborators (subprocess ``cidx
index``, CoW ``cp --reflink``, real git) -- legitimate mocking-boundary
externals, not the system under test. This is the SAME seam set already
merged and passing in
``test_refresh_scheduler_index_source_first.py::TestExecuteRefreshCallSite::
test_execute_refresh_calls_index_source_then_create_snapshot`` for the
identical method. Real and unmocked here: ``RefreshScheduler``, the real
``WriteLockManager``/lock file on disk, ``GlobalRegistry``, and
``_run_and_publish_integrity_gate`` (runs for real -- naturally a clean
no-op pass against the empty ``source_repo`` fixture, per
``discover_chunks_db_collection_dirs``'s documented "missing index dir ->
[]" contract).
"""

from __future__ import annotations

import contextlib
import json
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.global_registry import GlobalRegistry
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

_ALIAS_NAME = "ownership-loss-global"
_REPO_NAME = "ownership-loss"


@pytest.fixture
def golden_repos_dir(tmp_path):
    # Path.mkdir() raises (FileExistsError/OSError) on failure rather
    # than returning a falsy value -- nothing to silently ignore here.
    d = tmp_path / "golden_repos"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def source_repo(tmp_path):
    # `.git` is a bare placeholder directory, not an initialized
    # repository: GitPullUpdater is fully mocked in every test using this
    # fixture (see _publish_seams_patched below), so no real git command
    # ever inspects it -- this mirrors the identical fixture in the
    # established precedent, test_refresh_scheduler_index_source_first.py.
    src = tmp_path / "source_repo"
    src.mkdir()
    (src / ".git").mkdir()
    return src


@pytest.fixture
def scheduler(golden_repos_dir, source_repo):
    registry = GlobalRegistry(str(golden_repos_dir))
    # register_global_repo() raises on a genuine validation failure
    # (ReservedNameError/ValueError) rather than returning a falsy
    # sentinel -- a bad alias here would fail the test loudly via that
    # exception, not silently.
    registry.register_global_repo(
        _REPO_NAME, _ALIAS_NAME, "git@github.com:org/repo.git", str(source_repo)
    )

    aliases_dir = golden_repos_dir / "aliases"
    aliases_dir.mkdir(exist_ok=True)
    # write_text() returns the character count written -- conventionally
    # unchecked throughout this codebase's own fixtures (including the
    # precedent file's identical alias-pointer write); a real write
    # failure raises OSError rather than returning a falsy count.
    (aliases_dir / f"{_ALIAS_NAME}.json").write_text(
        json.dumps({"target_path": str(source_repo)})
    )

    query_tracker = QueryTracker()
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=MagicMock(),
        query_tracker=query_tracker,
        cleanup_manager=CleanupManager(query_tracker=query_tracker),
        registry=registry,
    )


def _sever_ownership_during_index(scheduler):
    """Returns an ``_index_source`` side-effect callable that releases
    the write lock out from under the holder -- the same observable
    effect a dead DB connection's automatic transaction rollback would
    have in DB-backed mode."""

    def _side_effect(
        alias_name, source_path, progress_callback=None, force_reconcile=False, **kw
    ):
        released = scheduler.write_lock_manager.release(
            _REPO_NAME, owner_name="refresh_scheduler"
        )
        assert released is True, "test setup: failed to sever the write lock"

    return _side_effect


@contextlib.contextmanager
def _publish_seams_patched(
    scheduler, source_repo, create_snapshot_calls, swap_alias_calls
):
    """Patches the same heavy I/O seams (external collaborators) as the
    established precedent test, matching this codebase's convention for
    testing _execute_refresh()'s orchestration in isolation -- see module
    docstring."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(
                scheduler,
                "_index_source",
                side_effect=_sever_ownership_during_index(scheduler),
            )
        )

        def _record_snapshot(alias_name, source_path):
            create_snapshot_calls.append(alias_name)
            return "unused-snapshot-path"

        stack.enter_context(
            patch.object(scheduler, "_create_snapshot", side_effect=_record_snapshot)
        )
        stack.enter_context(
            patch.object(
                scheduler.alias_manager,
                "swap_alias",
                side_effect=lambda **kw: swap_alias_calls.append(kw),
            )
        )
        stack.enter_context(patch.object(scheduler.cleanup_manager, "schedule_cleanup"))
        stack.enter_context(
            patch.object(scheduler, "_detect_existing_indexes", return_value={})
        )
        stack.enter_context(
            patch.object(scheduler, "_reconcile_registry_with_filesystem")
        )
        mock_gpu = stack.enter_context(
            patch("code_indexer.global_repos.refresh_scheduler.GitPullUpdater")
        )
        mock_updater = MagicMock()
        mock_updater.has_changes.return_value = True
        mock_updater.get_source_path.return_value = str(source_repo)
        mock_gpu.return_value = mock_updater
        yield


class TestExecuteRefreshCheckpointBeforePublish:
    def test_ownership_loss_during_indexing_aborts_before_snapshot_and_swap(
        self, scheduler, source_repo
    ):
        create_snapshot_calls: list = []
        swap_alias_calls: list = []

        with _publish_seams_patched(
            scheduler, source_repo, create_snapshot_calls, swap_alias_calls
        ):
            with pytest.raises(RuntimeError, match="AliasLockOwnershipLostError"):
                scheduler._execute_refresh(_ALIAS_NAME)

        assert create_snapshot_calls == [], (
            "_create_snapshot must never run after ownership was lost mid-indexing"
        )
        assert swap_alias_calls == [], (
            "swap_alias must never run after ownership was lost mid-indexing"
        )
