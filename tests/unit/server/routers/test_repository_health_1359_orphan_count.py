"""
Story #1359 (Epic #1333, S2) AC4: orphan_count exposed on the REST health
surface (/api/repositories/{repo_alias}/health).

`_to_collection_health_result()` is the pure mapping helper extracted from
the route handler's per-collection loop -- it maps an HNSWHealthService
HealthCheckResult onto the REST CollectionHealthResult model. This test
suite locks in that orphan_count propagates through unmodified, and that
valid stays the single zero-tolerance signal (no separate graded severity
introduced on the REST surface).
"""

from datetime import datetime, timezone

from code_indexer.server.routers.repository_health import (
    CollectionHealthResult,
    _to_collection_health_result,
)
from code_indexer.services.hnsw_health_service import HealthCheckResult


def _make_health_result(valid: bool, orphan_count: int) -> HealthCheckResult:
    return HealthCheckResult(
        valid=valid,
        file_exists=True,
        readable=True,
        loadable=True,
        element_count=1000,
        connections_checked=5000,
        min_inbound=0 if orphan_count else 2,
        max_inbound=10,
        orphan_count=orphan_count,
        index_path="/fake/hnsw_index.bin",
        file_size_bytes=1024,
        last_modified=datetime.now(timezone.utc),
        errors=[f"orphan {i}" for i in range(orphan_count)],
        check_duration_ms=12.3,
    )


class TestOrphanCountPropagatesToCollectionHealthResult:
    def test_zero_orphans_propagates_as_valid_and_zero_count(self):
        health_result = _make_health_result(valid=True, orphan_count=0)

        collection = _to_collection_health_result(
            "voyage-code-3", "semantic", health_result
        )

        assert isinstance(collection, CollectionHealthResult)
        assert collection.orphan_count == 0
        assert collection.valid is True

    def test_nonzero_orphans_propagates_as_invalid_with_actual_count(self):
        health_result = _make_health_result(valid=False, orphan_count=7)

        collection = _to_collection_health_result(
            "code-indexer-temporal-voyage_context_4-2025Q2",
            "temporal",
            health_result,
        )

        assert collection.orphan_count == 7
        # Zero-tolerance: any orphan_count > 0 must map to valid=False --
        # never a WARNING/lesser tier on the REST surface.
        assert collection.valid is False
