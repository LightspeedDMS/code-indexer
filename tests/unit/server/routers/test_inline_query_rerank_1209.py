"""Unit tests for Bug #1209: REST POST /api/query reranking support.

Bug: SemanticQueryRequest had no rerank_query/rerank_instruction fields and the
REST handler never called _apply_reranking_sync. This is a parity gap vs MCP/CLI.

Fix: Add the two fields to SemanticQueryRequest and wire the shared rerank funnel
(_apply_reranking_sync from code_indexer.server.mcp.reranking) into the REST
handler after results are gathered/fused and before truncation.

Tests:
  TestSemanticQueryRequestRerank - model field tests (additive, defaults None)
  TestRestQueryRerank - handler-level tests via FastAPI TestClient
    test_no_rerank_when_field_absent - no rerank_query -> funnel not called
    test_rerank_called_when_rerank_query_present - rerank_query wired -> funnel called
    test_rerank_fires_after_fusion_before_truncation - funnel receives full fused set
    test_rerank_ordering_applied_to_response - reranked order reflected in response
    test_no_rerank_query_response_identical_to_today - no rerank_query path unchanged
    test_rerank_instruction_forwarded_to_funnel - rerank_instruction threaded through
"""

import pytest
from unittest.mock import MagicMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.models.query import SemanticQueryRequest
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.routers.inline_query import register_query_routes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(username: str = "alice") -> User:
    user = MagicMock(spec=User)
    user.username = username
    user.role = UserRole.NORMAL_USER
    return user


def _qm_result(results=None, n: int = 3):
    """Minimal valid result dict from query_user_repositories."""
    if results is None:
        results = [
            {
                "file_path": f"file_{i}.py",
                "score": round(0.9 - i * 0.1, 2),
                "content": f"content of file {i}",
                "preview": None,
                "cache_handle": None,
                "total_size": None,
                "source_repo": None,
                "repository_alias": "myrepo",
            }
            for i in range(n)
        ]
    return {
        "results": results,
        "total_results": len(results),
        "query_metadata": {},
        "warning": None,
    }


