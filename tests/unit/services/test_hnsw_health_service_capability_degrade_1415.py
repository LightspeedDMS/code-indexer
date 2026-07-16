"""Bug #1415: HNSWHealthService must distinguish "hnswlib lacks the custom
fork's check_integrity()/repair_orphans() methods" from "the index is
genuinely corrupt/orphaned".

Before this fix, `_perform_check`'s Level 4 integrity check wraps
`index.check_integrity()` in a blanket `except Exception`, so a missing
capability (AttributeError) does NOT crash the health check -- but it DOES
get reported as `valid=False, errors=["Integrity check failed: ..."]`,
which is indistinguishable from a genuinely broken/orphaned index. That is
a false-positive ERROR on the Story #1359 AC4 zero-tolerance signal for
every single collection on a node running stock hnswlib.

Fix: a new `hnswlib_capability_available: Optional[bool]` field on
`HealthCheckResult`. When Level 4 is reached and the capability is missing,
`_perform_check` logs ONE WARNING, sets `hnswlib_capability_available=False`,
`orphan_count=None` (unknown, NOT zero -- no false "definitely clean" claim
either), and `valid=True` (every check that DID run -- exists, readable,
loadable -- passed; capability-unavailable is a SEPARATE signal, never
folded into the zero-tolerance orphan binary per Story #1359 AC4, which
stays completely unchanged for the capability-present path).

Real hnswlib fork throughout -- missing capability is simulated by
temporarily delattr-ing check_integrity/repair_orphans from the REAL
hnswlib.Index class (restored after each test).
"""

import logging
from pathlib import Path

import hnswlib
import numpy as np
import pytest

from code_indexer.services.hnsw_health_service import HNSWHealthService
from tests.utils.hnsw_orphan_corpus import build_hnsw_index

CORPUS_DIM = 1024
SINGLE_THREADED = 1


@pytest.fixture
def missing_capability():
    saved = {}
    for attr in ("check_integrity", "repair_orphans"):
        if hasattr(hnswlib.Index, attr):
            saved[attr] = getattr(hnswlib.Index, attr)
            delattr(hnswlib.Index, attr)
    try:
        yield
    finally:
        for attr, value in saved.items():
            setattr(hnswlib.Index, attr, value)


def _healthy_index_path(tmp_path: Path) -> Path:
    vectors = np.random.RandomState(7).randn(200, CORPUS_DIM).astype(np.float32)
    index = build_hnsw_index(vectors, num_threads=SINGLE_THREADED)
    index_path = tmp_path / "hnsw_index.bin"
    index.save_index(str(index_path))
    return index_path


class TestCapabilityUnavailableDoesNotReportFalseCorruption:
    def test_missing_capability_reports_distinct_field_not_valid_false(
        self, missing_capability, tmp_path, caplog
    ):
        index_path = _healthy_index_path(tmp_path)

        service = HNSWHealthService(cache_ttl_seconds=300)
        with caplog.at_level(logging.WARNING):
            result = service.check_health(str(index_path))

        assert result.hnswlib_capability_available is False
        assert result.orphan_count is None
        assert result.valid is True
        assert result.loadable is True

        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "check_integrity" in r.getMessage()
        ]
        assert len(warnings) == 1

    def test_missing_capability_does_not_fabricate_corruption_errors(
        self, missing_capability, tmp_path
    ):
        index_path = _healthy_index_path(tmp_path)

        service = HNSWHealthService(cache_ttl_seconds=300)
        result = service.check_health(str(index_path))

        assert not any("Integrity check failed" in e for e in result.errors)


class TestRegressionCapabilityPresentUnchanged:
    """When the real fork IS present, Story #1359 AC4 zero-tolerance
    behavior and the new field's value are unaffected."""

    def test_healthy_index_with_real_fork_reports_capability_true(self, tmp_path):
        index_path = _healthy_index_path(tmp_path)

        service = HNSWHealthService(cache_ttl_seconds=300)
        result = service.check_health(str(index_path))

        assert result.hnswlib_capability_available is True
        assert result.valid is True
        assert result.orphan_count == 0
