"""
Story #1359 (Epic #1333, S2) AC4: zero-tolerance orphan health check.

check_hnsw_health / HNSWHealthService must expose orphan_count explicitly
(not buried inside the generic `errors` string list) so CLI/MCP/REST/Web
surfaces can all report it. Design is STRICT BINARY (settled during
maintainer review): orphan_count == 0 is the only OK condition; any
orphan_count > 0 is ERROR, with NO intermediate WARNING tier and NO
configurable threshold. hnswlib's own check_integrity() already ties `valid`
to `errors.empty()` (proven in this story's spike check against the real
fork), so orphan_count > 0 already implies valid is False -- this test suite
locks in that invariant explicitly so a future hnswlib change cannot
silently reintroduce a graded/lenient state.

Real project hnswlib fork throughout. Zero mocks -- genuinely broken and
genuinely healthy on-disk indexes are built via the real fork and S1's
committed corpus generator.
"""

from pathlib import Path

import numpy as np

from code_indexer.services.hnsw_health_service import HNSWHealthService
from tests.utils.hnsw_orphan_corpus import build_hnsw_index, near_tie_corpus

CORPUS_DIM = 1024
SINGLE_THREADED = 1


def _orphan_count_from_errors(errors) -> int:
    return sum(1 for e in errors if "orphan" in e)


class TestOrphanCountFieldOnHealthyIndex:
    def test_healthy_index_reports_zero_orphan_count(self, tmp_path: Path):
        vectors = np.random.RandomState(7).randn(200, CORPUS_DIM).astype(np.float32)
        index = build_hnsw_index(vectors, num_threads=SINGLE_THREADED)
        assert index.check_integrity()["valid"] is True

        index_path = tmp_path / "hnsw_index.bin"
        index.save_index(str(index_path))

        service = HNSWHealthService(cache_ttl_seconds=300)
        result = service.check_health(str(index_path))

        assert result.valid is True
        assert result.orphan_count == 0


class TestOrphanCountFieldOnBrokenIndex:
    def test_broken_index_reports_actual_orphan_count(self, tmp_path: Path):
        vectors = near_tie_corpus(
            size=1000, dim=CORPUS_DIM, noise_scale=1e-6, pocket_fraction=1.0, seed=42
        )
        index = build_hnsw_index(vectors, num_threads=SINGLE_THREADED)
        integrity = index.check_integrity()
        expected_orphan_count = _orphan_count_from_errors(integrity["errors"])
        assert expected_orphan_count > 0, "fixture must start broken"

        index_path = tmp_path / "hnsw_index.bin"
        index.save_index(str(index_path))

        service = HNSWHealthService(cache_ttl_seconds=300)
        result = service.check_health(str(index_path))

        assert result.orphan_count == expected_orphan_count
        # AC4 zero-tolerance invariant: orphan_count > 0 must ALWAYS imply
        # valid is False -- no WARNING tier, no leniency.
        assert result.valid is False

    def test_repaired_index_reports_zero_orphan_count_and_valid_true(
        self, tmp_path: Path
    ):
        vectors = near_tie_corpus(
            size=1000, dim=CORPUS_DIM, noise_scale=1e-6, pocket_fraction=1.0, seed=42
        )
        index = build_hnsw_index(vectors, num_threads=SINGLE_THREADED)
        assert _orphan_count_from_errors(index.check_integrity()["errors"]) > 0

        index.repair_orphans()
        assert index.check_integrity()["valid"] is True

        index_path = tmp_path / "hnsw_index.bin"
        index.save_index(str(index_path))

        service = HNSWHealthService(cache_ttl_seconds=300)
        result = service.check_health(str(index_path))

        assert result.valid is True
        assert result.orphan_count == 0
