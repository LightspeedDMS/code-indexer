"""Bug #1415: the REST health surface (CollectionHealthResult, built by
_to_collection_health_result) must propagate HealthCheckResult's new
`hnswlib_capability_available` field, so REST/Web consumers can distinguish
"hnswlib lacks the fork" from "genuinely corrupt/orphaned index" -- the same
distinction HealthCheckResult itself now carries (see
tests/unit/services/test_hnsw_health_service_capability_degrade_1415.py).
"""

from code_indexer.server.services.repository_health_aggregator import (
    CollectionHealthResult,
    _to_collection_health_result,
)
from code_indexer.services.hnsw_health_service import HealthCheckResult


def _make_health_result(hnswlib_capability_available) -> HealthCheckResult:
    # mypy cannot see pydantic's Field()-generated __init__ kwargs here --
    # same pre-existing idiom as production hnsw_health_service.py and the
    # sibling test_repository_health_aggregator_1394.py::_make_health_result.
    return HealthCheckResult(  # type: ignore[call-arg]
        valid=True,
        file_exists=True,
        readable=True,
        loadable=True,
        orphan_count=None,
        hnswlib_capability_available=hnswlib_capability_available,
        index_path="/fake/hnsw_index.bin",
        file_size_bytes=1024,
        errors=[],
        check_duration_ms=1.0,
    )


class TestCapabilityFieldPropagation:
    def test_missing_capability_propagates_false(self):
        health_result = _make_health_result(hnswlib_capability_available=False)
        collection = _to_collection_health_result(
            "voyage-code-3", "semantic", health_result
        )
        assert isinstance(collection, CollectionHealthResult)
        assert collection.hnswlib_capability_available is False

    def test_present_capability_propagates_true(self):
        health_result = _make_health_result(hnswlib_capability_available=True)
        collection = _to_collection_health_result(
            "voyage-code-3", "semantic", health_result
        )
        assert collection.hnswlib_capability_available is True
