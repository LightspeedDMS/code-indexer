"""Tests for Story #883 Phase C — shared Voyage vector (zero duplicate API calls).

Declared scenarios (exactly 6):
  1. search_repository_path accepts precomputed_query_vector and THREADS it to
     _perform_semantic_search (asserted via spy on _perform_semantic_search)
  2. query_user_repositories signature accepts precomputed_query_vector at END
  3. _search_activated_repo passes precomputed vector to query_user_repositories
  4. _search_activated_repo passes the SAME vector to get_memory_candidates
  5a. _run_memory_retrieval signature includes query_vector parameter
  5b. _run_memory_retrieval does NOT call _compute_memory_query_vector when query_vector supplied

TDD: tests are written BEFORE the Phase C implementation.

Design under test:
  - SemanticSearchService.search_repository_path gains optional
    `precomputed_query_vector: Optional[List[float]] = None` and forwards it to
    _perform_semantic_search (already implemented there).
  - SemanticQueryManager.query_user_repositories gains optional
    `precomputed_query_vector: Optional[List[float]] = None` at END of signature.
  - _search_activated_repo computes the vector ONCE (via _compute_shared_query_vector)
    and passes it to both query_user_repositories AND _run_memory_retrieval.
  - _run_memory_retrieval accepts `query_vector: List[float]` and does NOT call
    _compute_memory_query_vector internally.

External dependencies mocked:
  - For search_repository_path: spy on _perform_semantic_search
  - For handler tests: app_module, config_service, MemoryRetrievalPipeline, golden_repos_dir
"""

import inspect
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Test 1: search_repository_path THREADS precomputed_query_vector to _perform_semantic_search
# ---------------------------------------------------------------------------


class TestSearchRepositoryPathPrecomputedVector:
    """Verify search_repository_path threads precomputed_query_vector to _perform_semantic_search.

    Scenario 1: the vector must reach _perform_semantic_search, not just be accepted
    at the surface. We spy on _perform_semantic_search to capture call args.
    """

    def test_search_repository_path_threads_precomputed_vector_to_perform_semantic_search(
        self, tmp_path
    ):
        """Scenario 1: search_repository_path must forward precomputed_query_vector.

        Asserts:
        - search_repository_path accepts precomputed_query_vector without TypeError
        - _perform_semantic_search is called with the exact precomputed_query_vector value
        """
        precomputed = [0.1, 0.2, 0.3, 0.4]

        from code_indexer.server.models.api_models import SemanticSearchRequest
        from code_indexer.server.services.search_service import SemanticSearchService

        svc = SemanticSearchService()

        with patch.object(
            svc,
            "_perform_semantic_search",
            return_value=[],
        ) as mock_perform:
            svc.search_repository_path(
                repo_path=str(tmp_path),
                search_request=SemanticSearchRequest(query="auth logic", limit=5),
                precomputed_query_vector=precomputed,
            )

        mock_perform.assert_called_once()
        call_kwargs = mock_perform.call_args.kwargs
        assert call_kwargs.get("precomputed_query_vector") == precomputed, (
            f"_perform_semantic_search must receive precomputed_query_vector={precomputed}, "
            f"got {call_kwargs.get('precomputed_query_vector')}"
        )


# ---------------------------------------------------------------------------
# Test 2: query_user_repositories signature accepts precomputed_query_vector
# ---------------------------------------------------------------------------


class TestQueryUserRepositoriesSignature:
    """Verify query_user_repositories signature includes precomputed_query_vector.

    Scenario 2: structural test — parameter must exist, be optional (default None),
    and be at the END of the signature to preserve positional-arg compatibility.
    """

    def test_query_user_repositories_accepts_precomputed_query_vector_at_end(self):
        """Scenario 2: query_user_repositories must have precomputed_query_vector as LAST param.

        The parameter must:
        - Exist in the signature
        - Be optional with default value of None
        - Be at the END of the signature to preserve positional-arg compatibility
        """
        from code_indexer.server.query.semantic_query_manager import (
            SemanticQueryManager,
        )

        sig = inspect.signature(SemanticQueryManager.query_user_repositories)
        params = list(sig.parameters.keys())

        assert "precomputed_query_vector" in params, (
            "query_user_repositories must accept precomputed_query_vector parameter"
        )

        param = sig.parameters["precomputed_query_vector"]
        assert param.default is None, (
            "precomputed_query_vector must default to None (optional)"
        )

        assert params[-1] == "precomputed_query_vector", (
            "precomputed_query_vector must be the LAST parameter to preserve "
            "positional-arg compatibility with existing callers"
        )


