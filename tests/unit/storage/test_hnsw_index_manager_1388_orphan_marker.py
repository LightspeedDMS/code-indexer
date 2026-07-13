"""
Bug #1388 (remediation after review rejection): HNSW finalize-time orphan
detect+repair (Story #1359, Epic #1333) has NEVER been observed in staging's
server-side admin logs, even after a post-deploy golden-repo refresh that
provably routes through the shared HNSWIndexManager build/finalize path.

REJECTED first attempt: threading a `total=0` marker through the existing
`progress_callback`/--progress-json wire protocol. Proven wrong by two
independent, compounding real-boundary facts:

  (a) The REAL --progress-json child callback in cli.py gates every event on
      `if total and total > 0:` -- a `total=0` marker event is dropped
      inside the child before it is even written to stdout.
  (b) Even if it were written, the parent's `run_with_popen_progress`
      applies a monotonic high-water-mark suppression (`_emit`) on the
      percentage channel -- HNSW finalize happens at the END of the
      semantic phase, when `high_water` is already near the phase's
      `range_end`, so a `total=0`-mapped (`range_start`) event is silently
      dropped there too.

This remediation abandons the percentage/--progress-json channel for this
event entirely. `_detect_and_repair_orphans` now emits the marker as a
plain, unbuffered line on the process's **stderr** -- not logging (so the
child's WARNING-level root logger filter never applies) and not the JSON
progress wire (so neither the child's `total > 0` gate nor the parent's
monotonic `_emit` guard can apply). The parent-side scraping half of this
fix lives in `progress_subprocess_runner.run_with_popen_progress`
(see tests/unit/services/test_progress_subprocess_runner_hnsw_orphan_forwarding_1388.py).

This test module covers the HNSWIndexManager half: `_detect_and_repair_orphans`
no longer accepts (or needs) a `progress_callback` parameter -- the marker
is emitted unconditionally (proportionate: only on the interesting
orphan_count > 0 event) directly to stderr.
"""

import json
from pathlib import Path

import pytest

from code_indexer.storage.hnsw_index_manager import (
    HNSWIndexManager,
    HNSWIntegrityRepairError,
    HNSW_ORPHAN_REPAIR_MARKER,
)
from tests.utils.hnsw_orphan_corpus import build_hnsw_index, near_tie_corpus

CORPUS_DIM = 1024
_MARKER_PREFIX = HNSW_ORPHAN_REPAIR_MARKER + ":"


class _FakeOrphanedIndex:
    """Plain fake (not a Mock, not hnswlib, not a test-grouping class) --
    same shape as the #1359 precedent test module's own documented
    exception to the anti-mock rule (repair_orphans() is proven
    deterministic against the real fork and cannot be made to fail to
    converge for real).
    """

    def __init__(self, orphans_before: int, orphans_after: int):
        self._orphans_before = orphans_before
        self._orphans_after = orphans_after
        self._check_calls = 0
        self.repair_called = False

    def check_integrity(self):
        self._check_calls += 1
        n = self._orphans_before if self._check_calls == 1 else self._orphans_after
        errors = [f"orphan {i}: zero inbound edges" for i in range(n)]
        return {
            "valid": n == 0,
            "element_count": 10,
            "connections_checked": 10,
            "min_inbound": 0 if n else 1,
            "max_inbound": 5,
            "errors": errors,
        }

    def repair_orphans(self):
        self.repair_called = True
        return {
            "orphans_before": self._orphans_before,
            "orphans_after": self._orphans_after,
            "repaired_count": self._orphans_before - self._orphans_after,
            "passes_used": 1,
            "forced_evictions": 0,
            "valid": self._orphans_after == 0,
        }


def _marker_lines(stderr_text: str) -> list:
    return [
        line for line in stderr_text.splitlines() if line.startswith(_MARKER_PREFIX)
    ]


def test_no_stderr_marker_when_no_orphans(capsys):
    """Proportionate design: the common, silent-success path (orphan_count
    == 0) must NOT emit anything on stderr."""
    manager = HNSWIndexManager(vector_dim=CORPUS_DIM)
    fake = _FakeOrphanedIndex(orphans_before=0, orphans_after=0)

    manager._detect_and_repair_orphans(fake, context="unit-test")

    captured = capsys.readouterr()
    assert _marker_lines(captured.err) == []


def test_stderr_receives_marker_on_successful_repair(capsys):
    manager = HNSWIndexManager(vector_dim=CORPUS_DIM)
    fake = _FakeOrphanedIndex(orphans_before=3, orphans_after=0)

    manager._detect_and_repair_orphans(fake, context="rebuild_from_vectors:/some/path")

    assert fake.repair_called is True
    captured = capsys.readouterr()
    # Never on stdout: stdout is reserved for the --progress-json wire.
    assert _marker_lines(captured.out) == []
    markers = _marker_lines(captured.err)
    assert len(markers) == 1, f"expected exactly one marker line, got: {markers}"
    line = markers[0]
    assert "context=rebuild_from_vectors:/some/path" in line
    assert "orphan_count=3" in line
    assert "repaired=true" in line


