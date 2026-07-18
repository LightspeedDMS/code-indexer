"""Tests for the embedding-stats admin query endpoint (Story #1418 Phase 3
Component 7).

Mirrors the direct-function-call test pattern established for
hnsw_orphan_sweep_admin.py (Story #1360): call the endpoint function
directly with a synthetic Request-like SimpleNamespace, bypassing FastAPI
TestClient/auth machinery entirely -- but backed by a REAL
EmbeddingCallStatsSqliteBackend (Anti-Mock: real SQLite, real rows,
real filtering), not a fake/mock backend.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

_OLDER_RECORD_AGE_SECONDS = 100
_NEWER_RECORD_AGE_SECONDS = 50


def _make_request(backend_registry):
    app_state = SimpleNamespace(backend_registry=backend_registry)
    app = SimpleNamespace(state=app_state)
    return SimpleNamespace(app=app)


def _make_request_with_no_backend_registry_attribute():
    """app.state genuinely lacks a backend_registry attribute -- distinct
    from `_make_request(None)`, which sets the attribute TO None. The
    router uses getattr(..., default=None) so both must 503 identically,
    but this proves the attribute-absent case specifically."""
    app_state = SimpleNamespace()  # no backend_registry attribute at all
    app = SimpleNamespace(state=app_state)
    return SimpleNamespace(app=app)


def _seed_records(backend, now: float):
    from code_indexer.server.services.embedding_call_stats import EmbeddingCallRecord

    records = [
        EmbeddingCallRecord(
            provider="voyageai",
            call_type="embed",
            model="voyage-code-3",
            item_count=1,
            token_count=10,
            batch_size=1,
            purpose="index",
            success=True,
            latency_ms=5,
            occurred_at=now - _OLDER_RECORD_AGE_SECONDS,
            golden_repo_alias="repo-a",
            job_id="job-1",
        ),
        EmbeddingCallRecord(
            provider="cohere",
            call_type="rerank",
            model="rerank-english-v3.0",
            item_count=5,
            token_count=0,
            batch_size=5,
            purpose="query",
            success=True,
            latency_ms=8,
            occurred_at=now - _NEWER_RECORD_AGE_SECONDS,
            golden_repo_alias="repo-b",
            job_id="job-2",
        ),
    ]
    backend.insert_batch(records)


@pytest.fixture()
def seed_now():
    return time.time()


@pytest.fixture()
def real_backend(tmp_path, seed_now):
    from code_indexer.server.services.embedding_call_stats import (
        EmbeddingCallStatsSqliteBackend,
    )

    backend = EmbeddingCallStatsSqliteBackend(str(tmp_path / "stats.db"))
    _seed_records(backend, seed_now)
    return backend


class TestQueryEmbeddingCallStatsFiltersByProvider:
    def test_filters_to_matching_provider_only(self, real_backend) -> None:
        from code_indexer.server.routers.embedding_stats_admin import (
            query_embedding_call_stats,
        )

        request = _make_request(SimpleNamespace(embedding_call_stats=real_backend))

        result = query_embedding_call_stats(
            request, provider="voyageai", current_user=None
        )

        assert result["count"] == 1
        assert result["records"][0]["provider"] == "voyageai"

    def test_no_filters_returns_all_records(self, real_backend) -> None:
        from code_indexer.server.routers.embedding_stats_admin import (
            query_embedding_call_stats,
        )

        request = _make_request(SimpleNamespace(embedding_call_stats=real_backend))

        result = query_embedding_call_stats(request, current_user=None)

        assert result["count"] == 2


class TestQueryEmbeddingCallStatsFiltersByOtherDimensions:
    def test_filters_by_purpose(self, real_backend) -> None:
        from code_indexer.server.routers.embedding_stats_admin import (
            query_embedding_call_stats,
        )

        request = _make_request(SimpleNamespace(embedding_call_stats=real_backend))

        result = query_embedding_call_stats(request, purpose="query", current_user=None)

        assert result["count"] == 1
        assert result["records"][0]["purpose"] == "query"

    def test_filters_by_golden_repo_alias(self, real_backend) -> None:
        from code_indexer.server.routers.embedding_stats_admin import (
            query_embedding_call_stats,
        )

        request = _make_request(SimpleNamespace(embedding_call_stats=real_backend))

        result = query_embedding_call_stats(
            request, golden_repo_alias="repo-a", current_user=None
        )

        assert result["count"] == 1
        assert result["records"][0]["golden_repo_alias"] == "repo-a"

    def test_filters_by_job_id(self, real_backend) -> None:
        from code_indexer.server.routers.embedding_stats_admin import (
            query_embedding_call_stats,
        )

        request = _make_request(SimpleNamespace(embedding_call_stats=real_backend))

        result = query_embedding_call_stats(request, job_id="job-2", current_user=None)

        assert result["count"] == 1
        assert result["records"][0]["job_id"] == "job-2"


class TestQueryEmbeddingCallStatsFiltersByTimeRange:
    def test_filters_to_newer_record_within_time_window(
        self, real_backend, seed_now
    ) -> None:
        """start_time/end_time must narrow results to only the record whose
        occurred_at falls within [start_time, end_time)."""
        from code_indexer.server.routers.embedding_stats_admin import (
            query_embedding_call_stats,
        )

        request = _make_request(SimpleNamespace(embedding_call_stats=real_backend))
        window_start = seed_now - _NEWER_RECORD_AGE_SECONDS - 10
        window_end = seed_now

        result = query_embedding_call_stats(
            request,
            start_time=window_start,
            end_time=window_end,
            current_user=None,
        )

        assert result["count"] == 1
        occurred_at = result["records"][0]["occurred_at"]
        assert window_start <= occurred_at < window_end
        assert result["records"][0]["job_id"] == "job-2"


class TestQueryEmbeddingCallStatsUnavailableBackend:
    def test_raises_503_when_backend_registry_attribute_absent(self) -> None:
        from fastapi import HTTPException

        from code_indexer.server.routers.embedding_stats_admin import (
            query_embedding_call_stats,
        )

        request = _make_request_with_no_backend_registry_attribute()

        with pytest.raises(HTTPException) as exc_info:
            query_embedding_call_stats(request, current_user=None)

        assert exc_info.value.status_code == 503

    def test_raises_503_when_backend_registry_is_none(self) -> None:
        from fastapi import HTTPException

        from code_indexer.server.routers.embedding_stats_admin import (
            query_embedding_call_stats,
        )

        request = _make_request(None)

        with pytest.raises(HTTPException) as exc_info:
            query_embedding_call_stats(request, current_user=None)

        assert exc_info.value.status_code == 503

    def test_raises_503_when_embedding_call_stats_field_missing(self) -> None:
        """backend_registry can exist without embedding_call_stats being
        wired (e.g. a partially-initialized registry) -- must ALSO 503,
        mirroring hnsw_orphan_sweep_admin.py's own unavailable-backend
        guard for its analogous field."""
        from fastapi import HTTPException

        from code_indexer.server.routers.embedding_stats_admin import (
            query_embedding_call_stats,
        )

        request = _make_request(SimpleNamespace(embedding_call_stats=None))

        with pytest.raises(HTTPException) as exc_info:
            query_embedding_call_stats(request, current_user=None)

        assert exc_info.value.status_code == 503


class TestQueryEmbeddingCallStatsBoundaryValidation:
    def test_limit_below_one_raises_400(self, real_backend) -> None:
        from fastapi import HTTPException

        from code_indexer.server.routers.embedding_stats_admin import (
            query_embedding_call_stats,
        )

        request = _make_request(SimpleNamespace(embedding_call_stats=real_backend))

        with pytest.raises(HTTPException) as exc_info:
            query_embedding_call_stats(request, limit=0, current_user=None)

        assert exc_info.value.status_code == 400

    def test_limit_above_max_raises_400(self, real_backend) -> None:
        from fastapi import HTTPException

        from code_indexer.server.routers.embedding_stats_admin import (
            query_embedding_call_stats,
        )

        request = _make_request(SimpleNamespace(embedding_call_stats=real_backend))

        with pytest.raises(HTTPException) as exc_info:
            query_embedding_call_stats(request, limit=1001, current_user=None)

        assert exc_info.value.status_code == 400

    def test_negative_offset_raises_400(self, real_backend) -> None:
        from fastapi import HTTPException

        from code_indexer.server.routers.embedding_stats_admin import (
            query_embedding_call_stats,
        )

        request = _make_request(SimpleNamespace(embedding_call_stats=real_backend))

        with pytest.raises(HTTPException) as exc_info:
            query_embedding_call_stats(request, offset=-1, current_user=None)

        assert exc_info.value.status_code == 400


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
