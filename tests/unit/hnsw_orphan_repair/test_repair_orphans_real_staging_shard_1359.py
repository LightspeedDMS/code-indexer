"""
Story #1359 (Epic #1333, S2) AC6: real-shard regression test.

Codifies the EXACT real-world regression this story fixed -- not just the
synthetic near-tie/exact-tie corpus classes S1 already covers (AC1-AC5), but
the actual production artifact that surfaced it: a genuinely orphaned HNSW
shard pulled from the staging server (repo `click`, collection
code-indexer-temporal-voyage_context_4-2025Q2, elements 270-273).

Fixture provenance: pulled via MCP SSH from the staging cidx-server
(golden repo `click`) on 2026-07-11, with NO modifications to the staging
server itself. The point-id mapping in collection_meta.json references only
public git commit hashes from the pallets/click open-source project
(CIDX's standard test golden repo) -- no secrets, no PII, no internal
hostnames or IP addresses.

Regime classification (recorded on the story, reproduced here): elements
270-273 are bit-identical to each other and to element 274 (cosine ==
1.0000000000 exactly, numpy.array_equal == True) -- this is the EXACT-TIE
(race) regime (S1's regime 2), consistent with a multi-threaded add_items
back-link race on genuinely duplicate content (e.g. identical commit diffs
across adjacent temporal commits), not a near-tie floating-point
degeneracy.

Real project hnswlib fork only. Zero mocks -- this loads and repairs the
actual downloaded production artifact.
"""

import json
from pathlib import Path

import hnswlib
import numpy as np

FIXTURE_DIR = (
    Path(__file__).parents[2]
    / "fixtures"
    / "hnsw_orphan_repair"
    / "click_temporal_2025q2_real_shard"
)
FIXTURE_INDEX = FIXTURE_DIR / "hnsw_index.bin"
FIXTURE_META = FIXTURE_DIR / "collection_meta.json"

# Known regression fingerprint (recorded on issue #1359 AC6 evidence).
EXPECTED_VECTOR_DIM = 1024
EXPECTED_ELEMENT_COUNT = 484
EXPECTED_ORPHAN_IDS = frozenset({270, 271, 272, 273})
KNOWN_TWIN_ID = 274  # bit-identical to the orphans but retained connectivity


def _load_fixture_index() -> "hnswlib.Index":
    index = hnswlib.Index(space="cosine", dim=EXPECTED_VECTOR_DIM)
    index.load_index(str(FIXTURE_INDEX), max_elements=EXPECTED_ELEMENT_COUNT)
    return index


def _orphan_ids(check_integrity_result: dict) -> frozenset:
    return frozenset(
        int(e.split()[1]) for e in check_integrity_result["errors"] if "orphan" in e
    )


class TestFixtureFilesExist:
    def test_fixture_index_file_exists(self):
        assert FIXTURE_INDEX.exists(), f"missing fixture: {FIXTURE_INDEX}"

    def test_fixture_meta_file_exists(self):
        assert FIXTURE_META.exists(), f"missing fixture: {FIXTURE_META}"

    def test_fixture_meta_matches_expected_shape(self):
        meta = json.loads(FIXTURE_META.read_text())
        hnsw_meta = meta["hnsw_index"]
        assert hnsw_meta["vector_dim"] == EXPECTED_VECTOR_DIM
        assert hnsw_meta["vector_count"] == EXPECTED_ELEMENT_COUNT
        assert hnsw_meta["M"] == 16
        assert hnsw_meta["ef_construction"] == 200
        assert hnsw_meta["space"] == "cosine"


class TestRealStagingShardKnownRegression:
    """Reproduces the exact regression recorded on issue #1359 AC6."""

    def test_pre_repair_check_integrity_shows_known_orphans(self):
        index = _load_fixture_index()

        result = index.check_integrity()

        assert result["valid"] is False
        assert result["element_count"] == EXPECTED_ELEMENT_COUNT
        assert result["min_inbound"] == 0
        assert _orphan_ids(result) == EXPECTED_ORPHAN_IDS

    def test_orphans_are_exact_tie_regime_bit_identical_vectors(self):
        """Confirms the regime classification recorded on the story: these
        orphans are bit-identical (exact-tie race), not near-tie."""
        index = _load_fixture_index()
        orphan_ids = sorted(EXPECTED_ORPHAN_IDS)

        vecs = np.array(index.get_items(orphan_ids + [KNOWN_TWIN_ID]))

        for i in range(len(vecs)):
            for j in range(i + 1, len(vecs)):
                assert np.array_equal(vecs[i], vecs[j]), (
                    f"expected bit-identical vectors (exact-tie regime) "
                    f"at indices {i},{j}"
                )

    def test_repair_orphans_drives_known_regression_to_zero(self):
        index = _load_fixture_index()
        before = index.check_integrity()
        assert _orphan_ids(before) == EXPECTED_ORPHAN_IDS

        repair_result = index.repair_orphans()

        after = index.check_integrity()
        assert after["valid"] is True
        assert _orphan_ids(after) == frozenset()
        assert repair_result["orphans_before"] == len(EXPECTED_ORPHAN_IDS)
        assert repair_result["orphans_after"] == 0
        # min_inbound must improve from the known-broken 0.
        assert after["min_inbound"] >= 1

    def test_recall_restored_for_previously_orphaned_elements(self):
        index = _load_fixture_index()
        orphan_ids = sorted(EXPECTED_ORPHAN_IDS)
        vecs = index.get_items(orphan_ids)

        index.repair_orphans()
        assert index.check_integrity()["valid"] is True

        index.set_ef(200)
        for oid, vec in zip(orphan_ids, vecs):
            labels, dists = index.knn_query(np.array(vec), k=4)
            # Exact-tie block: the previously-orphaned element must be
            # discoverable at distance ~0 among its own bit-identical twins.
            assert any(
                label in EXPECTED_ORPHAN_IDS and dist <= 1e-4
                for label, dist in zip(labels[0], dists[0])
            ), f"element {oid} not recall-restored post-repair"
