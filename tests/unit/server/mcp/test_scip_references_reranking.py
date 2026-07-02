"""Unit tests for Story #659: rerank_query/rerank_instruction on scip_references.

Tests verify:
1. Tool schema includes rerank_query and rerank_instruction parameters.
2. When rerank_query=None, _apply_reranking_sync is not called (no overhead).
3. When rerank_query is provided, _apply_reranking_sync is called with context extractor.
4. Response includes query_metadata with reranker telemetry (no rerank_hint field).
5. Overfetch: fetch_limit = min(limit * 5, 200) when rerank_query is set.
6. Provider fallback: Voyage fails, Cohere used, reflected in metadata.

Story #659, Epic #649.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.mcp.tools import TOOL_REGISTRY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_user():
    """Return a mock User with username 'testuser'."""
    from code_indexer.server.auth.user_manager import User

    user = MagicMock(spec=User)
    user.username = "testuser"
    return user


def _make_reference(
    symbol="UserService",
    project="myproject",
    file_path="src/auth.py",
    line=42,
    column=8,
    kind="reference",
    relationship="call",
    context="result = UserService.authenticate(token)",
):
    """Build a mock reference dict as returned by find_references."""
    return {
        "symbol": symbol,
        "project": project,
        "file_path": file_path,
        "line": line,
        "column": column,
        "kind": kind,
        "relationship": relationship,
        "context": context,
    }


def _make_config_service_with_rerank():
    """Build a mock config_service with rerank config populated."""
    from code_indexer.server.utils.config_manager import RerankConfig

    rerank_cfg = RerankConfig(
        voyage_reranker_model="rerank-2.5",
        cohere_reranker_model="rerank-v3.5",
        overfetch_multiplier=5,
    )
    config = MagicMock()
    config.rerank_config = rerank_cfg
    svc = MagicMock()
    svc.get_config.return_value = config
    return svc


def _default_rerank_meta(used=False, provider=None, time_ms=0):
    return {
        "reranker_used": used,
        "reranker_provider": provider,
        "rerank_time_ms": time_ms,
        "rerank_hint": None,
    }


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def scip_svc_mock():
    """Patch _get_scip_query_service for all handler tests."""
    refs = [_make_reference()]
    with patch(
        "code_indexer.server.mcp.handlers._legacy._get_scip_query_service"
    ) as mock_factory:
        mock_svc = MagicMock()
        mock_svc.find_references.return_value = refs
        mock_factory.return_value = mock_svc
        yield mock_svc


@pytest.fixture
def scip_svc_with_rerank_mock():
    """Patch _get_scip_query_service and get_config_service (rerank enabled)."""
    refs = [_make_reference()]
    with (
        patch(
            "code_indexer.server.mcp.handlers._legacy._get_scip_query_service"
        ) as mock_factory,
        patch(
            "code_indexer.server.mcp.handlers._legacy.get_config_service",
            return_value=_make_config_service_with_rerank(),
        ),
    ):
        mock_svc = MagicMock()
        mock_svc.find_references.return_value = refs
        mock_factory.return_value = mock_svc
        yield mock_svc


# ---------------------------------------------------------------------------
# PART 1: Tool schema — rerank_query and rerank_instruction present
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("param_name", ["rerank_query", "rerank_instruction"])
class TestScipReferencesToolSchema:
    """scip_references tool schema must expose rerank_query and rerank_instruction."""

    def test_param_exists_in_properties(self, param_name):
        """Parameter must be present in inputSchema.properties."""
        tool = TOOL_REGISTRY["scip_references"]
        props = tool["inputSchema"]["properties"]
        assert param_name in props, (
            f"scip_references is missing '{param_name}' in inputSchema.properties"
        )

    def test_param_is_string_type(self, param_name):
        """Parameter must be declared as type string."""
        tool = TOOL_REGISTRY["scip_references"]
        props = tool["inputSchema"]["properties"]
        assert props[param_name]["type"] == "string", (
            f"scip_references property '{param_name}' must be type string"
        )

    def test_param_is_optional(self, param_name):
        """Parameter must be optional (not in required list)."""
        tool = TOOL_REGISTRY["scip_references"]
        required = tool["inputSchema"].get("required", [])
        assert param_name not in required, (
            f"scip_references property '{param_name}' must not be required"
        )


class TestScipReferencesOutputSchema:
    """scip_references output schema must expose query_metadata."""

    def test_query_metadata_in_output_schema(self):
        """query_metadata must be present in outputSchema.properties."""
        tool = TOOL_REGISTRY["scip_references"]
        output_props = tool.get("outputSchema", {}).get("properties", {})
        assert "query_metadata" in output_props, (
            "scip_references outputSchema missing 'query_metadata'"
        )


# ---------------------------------------------------------------------------
# PART 2: Reranking guard — no overhead when rerank_query is absent
# ---------------------------------------------------------------------------


class TestScipReferencesNoRerankOverhead:
    """When rerank_query is absent, _apply_reranking_sync must not be called."""

    def test_no_rerank_query_skips_reranking(self, scip_svc_mock):
        """When rerank_query=None, _apply_reranking_sync must not be invoked."""
        from code_indexer.server.mcp.handlers._legacy import scip_references

        with patch(
            "code_indexer.server.mcp.reranking._apply_reranking_sync"
        ) as mock_rerank:
            args = {
                "symbol": "UserService",
                "limit": 10,
            }
            scip_references(args, _fake_user())

        mock_rerank.assert_not_called()

    def test_empty_rerank_query_skips_reranking(self, scip_svc_mock):
        """When rerank_query='', _apply_reranking_sync must not be invoked."""
        from code_indexer.server.mcp.handlers._legacy import scip_references

        with patch(
            "code_indexer.server.mcp.reranking._apply_reranking_sync"
        ) as mock_rerank:
            args = {
                "symbol": "UserService",
                "rerank_query": "",
                "limit": 10,
            }
            scip_references(args, _fake_user())

        mock_rerank.assert_not_called()

    def test_no_rerank_query_returns_identical_results(self):
        """Without rerank_query, results match current behavior (original order)."""
        from code_indexer.server.mcp.handlers._legacy import scip_references

        refs = [
            _make_reference(context="first import"),
            _make_reference(context="second call"),
        ]
        with patch(
            "code_indexer.server.mcp.handlers._legacy._get_scip_query_service"
        ) as mock_factory:
            mock_svc = MagicMock()
            mock_svc.find_references.return_value = refs
            mock_factory.return_value = mock_svc

            args = {
                "symbol": "UserService",
                "limit": 10,
            }
            response = scip_references(args, _fake_user())

        payload = json.loads(response["content"][0]["text"])
        assert payload["success"] is True
        assert payload["results"][0]["context"] == "first import"
        assert payload["results"][1]["context"] == "second call"


# ---------------------------------------------------------------------------
# PART 3: Reranking wiring — _apply_reranking_sync called with correct extractor
# ---------------------------------------------------------------------------


class TestScipReferencesRerankingWiring:
    """scip_references calls _apply_reranking_sync with context extractor."""

    def test_rerank_called_with_context_extractor(self, scip_svc_with_rerank_mock):
        """When rerank_query is set, _apply_reranking_sync is invoked with context extractor."""
        from code_indexer.server.mcp.handlers._legacy import scip_references

        ref = _make_reference(context="result = UserService.authenticate(token)")
        scip_svc_with_rerank_mock.find_references.return_value = [ref]

        with patch(
            "code_indexer.server.mcp.reranking._apply_reranking_sync",
            return_value=(
                [ref],
                _default_rerank_meta(used=True, provider="voyage", time_ms=10),
            ),
        ) as mock_rerank:
            args = {
                "symbol": "UserService",
                "rerank_query": "references that instantiate or call UserService in production code",
                "rerank_instruction": "Focus on instantiation and production use, not imports or tests",
                "limit": 10,
            }
            scip_references(args, _fake_user())

        mock_rerank.assert_called_once()
        call_kwargs = mock_rerank.call_args[1]

        assert (
            call_kwargs["rerank_query"]
            == "references that instantiate or call UserService in production code"
        )
        assert (
            call_kwargs["rerank_instruction"]
            == "Focus on instantiation and production use, not imports or tests"
        )

        # Verify content extractor uses context field
        extractor = call_kwargs["content_extractor"]
        assert (
            extractor({"context": "result = UserService.authenticate(token)"})
            == "result = UserService.authenticate(token)"
        )
        assert (
            extractor({"context": "from auth import UserService"})
            == "from auth import UserService"
        )
        assert extractor({"context": ""}) == ""
        assert extractor({}) == ""

    def test_rerank_requested_limit_matches_user_limit(self, scip_svc_with_rerank_mock):
        """requested_limit passed to _apply_reranking_sync matches user-specified limit."""
        from code_indexer.server.mcp.handlers._legacy import scip_references

        ref = _make_reference()
        scip_svc_with_rerank_mock.find_references.return_value = [ref]

        with patch(
            "code_indexer.server.mcp.reranking._apply_reranking_sync",
            return_value=([ref], _default_rerank_meta()),
        ) as mock_rerank:
            args = {
                "symbol": "UserService",
                "rerank_query": "production instantiation of UserService",
                "limit": 7,
            }
            scip_references(args, _fake_user())

        call_kwargs = mock_rerank.call_args[1]
        assert call_kwargs["requested_limit"] == 7


# ---------------------------------------------------------------------------
# PART 4: Overfetch — find_references called with 5x limit when rerank_query set
# ---------------------------------------------------------------------------


class TestScipReferencesOverfetch:
    """When rerank_query is set, find_references must be called with overfetched limit."""

    def test_find_references_called_with_overfetch_limit(
        self, scip_svc_with_rerank_mock
    ):
        """find_references receives limit=min(requested*5, 200) when rerank_query set."""
        from code_indexer.server.mcp.handlers._legacy import scip_references

        with patch(
            "code_indexer.server.mcp.reranking._apply_reranking_sync",
            return_value=([{}], _default_rerank_meta()),
        ):
            args = {
                "symbol": "UserService",
                "rerank_query": "production instantiation",
                "limit": 10,
            }
            scip_references(args, _fake_user())

        call_kwargs = scip_svc_with_rerank_mock.find_references.call_args[1]
        assert call_kwargs["limit"] == 50, (
            f"Expected find_references called with limit=50 (10*5), got {call_kwargs['limit']}"
        )

    def test_overfetch_capped_at_200(self, scip_svc_with_rerank_mock):
        """Overfetch limit is capped at 200 even for large requested limits."""
        from code_indexer.server.mcp.handlers._legacy import scip_references

        with patch(
            "code_indexer.server.mcp.reranking._apply_reranking_sync",
            return_value=([{}], _default_rerank_meta()),
        ):
            args = {
                "symbol": "UserService",
                "rerank_query": "production instantiation",
                "limit": 100,  # 100 * 5 = 500, capped at 200
            }
            scip_references(args, _fake_user())

        call_kwargs = scip_svc_with_rerank_mock.find_references.call_args[1]
        assert call_kwargs["limit"] == 200, (
            f"Expected find_references called with limit=200 (capped), got {call_kwargs['limit']}"
        )

    def test_no_overfetch_without_rerank_query(self, scip_svc_mock):
        """Without rerank_query, find_references receives the plain requested limit."""
        from code_indexer.server.mcp.handlers._legacy import scip_references

        args = {
            "symbol": "UserService",
            "limit": 10,
        }
        scip_references(args, _fake_user())

        call_kwargs = scip_svc_mock.find_references.call_args[1]
        assert call_kwargs["limit"] == 10, (
            f"Expected find_references called with limit=10 (no reranking), got {call_kwargs['limit']}"
        )


# ---------------------------------------------------------------------------
# PART 5: Response query_metadata telemetry fields
# ---------------------------------------------------------------------------


class TestScipReferencesQueryMetadata:
    """Response must include query_metadata with reranker telemetry."""

    def _run_with_rerank_meta(self, rerank_meta: dict) -> dict:
        """Run scip_references with patched reranking; return parsed payload."""
        from code_indexer.server.mcp.handlers._legacy import scip_references

        ref = _make_reference()

        with (
            patch(
                "code_indexer.server.mcp.handlers._legacy._get_scip_query_service"
            ) as mock_factory,
            patch(
                "code_indexer.server.mcp.handlers._legacy.get_config_service",
                return_value=_make_config_service_with_rerank(),
            ),
            patch(
                "code_indexer.server.mcp.reranking._apply_reranking_sync",
                return_value=([ref], rerank_meta),
            ),
        ):
            mock_svc = MagicMock()
            mock_svc.find_references.return_value = [ref]
            mock_factory.return_value = mock_svc

            args = {
                "symbol": "UserService",
                "rerank_query": "production instantiation of UserService",
                "limit": 5,
            }
            response = scip_references(args, _fake_user())

        result: dict = json.loads(response["content"][0]["text"])
        return result

    def test_query_metadata_present_when_reranking_active(self):
        """When rerank_query is provided, response must contain query_metadata."""
        rerank_meta = _default_rerank_meta(used=True, provider="voyage", time_ms=12)
        payload = self._run_with_rerank_meta(rerank_meta)

        assert payload["success"] is True
        assert "query_metadata" in payload, "query_metadata missing from response"

    def test_query_metadata_contains_required_fields(self):
        """query_metadata must contain reranker_used, reranker_provider, rerank_time_ms."""
        rerank_meta = _default_rerank_meta(used=True, provider="voyage", time_ms=12)
        payload = self._run_with_rerank_meta(rerank_meta)

        qm = payload["query_metadata"]
        assert "reranker_used" in qm
        assert "reranker_provider" in qm
        assert "rerank_time_ms" in qm

    def test_query_metadata_voyage_provider_values(self):
        """When Voyage reranks successfully, metadata shows correct values."""
        rerank_meta = _default_rerank_meta(used=True, provider="voyage", time_ms=15)
        payload = self._run_with_rerank_meta(rerank_meta)

        qm = payload["query_metadata"]
        assert qm["reranker_used"] is True
        assert qm["reranker_provider"] == "voyage"
        assert qm["rerank_time_ms"] == 15

    def test_query_metadata_not_used_values(self):
        """When reranking not used, metadata shows not-active state."""
        rerank_meta = _default_rerank_meta(used=False, provider=None, time_ms=0)
        payload = self._run_with_rerank_meta(rerank_meta)

        qm = payload["query_metadata"]
        assert qm["reranker_used"] is False
        assert qm["reranker_provider"] is None
        assert qm["rerank_time_ms"] == 0

    def test_query_metadata_no_rerank_hint(self):
        """query_metadata must NOT contain rerank_hint — matches git_search_commits pattern."""
        rerank_meta = _default_rerank_meta(used=True, provider="voyage", time_ms=12)
        payload = self._run_with_rerank_meta(rerank_meta)

        qm = payload["query_metadata"]
        assert "rerank_hint" not in qm, (
            "rerank_hint must not appear in query_metadata (not in outputSchema)"
        )

    def test_no_rerank_query_includes_query_metadata_with_used_false(self):
        """Without rerank_query, response still includes query_metadata with reranker_used=False."""
        from code_indexer.server.mcp.handlers._legacy import scip_references

        ref = _make_reference()

        with patch(
            "code_indexer.server.mcp.handlers._legacy._get_scip_query_service"
        ) as mock_factory:
            mock_svc = MagicMock()
            mock_svc.find_references.return_value = [ref]
            mock_factory.return_value = mock_svc

            args = {
                "symbol": "UserService",
                "limit": 10,
            }
            response = scip_references(args, _fake_user())

        payload = json.loads(response["content"][0]["text"])
        assert payload["success"] is True
        assert "query_metadata" in payload
        assert payload["query_metadata"]["reranker_used"] is False


# ---------------------------------------------------------------------------
# PART 6: Provider fallback — Voyage fails, Cohere used
# ---------------------------------------------------------------------------


class TestScipReferencesProviderFallback:
    """Reranking provider fallback behavior in scip_references handler."""

    def test_cohere_fallback_reflected_in_metadata(self, scip_svc_with_rerank_mock):
        """When Voyage fails and Cohere is used, metadata shows cohere as provider."""
        from code_indexer.server.mcp.handlers._legacy import scip_references

        ref = _make_reference()
        scip_svc_with_rerank_mock.find_references.return_value = [ref]
        cohere_meta = _default_rerank_meta(used=True, provider="cohere", time_ms=20)

        with patch(
            "code_indexer.server.mcp.reranking._apply_reranking_sync",
            return_value=([ref], cohere_meta),
        ):
            args = {
                "symbol": "UserService",
                "rerank_query": "production instantiation of UserService",
                "limit": 5,
            }
            response = scip_references(args, _fake_user())

        payload = json.loads(response["content"][0]["text"])
        qm = payload["query_metadata"]
        assert qm["reranker_used"] is True
        assert qm["reranker_provider"] == "cohere"
        assert qm["rerank_time_ms"] == 20
