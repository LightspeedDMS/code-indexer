"""Tests for the admin_embedding_stats_query MCP handler (Story #1418 Phase 3
Component 7).

Mirrors get_job_statistics's lighter-weight pattern (no TOTP step-up
elevation -- this is a read-only reporting tool, not a sensitive
write-adjacent action like admin_logs_query's export capability) with an
explicit admin-role check like admin_logs_query. Backed by a REAL
EmbeddingCallStatsSqliteBackend (Anti-Mock), injected via the established
`patch("code_indexer.server.mcp.handlers._utils.app_module")` MCP handler
test pattern.
"""

from __future__ import annotations

import time
from unittest.mock import patch

from .conftest import extract_mcp_data


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
            ),
        ]
    )


class TestAdminEmbeddingStatsQueryRequiresAdminRole:
    def test_non_admin_returns_permission_denied(self) -> None:
        from code_indexer.server.mcp.handlers.admin import (
            handle_admin_embedding_stats_query,
        )
        from code_indexer.server.auth.user_manager import User, UserRole

        user = User(
            username="normie",
            role=UserRole.NORMAL_USER,
            password_hash="x",
            created_at="2026-01-01T00:00:00Z",
        )

        result = extract_mcp_data(handle_admin_embedding_stats_query({}, user))

        assert result["success"] is False
        assert "admin" in result["error"].lower()


class TestAdminEmbeddingStatsQueryReturnsRecords:
    def test_admin_gets_all_records_with_no_filters(self, tmp_path) -> None:
        from code_indexer.server.mcp.handlers.admin import (
            handle_admin_embedding_stats_query,
        )
        from code_indexer.server.auth.user_manager import User, UserRole
        from code_indexer.server.services.embedding_call_stats import (
            EmbeddingCallStatsSqliteBackend,
        )

        backend = EmbeddingCallStatsSqliteBackend(str(tmp_path / "stats.db"))
        _seed_records(backend)

        admin_user = User(
            username="admin",
            role=UserRole.ADMIN,
            password_hash="x",
            created_at="2026-01-01T00:00:00Z",
        )

        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module"
        ) as mock_app_module:
            mock_app_module.app.state.backend_registry.embedding_call_stats = backend
            result = extract_mcp_data(
                handle_admin_embedding_stats_query({}, admin_user)
            )

        assert result["success"] is True
        assert result["count"] == 2

    def test_admin_filters_by_provider(self, tmp_path) -> None:
        from code_indexer.server.mcp.handlers.admin import (
            handle_admin_embedding_stats_query,
        )
        from code_indexer.server.auth.user_manager import User, UserRole
        from code_indexer.server.services.embedding_call_stats import (
            EmbeddingCallStatsSqliteBackend,
        )

        backend = EmbeddingCallStatsSqliteBackend(str(tmp_path / "stats.db"))
        _seed_records(backend)

        admin_user = User(
            username="admin",
            role=UserRole.ADMIN,
            password_hash="x",
            created_at="2026-01-01T00:00:00Z",
        )

        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module"
        ) as mock_app_module:
            mock_app_module.app.state.backend_registry.embedding_call_stats = backend
            result = extract_mcp_data(
                handle_admin_embedding_stats_query({"provider": "cohere"}, admin_user)
            )

        assert result["success"] is True
        assert result["count"] == 1
        assert result["records"][0]["provider"] == "cohere"


class TestAdminEmbeddingStatsQueryRegistration:
    def test_registered_in_handler_registry(self) -> None:
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "admin_embedding_stats_query" in HANDLER_REGISTRY

    def test_registered_tool_doc_exists_in_tool_registry(self) -> None:
        from code_indexer.server.mcp.tools import TOOL_REGISTRY

        assert "admin_embedding_stats_query" in TOOL_REGISTRY


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
