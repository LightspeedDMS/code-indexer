"""Tests for the embedding-stats Web UI dashboard page (Story #1418 Phase 3
Component 8).

Two layers, per this project's established pattern for full-page routes
with session dependencies (mirrors self_monitoring_page's split between
_load_self_monitoring_data (testable helper) and the route itself, which is
thin session/template glue not independently unit tested):

  1. _load_embedding_stats_dashboard_data(backend, ...) -- a pure,
     independently testable helper, exercised here against a REAL
     EmbeddingCallStatsSqliteBackend (Anti-Mock).
  2. embedding_stats_dashboard.html -- structural HTML/template checks,
     mirroring test_self_monitoring_pagination.py's "read the template
     file, check for required patterns" convention.
"""

from __future__ import annotations

import time
from pathlib import Path


def _seed_records(backend):
    from code_indexer.server.services.embedding_call_stats import EmbeddingCallRecord

    now = time.time()
    backend.insert_batch(
        [
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
                occurred_at=now - 10,
                golden_repo_alias="repo-a",
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
                occurred_at=now - 5,
                golden_repo_alias="repo-b",
            ),
        ]
    )


class TestLoadEmbeddingStatsDashboardData:
    def test_no_filters_returns_all_records_as_dicts(self, tmp_path) -> None:
        from code_indexer.server.services.embedding_call_stats import (
            EmbeddingCallStatsSqliteBackend,
        )
        from code_indexer.server.web.routes import (
            _load_embedding_stats_dashboard_data,
        )

        backend = EmbeddingCallStatsSqliteBackend(str(tmp_path / "stats.db"))
        _seed_records(backend)

        result = _load_embedding_stats_dashboard_data(backend)

        assert len(result) == 2
        assert isinstance(result[0], dict)
        assert {"provider", "purpose", "occurred_at"} <= result[0].keys()

    def test_filters_by_provider(self, tmp_path) -> None:
        from code_indexer.server.services.embedding_call_stats import (
            EmbeddingCallStatsSqliteBackend,
        )
        from code_indexer.server.web.routes import (
            _load_embedding_stats_dashboard_data,
        )

        backend = EmbeddingCallStatsSqliteBackend(str(tmp_path / "stats.db"))
        _seed_records(backend)

        result = _load_embedding_stats_dashboard_data(backend, provider="voyageai")

        assert len(result) == 1
        assert result[0]["provider"] == "voyageai"

    def test_backend_none_returns_empty_list(self) -> None:
        from code_indexer.server.web.routes import (
            _load_embedding_stats_dashboard_data,
        )

        assert _load_embedding_stats_dashboard_data(None) == []


def _read_template() -> str:
    template_path = (
        Path(__file__).resolve().parents[4]
        / "src"
        / "code_indexer"
        / "server"
        / "web"
        / "templates"
        / "embedding_stats_dashboard.html"
    )
    return template_path.read_text()


class TestEmbeddingStatsDashboardTemplateStructure:
    def test_template_file_exists(self) -> None:
        html = _read_template()
        assert "embedding" in html.lower()

    def test_template_has_filter_form_fields(self) -> None:
        html = _read_template()
        for field_name in ("provider", "purpose", "golden_repo_alias", "job_id"):
            assert f'name="{field_name}"' in html, f"Missing filter input: {field_name}"

    def test_template_has_results_table(self) -> None:
        html = _read_template()
        assert "<table" in html


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
