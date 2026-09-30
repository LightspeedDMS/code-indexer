"""Unit tests for Bug #1969: IDIndexManager.rebuild_from_vectors() must
self-heal a legacy SHARDED_JSON duplicate point_id collision (two
vector_*.json records sharing the same point_id but different content --
the pre-Bug-#1502 chunk-index collision shape) via the existing
repair_duplicate_and_shifted_points() metadata-only repair, mirroring the
FilesystemVectorStore.scroll_points() self-heal pattern (Bug #1579)
exactly: catch DuplicateSourceIdError, attempt repair_duplicate_and_
shifted_points() ONCE, retry the scan ONCE, and let a second
DuplicateSourceIdError (or any DedupRepairAmbiguousError from the repair
itself) propagate uncaught -- never an infinite loop, never a silently
swallowed more-specific error.

Before this fix, rebuild_from_vectors() propagated DuplicateSourceIdError
straight out of scan_vectors_for_id_map(), hard-aborting cidx index (and
any other caller, e.g. temporal_reconciliation.py's
_reconcile_shard_legacy) with "operator intervention required" -- which
does not exist on an unattended ~900-repo production deployment (this
project's "no settings, no manual steps, no babysitting" rule).

Code review round 1 (F1, P2 BLOCKING): an unconditional self-heal inside
rebuild_from_vectors() is reachable from QUERY-TIME read paths too (via
FilesystemVectorStore._load_id_index()'s corrupt-index branch, called from
search, count_points, list_files, the daemon cache, etc.) -- a plain
search/query against a legacy SHARDED_JSON collection could silently
DELETE and rewrite vector_*.json files and rebuild the whole HNSW index
from inside a query thread, with no rollout gate, no query drain, and
worst of all could target an immutable ``.versioned/`` snapshot (this
project's "NEVER modify/checkout/index inside .versioned/" absolute
invariant). Fix: self-heal is now OPT-IN via an explicit ``self_heal``
keyword (default False, preserving the original hard-fail-immediately
behavior for every caller that does not explicitly ask for it), AND even
when self_heal=True is passed, an immutable versioned snapshot is NEVER
mutated -- the original DuplicateSourceIdError propagates instead
(defense-in-depth, since indexing/reconcile writers should never target a
versioned snapshot in the first place, but this project's invariant is
absolute).
"""

import hashlib
import json
import logging
from pathlib import Path

import pytest

from code_indexer.storage.id_index_manager import DuplicateSourceIdError, IDIndexManager
from code_indexer.storage.shared.collection_dedup_repair import (
    DedupRepairAmbiguousError,
)
import code_indexer.storage.shared.collection_dedup_repair as repair_mod


def _point_id(project_id: str, file_hash: str, index: int) -> str:
    return hashlib.md5(f"{project_id}_{file_hash}_{index}".encode()).hexdigest()


def _write_repairable_duplicate_record(
    collection_dir: Path,
    *,
    project_id: str,
    file_hash: str,
    index: int,
    vector: list,
    line_start: int,
    line_end: int,
    shard_suffix: str,
) -> Path:
    """Write one legacy sharded vector_<id>.json record using the REAL
    production identity scheme (unique_key = f"{project_id}_{file_hash}_
    {index}", point_id = md5(unique_key)) -- self-consistent with
    collection_dedup_repair.py's whole-collection identity gate, so a
    duplicate built this way IS repairable. Two calls with the SAME
    (project_id, file_hash, index) collide on point_id exactly like the
    confirmed real bug (same label, different content, Bug #1502)."""
    unique_key = f"{project_id}_{file_hash}_{index}"
    point_id = _point_id(project_id, file_hash, index)
    payload = {
        "path": "src/foo.py",
        "content": f"chunk content {index}{shard_suffix}",
        "language": "python",
        "project_id": project_id,
        "file_hash": file_hash,
        "chunk_index": index,
        "total_chunks": 1,
        "line_start": line_start,
        "line_end": line_end,
        "point_id": point_id,
        "unique_key": unique_key,
    }
    record = {"id": point_id, "vector": vector, "payload": payload}
    shard_dir = collection_dir / point_id[:2] / (point_id[2:4] + shard_suffix)
    shard_dir.mkdir(parents=True, exist_ok=True)
    file_path = shard_dir / f"vector_{point_id}.json"
    file_path.write_text(json.dumps(record))
    return file_path