def test_stderr_receives_failure_marker_before_raise(capsys):
    manager = HNSWIndexManager(vector_dim=CORPUS_DIM)
    fake = _FakeOrphanedIndex(orphans_before=3, orphans_after=2)

    with pytest.raises(HNSWIntegrityRepairError):
        manager._detect_and_repair_orphans(fake, context="unit-test")

    captured = capsys.readouterr()
    markers = _marker_lines(captured.err)
    assert len(markers) == 1
    line = markers[0]
    assert "orphan_count=3" in line
    assert "repaired=false" in line
    assert "remaining=2" in line


def test_rebuild_from_vectors_forwards_marker_to_stderr_for_real_orphan_repair(
    tmp_path: Path, capsys
):
    """Mandatory regression test (issue #1388): drive the marker through the
    REAL production build path (rebuild_from_vectors -> finalize), using the
    real project hnswlib fork and the SAME near-tie corpus recipe already
    proven (#1359 precedent test) to genuinely orphan a real,
    single-threaded production build. Asserts on the process's actual
    stderr stream (capsys) -- against the OLD (rejected) implementation,
    which never wrote anything to stderr, this assertion fails outright.
    """
    size = 1000
    vectors = near_tie_corpus(
        size=size, dim=CORPUS_DIM, noise_scale=1e-6, pocket_fraction=1.0, seed=42
    )

    for i, vec in enumerate(vectors):
        vector_file = tmp_path / f"vector_{i}.json"
        with open(vector_file, "w") as f:
            json.dump({"id": f"vec_{i}", "vector": vec.tolist()}, f)
    meta_file = tmp_path / "collection_meta.json"
    with open(meta_file, "w") as f:
        json.dump({"vector_dim": CORPUS_DIM}, f)

    manager = HNSWIndexManager(vector_dim=CORPUS_DIM)

    vector_count = manager.rebuild_from_vectors(tmp_path)
    assert vector_count == size

    captured = capsys.readouterr()
    markers = _marker_lines(captured.err)
    assert len(markers) == 1, (
        f"expected exactly one orphan-repair marker on stderr from the real "
        f"production rebuild_from_vectors path, got stderr: {captured.err!r}"
    )
    assert "context=rebuild_from_vectors" in markers[0]
    assert "repaired=true" in markers[0]

    # Finalize invariant unchanged by this fix: zero orphans on disk.
    loaded = manager.load_index(tmp_path, max_elements=size)
    assert loaded is not None
    assert loaded.check_integrity()["valid"] is True


def test_save_incremental_update_forwards_marker_for_persisted_broken_fixture(
    tmp_path: Path, capsys
):
    """save_incremental_update's finalize checkpoint forwards the marker
    too, driven through the REAL production incremental-finalize path
    (persisted broken .bin fixture -> load_for_incremental_update ->
    save_incremental_update), matching the #1359 precedent test module's
    own AC5 on-disk-fixture pattern (identical recipe constants).
    """
    size = 270
    noise_scale = 0.01
    pocket_fraction = 1.0
    seed = 42
    hnsw_m = 16
    hnsw_ef_construction = 200
    single_threaded = 1

    vectors = near_tie_corpus(
        size=size,
        dim=CORPUS_DIM,
        noise_scale=noise_scale,
        pocket_fraction=pocket_fraction,
        seed=seed,
    )
    broken_index = build_hnsw_index(vectors, num_threads=single_threaded)
    orphans_before = sum(
        1 for e in broken_index.check_integrity()["errors"] if "orphan" in e
    )
    assert orphans_before > 0, "AC5 fixture recipe must start broken"

    index_file = tmp_path / HNSWIndexManager.INDEX_FILENAME
    broken_index.save_index(str(index_file))

    id_mapping = {str(i): f"vec_{i}" for i in range(size)}
    meta_file = tmp_path / "collection_meta.json"
    with open(meta_file, "w") as f:
        json.dump(
            {
                "vector_dim": CORPUS_DIM,
                "hnsw_index": {
                    "vector_count": size,
                    "vector_dim": CORPUS_DIM,
                    "M": hnsw_m,
                    "ef_construction": hnsw_ef_construction,
                    "id_mapping": id_mapping,
                },
            },
            f,
        )

    manager = HNSWIndexManager(vector_dim=CORPUS_DIM)
    index, id_to_label, label_to_id, next_label = manager.load_for_incremental_update(
        tmp_path
    )
    assert next_label == size

    manager.save_incremental_update(
        index, tmp_path, id_to_label, label_to_id, vector_count=size
    )

    captured = capsys.readouterr()
    markers = _marker_lines(captured.err)
    assert len(markers) == 1
    assert "context=incremental_update" in markers[0]
    assert "repaired=true" in markers[0]

    reloaded = manager.load_index(tmp_path, max_elements=size)
    assert reloaded is not None
    assert reloaded.check_integrity()["valid"] is True