# ---------------------------------------------------------------------------
# Shared helpers for handler integration tests (Scenarios 3-4, 5a-5b)
# ---------------------------------------------------------------------------

_PIPELINE_CLS_PATCH = "code_indexer.server.mcp.handlers.search.MemoryRetrievalPipeline"
_CONFIG_SVC_PATCH = "code_indexer.server.mcp.handlers.search.get_config_service"
_GOLDEN_DIR_PATCH = "code_indexer.server.mcp.handlers.search._get_golden_repos_dir"
_APP_MODULE_PATCH = "code_indexer.server.mcp.handlers._utils.app_module"
_COMPUTE_VECTOR_PATCH = (
    "code_indexer.server.mcp.handlers.search._compute_shared_query_vector"
)

_FAKE_GOLDEN_DIR = "/fake/golden-repos"
_EXPECTED_USERNAME = "gap-c-user"
_EXPECTED_QUERY = "auth logic"
_EXPECTED_LIMIT = 5
_EXPECTED_PRECOMPUTED_VECTOR = [0.11, 0.22, 0.33, 0.44]


def _make_mock_user():
    """Return a minimal User mock."""
    from code_indexer.server.auth.user_manager import User, UserRole

    user = MagicMock(spec=User)
    user.username = _EXPECTED_USERNAME
    user.role = UserRole.NORMAL_USER
    user.has_permission = MagicMock(return_value=True)
    return user


def _make_mock_config_service(enabled: bool = True):
    """Return a mock config service with MemoryRetrievalConfig."""
    from code_indexer.server.utils.config_manager import MemoryRetrievalConfig

    mem_cfg = MemoryRetrievalConfig(memory_retrieval_enabled=enabled)
    config = MagicMock()
    config.memory_retrieval_config = mem_cfg
    svc = MagicMock()
    svc.get_config.return_value = config
    return svc


def _make_activated_repo_result():
    """Minimal activated-repo result returned by query_user_repositories."""
    return {
        "results": [
            {"file_path": "src/foo.py", "content": "def foo(): pass", "score": 0.9}
        ],
        "total_results": 1,
        "query_metadata": {
            "query_text": _EXPECTED_QUERY,
            "execution_time_ms": 5,
            "repositories_searched": 1,
            "timeout_occurred": False,
        },
    }


def _build_pipeline_instance_stub():
    """Build a pipeline stub with all methods returning safe empty values."""
    pipeline_instance = MagicMock()
    pipeline_instance.get_memory_candidates.return_value = []
    pipeline_instance.apply_voyage_floor.return_value = []
    pipeline_instance.build_relevant_memories.return_value = []
    pipeline_instance.order_memory_items.return_value = []
    pipeline_instance.apply_cohere_floor.return_value = []
    return pipeline_instance


def _invoke_with_precomputed_vector_mock(pipeline_instance):
    """Run search_code() with _compute_shared_query_vector patched.

    Returns (result, mock_query_mgr) so callers can inspect call_args.
    """
    pipeline_cls = MagicMock()
    pipeline_cls.return_value = pipeline_instance
    mock_user = _make_mock_user()

    with (
        patch(_APP_MODULE_PATCH) as mock_app,
        patch(_CONFIG_SVC_PATCH, return_value=_make_mock_config_service(enabled=True)),
        patch(_GOLDEN_DIR_PATCH, return_value=_FAKE_GOLDEN_DIR),
        patch(_PIPELINE_CLS_PATCH, pipeline_cls),
        patch(
            _COMPUTE_VECTOR_PATCH,
            return_value=_EXPECTED_PRECOMPUTED_VECTOR,
        ),
    ):
        mock_app.app.state.access_filtering_service = None
        mock_app.app.state.payload_cache = None
        mock_app.golden_repo_manager._repo_category_service.get_repo_category_map.return_value = {}
        mock_app.semantic_query_manager.query_user_repositories.return_value = (
            _make_activated_repo_result()
        )

        from code_indexer.server.mcp.handlers import search_code

        result = search_code(
            {
                "query_text": _EXPECTED_QUERY,
                "search_mode": "semantic",
                "limit": _EXPECTED_LIMIT,
            },
            mock_user,
        )

    return result, mock_app.semantic_query_manager


# ---------------------------------------------------------------------------
# Tests 3-4: _search_activated_repo vector sharing
# ---------------------------------------------------------------------------