def _write_collection_meta(
    collection_dir: Path, *, vector_dim: int = 4, space: str = "cosine"
) -> None:
    """collection_meta.json's hnsw_index.vector_dim/.space is the sole
    authoritative source repair_duplicate_and_shifted_points uses for its
    HNSW rebuild -- required whenever a winner-kept (not gate-rejected)
    repair actually runs."""
    meta = {
        "name": "coll",
        "vector_size": vector_dim,
        "hnsw_index": {
            "version": 1,
            "vector_dim": vector_dim,
            "space": space,
            "vector_count": 0,
            "id_mapping": {},
        },
    }
    (collection_dir / "collection_meta.json").write_text(json.dumps(meta))


def _make_counting_repair(monkeypatch):
    """Wrap repair_mod.repair_duplicate_and_shifted_points with a call
    counter, returning the shared mutable counter dict."""
    call_count = {"n": 0}
    original = repair_mod.repair_duplicate_and_shifted_points

    def _counting_repair(*args, **kwargs):
        call_count["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        repair_mod, "repair_duplicate_and_shifted_points", _counting_repair
    )
    return call_count


class TestRebuildFromVectorsSelfHealsWhenExplicitlyRequested:
    """The primary fix: a repairable duplicate (proper unique_key scheme,
    id_index.bin naming a resolvable winner) self-heals when the caller
    explicitly opts in via self_heal=True -- the write-path contract."""

    def test_self_heals_instead_of_raising(self, tmp_path: Path, caplog) -> None:
        _write_collection_meta(tmp_path)
        winner_path = _write_repairable_duplicate_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:ccc",
            index=7,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=100,
            line_end=110,
            shard_suffix="-a",
        )
        loser_path = _write_repairable_duplicate_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:ccc",
            index=7,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=100,
            line_end=110,
            shard_suffix="-b",
        )
        shared_point_id = _point_id("proj", "sha256:ccc", 7)
        # Pre-existing (possibly stale) id_index.bin naming the winner --
        # exactly the shape repair_duplicate_and_shifted_points resolves
        # via its id_index.bin winner-selection mechanism.
        IDIndexManager().save_index(tmp_path, {shared_point_id: winner_path})

        manager = IDIndexManager()
        with caplog.at_level(logging.WARNING):
            result = manager.rebuild_from_vectors(tmp_path, self_heal=True)

        # Self-healed: the winner survives, RENAMED to its canonical
        # (post-renumber) id -- assert the actual repair outcome (F4:
        # asserting id_index.bin merely "exists" proves nothing, since the
        # test itself wrote it before the call).
        assert len(result) == 1
        surviving_point_id, surviving_path = next(iter(result.items()))
        assert surviving_path.resolve() == winner_path.resolve()
        assert not loser_path.exists()
        remaining_vector_files = list(tmp_path.rglob("vector_*.json"))
        assert len(remaining_vector_files) == 1
        assert "Bug #1969" in caplog.text

    def test_second_rebuild_after_self_heal_is_a_clean_no_op(
        self, tmp_path: Path
    ) -> None:
        """After the first self-heal, a second rebuild_from_vectors() call
        must find a clean (non-duplicated) tree and complete without
        needing to repair again."""
        _write_collection_meta(tmp_path)
        winner_path = _write_repairable_duplicate_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:ccc2",
            index=3,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
            shard_suffix="-a",
        )
        _write_repairable_duplicate_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:ccc2",
            index=3,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=1,
            line_end=10,
            shard_suffix="-b",
        )
        shared_point_id = _point_id("proj", "sha256:ccc2", 3)
        IDIndexManager().save_index(tmp_path, {shared_point_id: winner_path})

        manager = IDIndexManager()
        manager.rebuild_from_vectors(tmp_path, self_heal=True)
        # Second call must not raise and must not need to repair again.
        result2 = manager.rebuild_from_vectors(tmp_path, self_heal=True)
        assert len(result2) == 1


