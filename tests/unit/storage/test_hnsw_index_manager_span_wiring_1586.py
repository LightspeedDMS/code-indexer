"""Story #1586 AC5: cidx.hnsw.build_index custom OTEL span wired into
HNSWIndexManager.build_index().

Proves the WIRING -- a real call into build_index() emits a real OTEL span
via create_span() -- not just that create_span() works standalone (already
covered in tests/unit/server/telemetry/). Real TracerProvider +
InMemorySpanExporter (installed via the shared otel_test_support helper),
real HNSWIndexManager, real numpy vectors -- MESSI Rule #1: no mocks of the
code under test.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from opentelemetry.trace import StatusCode

from code_indexer.storage.hnsw_index_manager import HNSWIndexManager

from tests.unit.server.telemetry.otel_test_support import active_span_exporter


def _find_span(exporter, name: str):
    for span in exporter.get_finished_spans():
        if span.name == name:
            return span
    return None


class TestHnswBuildIndexSpanSuccess:
    def test_build_index_emits_span(self, tmp_path: Path):
        manager = HNSWIndexManager(vector_dim=32)
        vectors = np.random.randn(20, 32).astype(np.float32)
        ids = [f"vec_{i}" for i in range(20)]

        with active_span_exporter() as exporter:
            manager.build_index(tmp_path, vectors, ids)

        span = _find_span(exporter, "cidx.hnsw.build_index")
        assert span is not None, "cidx.hnsw.build_index span not emitted"
        assert span.status.status_code == StatusCode.UNSET
        assert span.attributes.get("collection_path") == str(tmp_path)


class TestHnswBuildIndexSpanFailure:
    def test_build_index_failure_records_error_span(self, tmp_path: Path):
        manager = HNSWIndexManager(vector_dim=32)
        # Mismatched IDs length triggers the real ValueError raised by
        # build_index's own input validation -- a genuine failure path,
        # not an injected/mocked one.
        vectors = np.random.randn(20, 32).astype(np.float32)
        ids = [f"vec_{i}" for i in range(5)]  # wrong length on purpose

        with active_span_exporter() as exporter:
            with pytest.raises(ValueError):
                manager.build_index(tmp_path, vectors, ids)

        span = _find_span(exporter, "cidx.hnsw.build_index")
        assert span is not None, "cidx.hnsw.build_index span not emitted"
        assert span.status.status_code == StatusCode.ERROR
        assert len(span.events) >= 1, "exception must be recorded on the span"
        assert span.events[0].name == "exception"


def _build_index_and_load_for_incremental_update(
    manager, collection_path, vectors, ids
):
    """Shared setup: build a real initial index, then reopen it via the
    real production incremental-update loader -- same round-trip
    save_incremental_update's actual callers (add_or_update_vector) use."""
    manager.build_index(collection_path, vectors, ids)
    return manager.load_for_incremental_update(collection_path)


class TestHnswSaveIncrementalUpdateSpanSuccess:
    def test_save_incremental_update_emits_span(self, tmp_path: Path):
        manager = HNSWIndexManager(vector_dim=32)
        vectors = np.random.randn(10, 32).astype(np.float32)
        ids = [f"vec_{i}" for i in range(10)]

        index, id_to_label, label_to_id, next_label = (
            _build_index_and_load_for_incremental_update(
                manager, tmp_path, vectors, ids
            )
        )
        new_vector = np.random.randn(32).astype(np.float32)
        _label, id_to_label, label_to_id, _next_label = manager.add_or_update_vector(
            index=index,
            point_id="vec_new",
            vector=new_vector,
            id_to_label=id_to_label,
            label_to_id=label_to_id,
            next_label=next_label,
        )

        with active_span_exporter() as exporter:
            manager.save_incremental_update(
                index=index,
                collection_path=tmp_path,
                id_to_label=id_to_label,
                label_to_id=label_to_id,
                vector_count=11,
            )

        span = _find_span(exporter, "cidx.hnsw.save_incremental_update")
        assert span is not None, "cidx.hnsw.save_incremental_update span not emitted"
        assert span.status.status_code == StatusCode.UNSET
        assert span.attributes.get("collection_path") == str(tmp_path)


class TestHnswSaveIncrementalUpdateSpanFailure:
    def test_save_incremental_update_failure_records_error_span(self, tmp_path: Path):
        manager = HNSWIndexManager(vector_dim=32)
        vectors = np.random.randn(10, 32).astype(np.float32)
        ids = [f"vec_{i}" for i in range(10)]

        index, id_to_label, label_to_id, _next_label = (
            _build_index_and_load_for_incremental_update(
                manager, tmp_path, vectors, ids
            )
        )

        # Genuine failure, no mocking: a collection_path that was never
        # created on disk makes tempfile.mkstemp() raise FileNotFoundError
        # from inside save_incremental_update's own atomic-write sequence.
        missing_path = tmp_path / "does_not_exist"

        with active_span_exporter() as exporter:
            with pytest.raises(FileNotFoundError):
                manager.save_incremental_update(
                    index=index,
                    collection_path=missing_path,
                    id_to_label=id_to_label,
                    label_to_id=label_to_id,
                    vector_count=10,
                )

        span = _find_span(exporter, "cidx.hnsw.save_incremental_update")
        assert span is not None, "cidx.hnsw.save_incremental_update span not emitted"
        assert span.status.status_code == StatusCode.ERROR
        assert len(span.events) >= 1, "exception must be recorded on the span"
        assert span.events[0].name == "exception"