def _disabled_rerank_meta():
    """Rerank metadata returned when reranking is disabled/absent."""
    return {
        "reranker_used": False,
        "reranker_provider": None,
        "rerank_time_ms": 0,
        "rerank_hint": None,
        "reranker_status": {
            "status": "disabled",
            "provider": None,
            "rerank_time_ms": None,
            "hint": None,
        },
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_semantic_query_manager():
    mgr = MagicMock()
    mgr.query_user_repositories.return_value = _qm_result(n=5)
    return mgr


@pytest.fixture
def mock_activated_repo_manager():
    arm = MagicMock()
    arm.get_activated_repos.return_value = []
    return arm


@pytest.fixture
def app_with_routes(
    mock_semantic_query_manager, mock_activated_repo_manager, monkeypatch
):
    """FastAPI app with query route registered, minimal state wired."""
    fast_app = FastAPI()
    fast_app.state.payload_cache = None
    fast_app.state.access_filtering_service = None
    fast_app.state.search_event_log_writer = None

    register_query_routes(
        fast_app,
        semantic_query_manager=mock_semantic_query_manager,
        activated_repo_manager=mock_activated_repo_manager,
    )

    from code_indexer.server.auth import dependencies as auth_deps

    fast_app.dependency_overrides[auth_deps.get_current_user] = lambda: _make_user(
        "alice"
    )

    cfg_mock = MagicMock()
    cfg_mock.get_config.return_value.node_id = "test-node"
    monkeypatch.setattr(
        "code_indexer.server.routers.inline_query.get_config_service",
        lambda: cfg_mock,
        raising=False,
    )

    return fast_app


# ---------------------------------------------------------------------------
# Test class 1: SemanticQueryRequest model field tests
# ---------------------------------------------------------------------------


class TestSemanticQueryRequestRerank:
    """SemanticQueryRequest must accept rerank_query and rerank_instruction fields."""

    def test_default_rerank_query_is_none(self):
        """rerank_query defaults to None when not provided."""
        req = SemanticQueryRequest(query_text="find auth")
        assert req.rerank_query is None

    def test_default_rerank_instruction_is_none(self):
        """rerank_instruction defaults to None when not provided."""
        req = SemanticQueryRequest(query_text="find auth")
        assert req.rerank_instruction is None

    def test_rerank_query_accepted(self):
        """rerank_query is stored correctly when provided."""
        req = SemanticQueryRequest(
            query_text="find auth", rerank_query="authentication logic"
        )
        assert req.rerank_query == "authentication logic"

    def test_rerank_instruction_accepted(self):
        """rerank_instruction is stored correctly when provided."""
        req = SemanticQueryRequest(
            query_text="find auth",
            rerank_query="authentication logic",
            rerank_instruction="Prefer security-critical code",
        )
        assert req.rerank_instruction == "Prefer security-critical code"

    def test_existing_request_without_rerank_fields_still_validates(self):
        """Existing request payloads without rerank fields still validate identically."""
        req = SemanticQueryRequest(
            query_text="find auth",
            repository_alias="myrepo",
            limit=10,
            search_mode="semantic",
        )
        assert req.rerank_query is None
        assert req.rerank_instruction is None
        assert req.query_text == "find auth"
        assert req.limit == 10

    def test_rerank_query_can_be_none_explicitly(self):
        """rerank_query=None is valid (same as absent)."""
        req = SemanticQueryRequest(query_text="find auth", rerank_query=None)
        assert req.rerank_query is None

    def test_rerank_instruction_without_rerank_query_is_allowed(self):
        """rerank_instruction without rerank_query is allowed (model-level; no-op at runtime)."""
        req = SemanticQueryRequest(
            query_text="find auth", rerank_instruction="Be concise"
        )
        assert req.rerank_instruction == "Be concise"
        assert req.rerank_query is None


# ---------------------------------------------------------------------------
# Test class 2: REST handler reranking wiring tests
# ---------------------------------------------------------------------------


class TestRestQueryRerank:
    """POST /api/query must invoke _apply_reranking_sync when rerank_query is set."""

    def test_no_rerank_when_field_absent(
        self, app_with_routes, mock_semantic_query_manager
    ):
        """Without rerank_query, _apply_reranking_sync must NOT be called."""
        with patch(
            "code_indexer.server.routers.inline_query._rest_apply_reranking_sync"
        ) as mock_rerank:
            client = TestClient(app_with_routes, raise_server_exceptions=False)
            response = client.post(
                "/api/query",
                json={"query_text": "find auth", "repository_alias": "myrepo"},
            )

        assert response.status_code == 200
        mock_rerank.assert_not_called()

    def test_rerank_called_when_rerank_query_present(
        self, app_with_routes, mock_semantic_query_manager
    ):
        """With rerank_query set, _apply_reranking_sync MUST be called.

        This test MUST fail if the funnel wiring is removed (anti-orphan check).
        """
        fake_reranked = [
            {
                "file_path": "file_2.py",
                "score": 0.7,
                "content": "content of file 2",
                "preview": None,
                "cache_handle": None,
                "total_size": None,
                "source_repo": None,
                "repository_alias": "myrepo",
                "rerank_score": 0.98,
            }
        ]
        success_rerank_meta = {
            "reranker_used": True,
            "reranker_provider": "voyage",
            "rerank_time_ms": 42,
            "rerank_hint": None,
            "reranker_status": {
                "status": "success",
                "provider": "voyage",
                "rerank_time_ms": 42,
                "hint": None,
            },
        }

        with patch(
            "code_indexer.server.routers.inline_query._rest_apply_reranking_sync",
            return_value=(fake_reranked, success_rerank_meta),
        ) as mock_rerank:
            client = TestClient(app_with_routes, raise_server_exceptions=False)
            response = client.post(
                "/api/query",
                json={
                    "query_text": "find auth",
                    "repository_alias": "myrepo",
                    "rerank_query": "authentication logic",
                },
            )

        assert response.status_code == 200, f"Unexpected status: {response.text}"
        mock_rerank.assert_called_once()

    def test_rerank_fires_after_fusion_before_truncation(
        self, app_with_routes, mock_semantic_query_manager
    ):
        """Reranker must receive the FULL fused result set, not the post-limit subset.

        We configure query_user_repositories to return 5 results, request limit=2.
        The reranker must be called with all 5 results (full fused set), NOT 2.
        The final output is truncated to 2 by the reranker itself.
        """
        # 5 results returned from qm (overfetch means the qm call gets higher limit)
        # but we verify the reranker sees them all
        all_five_results = [
            {
                "file_path": f"file_{i}.py",
                "score": round(0.9 - i * 0.1, 2),
                "content": f"content {i}",
                "preview": None,
                "cache_handle": None,
                "total_size": None,
                "source_repo": None,
                "repository_alias": "myrepo",
            }
            for i in range(5)
        ]
        mock_semantic_query_manager.query_user_repositories.return_value = _qm_result(
            results=all_five_results
        )

        # Reranker returns only 2 (the requested limit)
        reranked_two = [all_five_results[4], all_five_results[2]]
        for r in reranked_two:
            r["rerank_score"] = 0.99

        success_meta = {
            "reranker_used": True,
            "reranker_provider": "voyage",
            "rerank_time_ms": 10,
            "rerank_hint": None,
            "reranker_status": {
                "status": "success",
                "provider": "voyage",
                "rerank_time_ms": 10,
                "hint": None,
            },
        }

        captured_results_arg = []

        def capture_rerank(
            results,
            rerank_query,
            rerank_instruction,
            content_extractor,
            requested_limit,
            config_service,
        ):
            captured_results_arg.extend(results)
            return reranked_two, success_meta

        with patch(
            "code_indexer.server.routers.inline_query._rest_apply_reranking_sync",
            side_effect=capture_rerank,
        ):
            client = TestClient(app_with_routes, raise_server_exceptions=False)
            response = client.post(
                "/api/query",
                json={
                    "query_text": "find auth",
                    "repository_alias": "myrepo",
                    "rerank_query": "authentication logic",
                    "limit": 2,
                },
            )

        assert response.status_code == 200, f"Unexpected: {response.text}"
        # The reranker must have received all 5 results (after fusion, before truncation)
        assert len(captured_results_arg) == 5, (
            f"Reranker received {len(captured_results_arg)} results instead of 5 — "
            "rerank must fire BEFORE truncation on the full fused result set"
        )

    def test_rerank_ordering_applied_to_response(
        self, app_with_routes, mock_semantic_query_manager
    ):
        """Reranked ordering must be reflected in the response results."""
        original_results = [
            {
                "file_path": f"file_{i}.py",
                "score": round(0.9 - i * 0.1, 2),
                "content": f"content {i}",
                "preview": None,
                "cache_handle": None,
                "total_size": None,
                "source_repo": None,
                "repository_alias": "myrepo",
            }
            for i in range(3)
        ]
        mock_semantic_query_manager.query_user_repositories.return_value = _qm_result(
            results=original_results
        )

        # Reranker reverses the order
        reranked = list(reversed(original_results))
        for i, r in enumerate(reranked):
            r["rerank_score"] = round(0.99 - i * 0.1, 2)

        success_meta = {
            "reranker_used": True,
            "reranker_provider": "voyage",
            "rerank_time_ms": 15,
            "rerank_hint": None,
            "reranker_status": {
                "status": "success",
                "provider": "voyage",
                "rerank_time_ms": 15,
                "hint": None,
            },
        }

        with patch(
            "code_indexer.server.routers.inline_query._rest_apply_reranking_sync",
            return_value=(reranked, success_meta),
        ):
            client = TestClient(app_with_routes, raise_server_exceptions=False)
            response = client.post(
                "/api/query",
                json={
                    "query_text": "find auth",
                    "repository_alias": "myrepo",
                    "rerank_query": "authentication logic",
                },
            )

        assert response.status_code == 200
        data = response.json()
        result_paths = [r["file_path"] for r in data["results"]]
        # Reranked order is reversed: file_2, file_1, file_0
        assert result_paths[0] == "file_2.py", (
            f"First result should be file_2.py (reranked), got {result_paths[0]}"
        )
        assert result_paths[-1] == "file_0.py", (
            f"Last result should be file_0.py (reranked), got {result_paths[-1]}"
        )

    def test_no_rerank_query_response_byte_identical_to_today(
        self, app_with_routes, mock_semantic_query_manager
    ):
        """Without rerank_query, response structure is byte-identical to pre-fix behavior."""
        original_results = [
            {
                "file_path": f"file_{i}.py",
                "score": round(0.9 - i * 0.1, 2),
                "content": f"content {i}",
                "preview": None,
                "cache_handle": None,
                "total_size": None,
                "source_repo": None,
                "repository_alias": "myrepo",
            }
            for i in range(3)
        ]
        mock_semantic_query_manager.query_user_repositories.return_value = _qm_result(
            results=original_results
        )

        with patch(
            "code_indexer.server.routers.inline_query._rest_apply_reranking_sync"
        ) as mock_rerank:
            client = TestClient(app_with_routes, raise_server_exceptions=False)
            response = client.post(
                "/api/query",
                json={"query_text": "find auth", "repository_alias": "myrepo"},
            )

        assert response.status_code == 200
        # No rerank call
        mock_rerank.assert_not_called()
        # Results in original order
        data = response.json()
        result_paths = [r["file_path"] for r in data["results"]]
        assert result_paths == ["file_0.py", "file_1.py", "file_2.py"]

    def test_rerank_instruction_forwarded_to_funnel(
        self, app_with_routes, mock_semantic_query_manager
    ):
        """rerank_instruction is forwarded to _apply_reranking_sync."""
        success_meta = {
            "reranker_used": True,
            "reranker_provider": "voyage",
            "rerank_time_ms": 5,
            "rerank_hint": None,
            "reranker_status": {
                "status": "success",
                "provider": "voyage",
                "rerank_time_ms": 5,
                "hint": None,
            },
        }
        fake_reranked = [
            {
                "file_path": "file_0.py",
                "score": 0.9,
                "content": "content 0",
                "preview": None,
                "cache_handle": None,
                "total_size": None,
                "source_repo": None,
                "repository_alias": "myrepo",
                "rerank_score": 0.99,
            }
        ]

        with patch(
            "code_indexer.server.routers.inline_query._rest_apply_reranking_sync",
            return_value=(fake_reranked, success_meta),
        ) as mock_rerank:
            client = TestClient(app_with_routes, raise_server_exceptions=False)
            response = client.post(
                "/api/query",
                json={
                    "query_text": "find auth",
                    "repository_alias": "myrepo",
                    "rerank_query": "authentication logic",
                    "rerank_instruction": "Prefer security-critical code",
                },
            )

        assert response.status_code == 200
        mock_rerank.assert_called_once()
        # Verify the instruction was forwarded
        call_kwargs = mock_rerank.call_args
        # Either positional or keyword
        if call_kwargs.kwargs:
            assert (
                call_kwargs.kwargs.get("rerank_instruction")
                == "Prefer security-critical code"
            )
        else:
            # positional: (results, rerank_query, rerank_instruction, ...)
            assert call_kwargs.args[2] == "Prefer security-critical code"

    def test_rerank_query_forwarded_to_funnel(
        self, app_with_routes, mock_semantic_query_manager
    ):
        """rerank_query value is forwarded unchanged to _apply_reranking_sync."""
        success_meta = {
            "reranker_used": True,
            "reranker_provider": "voyage",
            "rerank_time_ms": 5,
            "rerank_hint": None,
            "reranker_status": {
                "status": "success",
                "provider": "voyage",
                "rerank_time_ms": 5,
                "hint": None,
            },
        }

        with patch(
            "code_indexer.server.routers.inline_query._rest_apply_reranking_sync",
            return_value=([], success_meta),
        ) as mock_rerank:
            client = TestClient(app_with_routes, raise_server_exceptions=False)
            response = client.post(
                "/api/query",
                json={
                    "query_text": "find auth",
                    "repository_alias": "myrepo",
                    "rerank_query": "MY SPECIFIC RERANK QUERY",
                },
            )

        assert response.status_code == 200
        call_kwargs = mock_rerank.call_args
        if call_kwargs.kwargs:
            assert call_kwargs.kwargs.get("rerank_query") == "MY SPECIFIC RERANK QUERY"
        else:
            assert call_kwargs.args[1] == "MY SPECIFIC RERANK QUERY"