class TestRebuildFromVectorsSelfHealDefaultsOff:
    """F1 (P2 BLOCKING) fix: self_heal defaults to False. A caller that
    does not explicitly opt in (every existing read-path caller of
    _load_id_index(), e.g. search, count_points, list_files, the daemon
    cache) must get the ORIGINAL hard-fail-immediately behavior -- zero
    mutation, repair never even attempted -- exactly as if this fix did
    not exist. This is what makes it safe to leave self-heal reachable
    from the shared rebuild_from_vectors() entry point at all."""

    def test_repairable_duplicate_is_not_touched_without_self_heal(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _write_collection_meta(tmp_path)
        winner_path = _write_repairable_duplicate_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:readpath",
            index=0,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
            shard_suffix="-a",
        )
        loser_path = _write_repairable_duplicate_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:readpath",
            index=0,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=1,
            line_end=10,
            shard_suffix="-b",
        )
        call_count = _make_counting_repair(monkeypatch)

        manager = IDIndexManager()
        with pytest.raises(DuplicateSourceIdError):
            manager.rebuild_from_vectors(tmp_path)  # self_heal omitted -> False

        assert call_count["n"] == 0, (
            "repair_duplicate_and_shifted_points must NEVER be invoked "
            "when self_heal is not explicitly requested -- a read-path "
            "caller must never trigger mutation"
        )
        # Zero mutation: both records untouched, no id_index.bin written.
        assert winner_path.exists()
        assert loser_path.exists()
        assert not (tmp_path / IDIndexManager.INDEX_FILENAME).exists()

    def test_repairable_duplicate_is_not_touched_with_self_heal_false_explicit(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _write_collection_meta(tmp_path)
        _write_repairable_duplicate_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:explicitfalse",
            index=0,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
            shard_suffix="-a",
        )
        _write_repairable_duplicate_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:explicitfalse",
            index=0,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=1,
            line_end=10,
            shard_suffix="-b",
        )
        call_count = _make_counting_repair(monkeypatch)

        manager = IDIndexManager()
        with pytest.raises(DuplicateSourceIdError):
            manager.rebuild_from_vectors(tmp_path, self_heal=False)

        assert call_count["n"] == 0


