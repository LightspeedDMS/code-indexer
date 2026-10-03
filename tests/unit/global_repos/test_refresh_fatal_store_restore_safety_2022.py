"""Bug #2022 Gap 4, review round 1: a restore over the source ``chunks.db``
happens ONLY when an integrity check completed and reported corruption, and
only while the refresh still owns the write lock.

Real faults throughout: a real SQLITE_IOERR (a directory where SQLite
expects its rollback journal), a real lock held by another connection, a
real write-lock release. The source store is made to differ from the
snapshot first, so any restore shows up as a changed hash.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Iterator

import pytest

from code_indexer.global_repos.refresh_failure_recovery import RefreshDeferredError
from code_indexer.global_repos.refresh_integrity_gate import (
    run_refresh_integrity_gate,
)
from code_indexer.server.services.alias_lock_store.base import (
    AliasLockOwnershipLostError,
)
from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend
from tests.utils.fatal_chunk_store_fixtures import (
    DUMMY_VOYAGE_KEY,
    corrupt_btree_pages,
    diverge_store,
    install_cidx_shim,
    make_journal_path_a_directory,
    quick_check_ok,
)
from tests.utils.golden_repo_metadata_stores import (
    STORE_KINDS,
    golden_repo_metadata_store,
)
from tests.utils.refresh_fatal_store_harness import (
    ALIAS,
    Harness,
    RecordingJobManager,
    build_harness,
    iter_chain,
    sha256_of,
    typed_kinds,
)


@pytest.fixture(params=STORE_KINDS)
def metadata(request, tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store(request.param, tmp_path) as backend:
        yield backend


@pytest.fixture
def cidx_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shim_dir = install_cidx_shim(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("VOYAGE_API_KEY", DUMMY_VOYAGE_KEY)


def _index_dir(root: Path) -> Path:
    return root / ".code-indexer" / "index"


@pytest.mark.usefixtures("cidx_on_path")
def test_io_fault_in_child_is_never_restored_over(
    tmp_path: Path, metadata: Any
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    diverge_store(harness.source_db)
    sha_before = sha256_of(harness.source_db)
    make_journal_path_a_directory(harness.source_db)

    with pytest.raises(Exception) as raised:
        harness.scheduler._execute_refresh(ALIAS)

    assert "environment" in typed_kinds(raised.value), repr(raised.value)
    assert sha256_of(harness.source_db) == sha_before, "restored over an I/O fault"
    assert harness.strikes() == 0
    jobs = RecordingJobManager()
    harness.scheduler.background_job_manager = jobs  # type: ignore[assignment]
    with pytest.raises(RefreshDeferredError):
        harness.scheduler.trigger_refresh_for_repo(ALIAS)
    assert jobs.submitted == [], "an I/O-faulted alias was not backed off"


def _gate(harness: Harness):
    assert harness.snapshot is not None
    return run_refresh_integrity_gate(
        source_index_dir=_index_dir(harness.source),
        healthy_index_dir=_index_dir(harness.snapshot),
        clone_backend=LocalCloneBackend(),
    )


def test_gate_check_raising_io_error_restores_nothing(
    tmp_path: Path, metadata: Any
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    diverge_store(harness.source_db)
    sha_before = sha256_of(harness.source_db)
    make_journal_path_a_directory(harness.source_db)

    result = _gate(harness)

    assert not result.passed
    assert sha256_of(harness.source_db) == sha_before, "restored on an I/O error"
    assert all(not f.self_heal_attempted for f in result.failures)


def test_gate_check_raising_locked_restores_nothing(
    tmp_path: Path, metadata: Any
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    diverge_store(harness.source_db)
    sha_before = sha256_of(harness.source_db)
    holder = sqlite3.connect(str(harness.source_db), timeout=0)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        result = _gate(harness)
    finally:
        holder.rollback()
        holder.close()

    assert not result.passed
    assert sha256_of(harness.source_db) == sha_before, "restored on a lock error"
    assert all(not f.self_heal_attempted for f in result.failures)


class _LoseLockAfterFirstCheck:
    """The real write-lock manager, except that a rival takes the lock right
    after the self-heal's first ownership check succeeds."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.renew_calls = 0

    def renew(self, alias: str, owner_name: str, owner_token: Any = None) -> bool:
        self.renew_calls += 1
        renewed = bool(
            self._real.renew(alias, owner_name=owner_name, owner_token=owner_token)
        )
        if self.renew_calls == 1:
            self._real.release(alias, owner_name=owner_name)
        return renewed

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


@pytest.mark.usefixtures("cidx_on_path")
def test_ownership_lost_before_restore_restores_nothing(
    tmp_path: Path, metadata: Any
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    corrupt_btree_pages(harness.source_db)
    corrupt_sha = sha256_of(harness.source_db)
    lock = _LoseLockAfterFirstCheck(harness.scheduler.write_lock_manager)
    harness.scheduler.write_lock_manager = lock  # type: ignore[assignment]

    with pytest.raises(Exception) as raised:
        harness.scheduler._execute_refresh(ALIAS)

    assert lock.renew_calls >= 2, "ownership not re-checked before the restore"
    assert any(
        isinstance(e, AliasLockOwnershipLostError) for e in iter_chain(raised.value)
    ), repr(raised.value)
    assert sha256_of(harness.source_db) == corrupt_sha
    assert not quick_check_ok(harness.source_db)


class _AllowFirstRestoreOnly:
    """A ``before_restore`` check that allows the chunks.db copy and refuses
    the next one (the sibling metadata copy), as when the write lock is
    lost between the two copies."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("write lock lost between the two restore copies")


def test_veto_at_metadata_copy_propagates_and_keeps_source_metadata(
    tmp_path: Path, metadata: Any
) -> None:
    from code_indexer.global_repos.refresh_integrity_gate import RestoreVetoedError
    from tests.utils.fatal_chunk_store_fixtures import read_metadata_marker

    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    assert harness.snapshot is not None
    corrupt_btree_pages(harness.source_db)
    veto = _AllowFirstRestoreOnly()

    with pytest.raises(RestoreVetoedError):
        run_refresh_integrity_gate(
            source_index_dir=_index_dir(harness.source),
            healthy_index_dir=_index_dir(harness.snapshot),
            clone_backend=LocalCloneBackend(),
            before_restore=veto,
        )

    assert veto.calls == 2, "metadata copy not re-checked"
    assert quick_check_ok(harness.source_db), "the allowed chunks.db copy ran"
    assert read_metadata_marker(harness.source) == "source-new", (
        "metadata restored after the veto"
    )
