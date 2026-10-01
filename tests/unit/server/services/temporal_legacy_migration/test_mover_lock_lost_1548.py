"""Issue #1548 round-8, Issue 1: a lost write lock must abort destructive
migration work, not merely be logged while it proceeds regardless.

Codex reproduced this as a NORMAL bug: forcing the write-lock heartbeat's
renewal to return ``False`` repeatedly still let the destructive migration
body run to completion. These tests exercise the real ``migrate_temporal_
shards`` engine (not the heartbeat itself -- see ``test_locking_1548.py``
for that) with a fake conforming to the ``LockLossCheck`` Protocol this
module's ``lock_lost_check`` parameter is typed against, proving the fix:
once the lock may have been lost, no further destructive filesystem work
proceeds, for the current shard OR any subsequent one in the same pass.
"""

import json
from pathlib import Path

import numpy as np

from code_indexer.server.services.temporal_legacy_migration.locking import (
    LockLostError,
)
from code_indexer.server.services.temporal_legacy_migration.mover import (
    migrate_temporal_shards,
)
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager


def _write_real_hnsw_index(shard_dir: Path, point_id: str, vector: list) -> None:
    manager = HNSWIndexManager(vector_dim=len(vector), space="cosine")
    manager.build_index(shard_dir, np.array([vector], dtype=np.float32), [point_id])


def _write_complete_shard(shard_dir: Path, point_id: str) -> None:
    shard_dir.mkdir(parents=True, exist_ok=True)
    (shard_dir / f"vector_{point_id}.json").write_text(
        json.dumps({"id": point_id, "vector": [1.0]})
    )
    (shard_dir / "collection_meta.json").write_text('{"name":"q1"}')
    _write_real_hnsw_index(shard_dir, point_id, [1.0])


class _FakeLockLossSignal:
    """Test-only fake implementing the ``LockLossCheck`` structural
    Protocol ``mover.py`` types ``lock_lost_check`` against: reports NOT
    lost for the first *calls_before_loss* checks, then permanently lost
    -- lets a test pin exactly WHICH step in a multi-step sequence first
    observes the loss.
    """

    def __init__(self, calls_before_loss: int) -> None:
        self._remaining = calls_before_loss
        self._lost = False

    def _check(self) -> bool:
        if not self._lost:
            if self._remaining <= 0:
                self._lost = True
            else:
                self._remaining -= 1
        return self._lost

    def is_lost(self) -> bool:
        return self._check()

    def raise_if_lost(self) -> None:
        if self._check():
            raise LockLostError("lock lost (test fake)")


def test_migrate_aborts_before_any_publish_when_lock_already_lost(tmp_path: Path):
    legacy = tmp_path / "repo" / ".code-indexer" / "index"
    fixed = tmp_path / ".temporal" / "repo"
    shard = legacy / "code-indexer-temporal-e-2026Q1"
    _write_complete_shard(shard, "p1")

    result = migrate_temporal_shards(
        legacy,
        fixed,
        relocation_enabled=True,
        cleanup_authorized=True,
        lock_lost_check=_FakeLockLossSignal(calls_before_loss=0),
    )

    assert result.published == 0
    assert result.deleted == 0
    assert result.failed == 1
    # Nothing was ever created at the fixed root, and the legacy source
    # is completely untouched -- the destructive body genuinely never ran.
    assert not (fixed / shard.name).exists()
    assert shard.exists()
    assert (shard / "vector_p1.json").exists()


def test_migrate_publishes_but_refuses_to_delete_source_once_lock_lost_mid_pass(
    tmp_path: Path,
):
    """Codex's exact scenario, precisely pinned: the lock is still
    considered held through publish (a non-destructive addition), but is
    lost by the time the DESTRUCTIVE legacy-source deletion is about to
    run. The already-published target is unaffected; the legacy source
    MUST survive, un-deleted.
    """
    legacy = tmp_path / "repo" / ".code-indexer" / "index"
    fixed = tmp_path / ".temporal" / "repo"
    shard = legacy / "code-indexer-temporal-e-2026Q1"
    _write_complete_shard(shard, "p1")

    # Check #1 (the per-shard loop's own top-of-iteration check in
    # _run_shard_pass) and check #2 (inside _publish, immediately before
    # its own destructive step) both report NOT lost -- publish proceeds.
    # Check #3 (inside _delete_source_atomically, immediately before the
    # rename-to-trash) reports lost -- deletion must abort.
    result = migrate_temporal_shards(
        legacy,
        fixed,
        relocation_enabled=True,
        cleanup_authorized=True,
        lock_lost_check=_FakeLockLossSignal(calls_before_loss=2),
    )

    # LockLostError raised from _delete_source_atomically propagates out
    # of the WHOLE _process_one_shard call, so _run_shard_pass's single
    # try/except counts this shard only as "failed" -- the SAME
    # pre-existing convention this codebase already applies to a
    # VerificationError raised in this identical spot (e.g. the round-5
    # "published shard not structurally complete" check just above the
    # delete call). The counters are not the load-bearing assertion here.
    assert result.published == 0
    assert result.deleted == 0
    assert result.failed == 1
    # The load-bearing assertions: the publish itself DID complete
    # successfully on disk (proving the abort did not roll back or skip
    # the non-destructive publish work)...
    assert (fixed / shard.name / "collection_meta.json").exists()
    # ...and the legacy source was NEVER deleted (not renamed to trash,
    # not removed) once the lock was reported lost.
    assert shard.exists()
    assert (shard / "vector_p1.json").exists()


def test_migrate_aborts_all_remaining_shards_once_lock_lost_before_first_one(
    tmp_path: Path,
):
    """With multiple shards, a lock lost before the FIRST shard is even
    attempted must abort every shard in this pass -- never process shard
    2 just because shard 1's abort was "only" a failure.
    """
    legacy = tmp_path / "repo" / ".code-indexer" / "index"
    fixed = tmp_path / ".temporal" / "repo"
    shard_a = legacy / "code-indexer-temporal-e-2026Q1"
    shard_b = legacy / "code-indexer-temporal-e-2026Q2"
    _write_complete_shard(shard_a, "p1")
    _write_complete_shard(shard_b, "p2")

    result = migrate_temporal_shards(
        legacy,
        fixed,
        relocation_enabled=True,
        cleanup_authorized=True,
        lock_lost_check=_FakeLockLossSignal(calls_before_loss=0),
    )

    assert result.published == 0
    assert result.deleted == 0
    assert result.failed == 2
    assert not (fixed / shard_a.name).exists()
    assert not (fixed / shard_b.name).exists()
    assert shard_a.exists()
    assert shard_b.exists()