class TestRebuildFromVectorsNeverSelfHealsVersionedSnapshot:
    """F1 defense-in-depth: even when a caller explicitly passes
    self_heal=True, an immutable ``.versioned/`` snapshot path is NEVER
    mutated -- this project's CLAUDE.md marks "NEVER modify/checkout/index
    inside .versioned/" as an absolute invariant, and repair_duplicate_
    and_shifted_points() itself has no such guard (it will happily mutate
    whatever directory it is given). The canonical predicate is
    is_immutable_versioned_snapshot() (server/services/query_path_cache.py),
    which recognizes a path at OR INSIDE a canonical
    .versioned/{ns}/v_<ts>/ snapshot root -- exactly the shape a real
    collection_path (nested several levels under the snapshot root) has."""

    def test_versioned_snapshot_duplicate_raises_without_any_mutation(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # Canonical shape: {root}/.versioned/{namespace}/v_<ts>/... -- the
        # collection directory lives several levels below the snapshot
        # leaf, matching a real golden-repo query path.
        snapshot_collection_dir = (
            tmp_path
            / ".versioned"
            / "my-golden-repo"
            / "v_1700000000"
            / ".code-indexer"
            / "index"
            / "voyage-code-3"
        )
        snapshot_collection_dir.mkdir(parents=True)
        _write_collection_meta(snapshot_collection_dir)
        winner_path = _write_repairable_duplicate_record(
            snapshot_collection_dir,
            project_id="proj",
            file_hash="sha256:versioned",
            index=0,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
            shard_suffix="-a",
        )
        loser_path = _write_repairable_duplicate_record(
            snapshot_collection_dir,
            project_id="proj",
            file_hash="sha256:versioned",
            index=0,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=1,
            line_end=10,
            shard_suffix="-b",
        )
        call_count = _make_counting_repair(monkeypatch)

        manager = IDIndexManager()
        with pytest.raises(DuplicateSourceIdError):
            # Even though self_heal=True is explicitly requested (as a
            # genuine write-path caller would), a versioned snapshot must
            # still never be mutated.
            manager.rebuild_from_vectors(snapshot_collection_dir, self_heal=True)

        assert call_count["n"] == 0, (
            "repair_duplicate_and_shifted_points must NEVER be invoked "
            "against a path at or inside an immutable .versioned/ "
            "snapshot, even when self_heal=True is explicitly requested"
        )
        assert winner_path.exists()
        assert loser_path.exists()
        assert not (snapshot_collection_dir / IDIndexManager.INDEX_FILENAME).exists()


class TestRebuildFromVectorsBoundedRetryNeverLoops:
    """When repair_duplicate_and_shifted_points() cannot actually resolve
    the duplicate (whole-collection identity gate rejects it -- e.g. no
    unique_key present, a foreign identity scheme), the retry's second
    DuplicateSourceIdError must propagate -- the self-heal is attempted
    EXACTLY ONCE, never looped."""

    def test_second_duplicate_error_propagates_repair_called_once(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # No unique_key at all -- the whole-collection identity gate
        # rejects this collection, so repair_duplicate_and_shifted_points
        # is a no-op passthrough and the duplicate survives the repair.
        (tmp_path / "vector_a.json").write_text(
            json.dumps({"id": "dup-id", "vector": [1.0]})
        )
        (tmp_path / "vector_b.json").write_text(
            json.dumps({"id": "dup-id", "vector": [2.0]})
        )
        call_count = _make_counting_repair(monkeypatch)

        manager = IDIndexManager()
        with pytest.raises(DuplicateSourceIdError):
            manager.rebuild_from_vectors(tmp_path, self_heal=True)

        assert call_count["n"] == 1, (
            "repair must be attempted exactly once -- a second failure "
            "must propagate, never trigger another repair attempt "
            "(bounded retry, no infinite loop)"
        )
        # Gate-rejected collection is left untouched: zero mutation.
        assert (tmp_path / "vector_a.json").exists()
        assert (tmp_path / "vector_b.json").exists()
        # A failed rebuild must not have written id_index.bin.
        assert not (tmp_path / IDIndexManager.INDEX_FILENAME).exists()


class TestRebuildFromVectorsPropagatesAmbiguousError:
    """DedupRepairAmbiguousError (e.g. a malformed record present
    alongside the duplicate) is MORE specific and actionable than
    DuplicateSourceIdError -- it must propagate through
    rebuild_from_vectors() unchanged, never be caught/swallowed/retried."""

    def test_malformed_record_alongside_duplicate_raises_ambiguous_error(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        _write_repairable_duplicate_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:ddd",
            index=0,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
            shard_suffix="-a",
        )
        _write_repairable_duplicate_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:ddd",
            index=0,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=1,
            line_end=10,
            shard_suffix="-b",
        )
        # A genuinely malformed record (missing 'id' field) elsewhere in
        # the same collection -- collection_dedup_repair.py's pre-mutation
        # malformed check refuses the WHOLE collection for this, raising
        # DedupRepairAmbiguousError before it ever reaches dedup
        # resolution.
        (tmp_path / "vector_malformed.json").write_text(json.dumps({"vector": [0.5]}))

        manager = IDIndexManager()
        with pytest.raises(DedupRepairAmbiguousError):
            manager.rebuild_from_vectors(tmp_path, self_heal=True)