class TestSearchActivatedRepoVectorSharing:
    """Scenarios 3-4: _search_activated_repo computes ONE vector and shares it.

    Phase C mandate: exactly ONE Voyage API call per semantic request,
    regardless of whether memory retrieval runs.
    """

    def test_precomputed_vector_passed_to_query_user_repositories(self):
        """Scenario 3: _search_activated_repo must pass precomputed vector to query_user_repositories.

        The shared vector computed by _compute_shared_query_vector must arrive as
        precomputed_query_vector kwarg in query_user_repositories — not recomputed.
        """
        pipeline_instance = _build_pipeline_instance_stub()
        _, mock_query_mgr = _invoke_with_precomputed_vector_mock(pipeline_instance)

        call_kwargs = mock_query_mgr.query_user_repositories.call_args
        assert call_kwargs is not None, "query_user_repositories must be called"

        pqv = call_kwargs.kwargs.get("precomputed_query_vector")
        assert pqv == _EXPECTED_PRECOMPUTED_VECTOR, (
            f"query_user_repositories must receive precomputed_query_vector="
            f"{_EXPECTED_PRECOMPUTED_VECTOR}, got {pqv}"
        )

    def test_same_vector_passed_to_get_memory_candidates(self):
        """Scenario 4: the SAME precomputed vector must reach get_memory_candidates.

        Proves exactly ONE Voyage API call: the vector computed at handler level
        is reused for both code search and memory retrieval.
        """
        pipeline_instance = _build_pipeline_instance_stub()
        _, _ = _invoke_with_precomputed_vector_mock(pipeline_instance)

        call_kwargs = pipeline_instance.get_memory_candidates.call_args
        assert call_kwargs is not None, "get_memory_candidates must be called"

        qv = call_kwargs.kwargs.get("query_vector")
        assert qv == _EXPECTED_PRECOMPUTED_VECTOR, (
            f"get_memory_candidates must receive the same precomputed vector "
            f"{_EXPECTED_PRECOMPUTED_VECTOR}, got {qv}"
        )


# ---------------------------------------------------------------------------
# Tests 5a-5b: _run_memory_retrieval accepts query_vector, no internal compute
# ---------------------------------------------------------------------------


class TestRunMemoryRetrievalAcceptsQueryVector:
    """Scenarios 5a-5b: _run_memory_retrieval accepts query_vector and skips internal compute.

    5a: structural — signature must have query_vector parameter.
    5b: behavioral — _compute_memory_query_vector must NOT be called when vector supplied.
    """

    def test_run_memory_retrieval_signature_has_query_vector(self):
        """Scenario 5a: _run_memory_retrieval signature must include query_vector."""
        from code_indexer.server.mcp.handlers import search as search_module

        sig = inspect.signature(search_module._run_memory_retrieval)
        assert "query_vector" in sig.parameters, (
            "_run_memory_retrieval must accept query_vector parameter "
            "(Phase C: vector computed externally and passed in)"
        )

    def test_run_memory_retrieval_does_not_call_compute_when_query_vector_supplied(
        self,
    ):
        """Scenario 5b: _compute_memory_query_vector must NOT be called when query_vector given.

        Asserts:
        - _run_memory_retrieval accepts query_vector without TypeError
        - _compute_memory_query_vector is not called internally when vector is provided
        """
        from code_indexer.server.mcp.handlers import search as search_module
        from code_indexer.server.auth.user_manager import User

        user = MagicMock(spec=User)
        user.username = "test-user"

        config_svc = _make_mock_config_service(enabled=True)
        pipeline_instance = _build_pipeline_instance_stub()

        with (
            patch(_PIPELINE_CLS_PATCH, return_value=pipeline_instance),
            patch(_GOLDEN_DIR_PATCH, return_value=_FAKE_GOLDEN_DIR),
            patch(
                "code_indexer.server.mcp.handlers.search._compute_memory_query_vector"
            ) as mock_compute,
        ):
            params = {
                "query_text": "test query",
                "search_mode": "semantic",
                "limit": 5,
            }
            try:
                search_module._run_memory_retrieval(
                    params=params,
                    user=user,
                    config_service=config_svc,
                    reranker_status="disabled",
                    query_vector=_EXPECTED_PRECOMPUTED_VECTOR,
                )
            except TypeError as e:
                pytest.fail(
                    f"_run_memory_retrieval must accept query_vector parameter, "
                    f"got TypeError: {e}"
                )

            mock_compute.assert_not_called()
