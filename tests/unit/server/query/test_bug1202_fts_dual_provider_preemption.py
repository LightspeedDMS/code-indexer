"""
Tests for Bug #1202: search_mode=fts/hybrid silently ignored on dual-provider repos.

Root cause: In _search_single_repository, when query_strategy is None and
_both_providers_configured() returns True, query_strategy is unconditionally set
to "parallel" WITHOUT checking search_mode. The parallel RRF fusion block then
returns early (before the FTS branch at line ~1563 is reached), so fts/hybrid
requests silently run parallel semantic fusion instead.

Fix: Gate the auto-parallel default on search_mode == "semantic" so fts/hybrid
requests fall through to the FTS branch.

Strategy: mock ONLY the external leaf services (TantivyIndexManager,
SemanticSearchService, EmbeddingProviderFactory.get_configured_providers) so the
real routing logic in _search_single_repository executes. Never mock SUT internal
routing helpers.
"""

import json
import logging
import shutil
import tempfile
from contextlib import contextmanager as _cm
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, cast
from unittest.mock import MagicMock, patch


from code_indexer.server.query.semantic_query_manager import (
    SemanticQueryManager,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_manager() -> SemanticQueryManager:
    """Build SemanticQueryManager with infrastructure mocked but routing real."""
    manager = SemanticQueryManager.__new__(SemanticQueryManager)
    manager.data_dir = "/fake/data"
    manager.query_timeout_seconds = 30
    manager.max_concurrent_queries_per_user = 5
    manager.max_results_per_query = 100
    manager._active_queries_per_user = {}
    manager.logger = logging.getLogger(__name__)
    mock_arm = MagicMock()
    mock_arm.activated_repos_dir = "/fake/data/activated_repos"
    manager.activated_repo_manager = mock_arm
    manager.background_job_manager = MagicMock()
    return manager


def _setup_fts_index(repo_path_str: str) -> None:
    """Create the tantivy_index directory so _execute_fts_search passes the existence check."""
    from pathlib import Path

    fts_dir = Path(repo_path_str) / ".code-indexer" / "tantivy_index"
    fts_dir.mkdir(parents=True, exist_ok=True)


def _fts_hit(path: str = "src/auth.py", match: str = "authenticate") -> dict:
    """One raw hit as TantivyIndexManager.search() returns it."""
    return {
        "path": path,
        "line": 10,
        "snippet": f"def {match}(): pass",
        "match_text": match,
        "language": "python",
    }


def _semantic_response(file_path: str = "src/api.py", score: float = 0.85):
    """One-result SemanticSearchResponse."""
    from code_indexer.server.models.api_models import (
        SemanticSearchResponse,
        SearchResultItem,
    )

    return SemanticSearchResponse(
        query="auth",
        results=[
            SearchResultItem(
                file_path=file_path,
                line_start=5,
                line_end=6,
                score=score,
                content="class UserAuth: pass",
                language="python",
            )
        ],
        total=1,
    )


def _patch_dual_providers():
    """Make EmbeddingProviderFactory report both providers configured."""
    return patch(
        "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
        ".get_configured_providers",
        return_value=["voyage-ai", "cohere"],
    )


def _patch_single_provider():
    """Make EmbeddingProviderFactory report only voyage-ai."""
    return patch(
        "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
        ".get_configured_providers",
        return_value=["voyage-ai"],
    )


def _patch_tantivy(hits: list):
    """Patch TantivyIndexManager at its source module; return context manager + mock instance."""
    mock_instance = MagicMock()
    mock_instance.search.return_value = hits
    # TantivyIndexManager is imported lazily inside _execute_fts_search via
    # `from ...services.tantivy_index_manager import TantivyIndexManager`
    # so we patch at the source module.
    ctx = patch(
        "code_indexer.services.tantivy_index_manager.TantivyIndexManager",
        return_value=mock_instance,
    )
    return ctx, mock_instance


# ---------------------------------------------------------------------------
# AC1: fts/hybrid must NOT trigger parallel auto-default on dual-provider repos
# ---------------------------------------------------------------------------


class TestAC1_FtsNotPreemptedOnDualProviders:
    """
    AC1: When search_mode='fts' or 'hybrid' and query_strategy is None, the
    auto-parallel default must NOT fire even if both providers are configured.
    Real _search_single_repository routing runs; only leaf services are mocked.
    """

    def setup_method(self):
        self.repo_path = tempfile.mkdtemp()
        _setup_fts_index(self.repo_path)

    def teardown_method(self):
        shutil.rmtree(self.repo_path, ignore_errors=True)

    def test_fts_mode_reaches_tantivy_on_dual_provider_repo(self):
        """
        BUG REPRODUCTION: search_mode='fts' with dual providers configured.
        Before fix: parallel block returns early -- TantivyIndexManager.search never called.
        After fix: FTS branch runs -- TantivyIndexManager.search IS called.
        """
        manager = _make_manager()
        tantivy_ctx, mock_tantivy = _patch_tantivy([_fts_hit()])

        # SemanticSearchService is lazily imported inside methods via
        # `from ..services.search_service import SemanticSearchService`
        # so patch at the source module.
        with (
            _patch_dual_providers(),
            tantivy_ctx,
            patch(
                "code_indexer.server.services.search_service.SemanticSearchService"
            ) as MockSSS,
        ):
            results = manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="myrepo",
                query_text="authenticate",
                limit=10,
                min_score=None,
                file_extensions=None,
                search_mode="fts",
            )

        mock_tantivy.search.assert_called_once()
        MockSSS.assert_not_called()
        assert len(results) > 0
        for r in results:
            d = r.to_dict()
            assert "fusion_score" not in d, (
                f"FTS result must NOT have fusion_score (parallel path ran): {d}"
            )
            assert "contributing_providers" not in d, (
                f"FTS result must NOT have contributing_providers (parallel path ran): {d}"
            )

    def test_hybrid_mode_reaches_tantivy_on_dual_provider_repo(self):
        """
        BUG REPRODUCTION: search_mode='hybrid' with dual providers configured.
        Before fix: parallel block returns early -- Tantivy never called.
        After fix: FTS + single semantic both run.
        """
        manager = _make_manager()
        tantivy_ctx, mock_tantivy = _patch_tantivy([_fts_hit()])

        with (
            _patch_dual_providers(),
            tantivy_ctx,
            patch(
                "code_indexer.server.services.search_service.SemanticSearchService"
            ) as MockSSS,
        ):
            mock_sss_instance = MagicMock()
            MockSSS.return_value = mock_sss_instance
            mock_sss_instance.search_repository_path.return_value = _semantic_response()

            results = manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="myrepo",
                query_text="authenticate",
                limit=10,
                min_score=None,
                file_extensions=None,
                search_mode="hybrid",
            )

        mock_tantivy.search.assert_called_once()
        mock_sss_instance.search_repository_path.assert_called_once()
        assert len(results) > 0

    def test_semantic_mode_still_triggers_parallel_on_dual_provider(self):
        """
        Regression: search_mode='semantic' with dual providers still routes to
        parallel RRF fusion. Observable: both providers queried via
        search_repository_path_with_provider (two calls).
        """
        manager = _make_manager()

        with (
            _patch_dual_providers(),
            patch(
                "code_indexer.server.services.config_service.get_config_service"
            ) as mock_cfg_svc,
            patch(
                "code_indexer.server.services.search_service.SemanticSearchService"
            ) as MockSSS,
        ):
            mock_cfg = MagicMock()
            mock_cfg.query_orchestration = None
            mock_cfg_svc.return_value.get_config.return_value = mock_cfg

            mock_sss_instance = MagicMock()
            MockSSS.return_value = mock_sss_instance
            mock_sss_instance.search_repository_path_with_provider.return_value = (
                _semantic_response()
            )

            manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="myrepo",
                query_text="auth",
                limit=10,
                min_score=None,
                file_extensions=None,
                search_mode="semantic",
            )

        assert mock_sss_instance.search_repository_path_with_provider.call_count == 2, (
            f"semantic+dual must trigger parallel (2 provider calls), "
            f"got {mock_sss_instance.search_repository_path_with_provider.call_count}"
        )

    def test_fts_single_provider_routes_correctly(self):
        """Regression: single-provider repo with search_mode='fts' still routes to FTS."""
        manager = _make_manager()
        tantivy_ctx, mock_tantivy = _patch_tantivy([_fts_hit()])

        with (
            _patch_single_provider(),
            tantivy_ctx,
            patch(
                "code_indexer.server.services.search_service.SemanticSearchService"
            ) as MockSSS,
        ):
            results = manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="myrepo",
                query_text="authenticate",
                limit=10,
                min_score=None,
                file_extensions=None,
                search_mode="fts",
            )

        mock_tantivy.search.assert_called_once()
        MockSSS.assert_not_called()
        assert len(results) > 0


# ---------------------------------------------------------------------------
# AC2: snippet_lines forwarded to Tantivy on both single- and dual-provider repos
# ---------------------------------------------------------------------------


class TestAC2_SnippetLinesForwarded:
    """
    AC2: snippet_lines must reach TantivyIndexManager.search on both
    single-provider and dual-provider repos with search_mode='fts'.
    """

    def setup_method(self):
        self.repo_path = tempfile.mkdtemp()
        _setup_fts_index(self.repo_path)

    def teardown_method(self):
        shutil.rmtree(self.repo_path, ignore_errors=True)

    def test_snippet_lines_forwarded_single_provider(self):
        """snippet_lines forwarded on single-provider fts path."""
        manager = _make_manager()
        tantivy_ctx, mock_tantivy = _patch_tantivy([])

        with _patch_single_provider(), tantivy_ctx:
            manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="myrepo",
                query_text="authenticate",
                limit=10,
                min_score=None,
                file_extensions=None,
                search_mode="fts",
                snippet_lines=25,
            )

        call_kwargs = mock_tantivy.search.call_args.kwargs
        assert call_kwargs.get("snippet_lines") == 25, (
            f"Expected snippet_lines=25, got {call_kwargs.get('snippet_lines')}"
        )

    def test_snippet_lines_forwarded_dual_provider_fts(self):
        """
        After fix: snippet_lines forwarded on dual-provider fts.
        Before fix: parallel block returned early without calling Tantivy.
        """
        manager = _make_manager()
        tantivy_ctx, mock_tantivy = _patch_tantivy([])

        with _patch_dual_providers(), tantivy_ctx:
            manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="myrepo",
                query_text="authenticate",
                limit=10,
                min_score=None,
                file_extensions=None,
                search_mode="fts",
                snippet_lines=50,
            )

        mock_tantivy.search.assert_called_once()
        call_kwargs = mock_tantivy.search.call_args.kwargs
        assert call_kwargs.get("snippet_lines") == 50, (
            f"snippet_lines not forwarded on dual-provider fts: "
            f"got {call_kwargs.get('snippet_lines')}"
        )

    def test_snippet_lines_zero_list_only_mode(self):
        """snippet_lines=0 (list-only) forwarded correctly on dual-provider fts."""
        manager = _make_manager()
        tantivy_ctx, mock_tantivy = _patch_tantivy([])

        with _patch_dual_providers(), tantivy_ctx:
            manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="myrepo",
                query_text="authenticate",
                limit=10,
                min_score=None,
                file_extensions=None,
                search_mode="fts",
                snippet_lines=0,
            )

        call_kwargs = mock_tantivy.search.call_args.kwargs
        assert call_kwargs.get("snippet_lines") == 0


# ---------------------------------------------------------------------------
# AC3: Dual-provider guard -- FTS results must not carry fusion metadata
# ---------------------------------------------------------------------------


class TestAC3_DualProviderFtsResultShape:
    """
    AC3: With dual providers configured and search_mode='fts', results must
    NOT carry fusion metadata (fusion_score, contributing_providers) and
    match_text MUST be present.

    BLOCKER 2 fix: patches _both_providers_configured directly to return True
    so the dual-provider condition is always exercised -- no factory-call-count
    gate. This test MUST always run and assert, never skip.
    """

    def setup_method(self):
        self.repo_path = tempfile.mkdtemp()
        _setup_fts_index(self.repo_path)

    def teardown_method(self):
        shutil.rmtree(self.repo_path, ignore_errors=True)

    def test_fts_results_have_no_fusion_metadata_on_dual_provider_repo(self):
        """
        DUAL-PROVIDER GUARD: With both providers configured, fts results
        must not carry parallel fusion metadata.

        Patches _both_providers_configured to return True so the dual-provider
        condition is always exercised without depending on factory call-count.
        Must FAIL if AC1 fix is reverted (parallel gate fires -> no FTS results).
        """
        manager = _make_manager()
        tantivy_ctx, mock_tantivy = _patch_tantivy(
            [
                _fts_hit("src/auth.py", "authenticate"),
                _fts_hit("src/login.py", "login_user"),
            ]
        )

        # Patch _both_providers_configured directly so this test always exercises
        # the dual-provider path without relying on factory call-count.
        with (
            patch.object(
                manager,
                "_both_providers_configured",
                return_value=True,
            ),
            tantivy_ctx,
        ):
            results = manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="myrepo",
                query_text="authenticate",
                limit=10,
                min_score=None,
                file_extensions=None,
                search_mode="fts",
            )

        assert len(results) > 0, "Expected FTS results, got none"

        for r in results:
            d = r.to_dict()
            assert "fusion_score" not in d, (
                f"FTS result must NOT have fusion_score (parallel path ran): {d}"
            )
            assert "contributing_providers" not in d, (
                f"FTS result must NOT have contributing_providers (parallel path ran): {d}"
            )
            assert "match_text" in d, (
                f"FTS result must have match_text. Got keys: {list(d.keys())}"
            )

    def test_rest_fts_path_is_independent_of_semantic_query_manager(self):
        """
        REST-UNTOUCHED GUARD: The REST /api/query FTS path in inline_query.py
        goes directly to TantivyIndexManager without calling
        SemanticQueryManager._search_single_repository at all.

        This test verifies the REST route imports TantivyIndexManager directly
        and does not re-route through the buggy chokepoint, so a future refactor
        cannot silently regress REST FTS by pulling it through the routing gate.
        """
        import inspect
        from code_indexer.server.routers import inline_query

        source = inspect.getsource(inline_query)
        # REST path must call TantivyIndexManager directly
        assert "TantivyIndexManager" in source, (
            "inline_query.py must use TantivyIndexManager directly for FTS; "
            "it must not re-route through SemanticQueryManager._search_single_repository"
        )
        # REST path must NOT delegate to SemanticQueryManager._search_single_repository
        assert "_search_single_repository" not in source, (
            "inline_query.py must NOT call _search_single_repository -- "
            "the REST FTS path must remain independent of the MCP routing gate"
        )


# ---------------------------------------------------------------------------
# AC7: effective_search_mode / effective_query_strategy in query_metadata
# ---------------------------------------------------------------------------


class TestAC7_EffectiveModeEcho:
    """
    AC7: query_user_repositories must include effective_search_mode and
    effective_query_strategy in the returned query_metadata dict.

    These values are threaded through per-request via QueryMetadata (no singleton
    state on the manager). The MCP handler reads them from result["query_metadata"]
    directly -- no read-back from manager attributes.
    """

    def setup_method(self):
        self.repo_path = tempfile.mkdtemp()
        _setup_fts_index(self.repo_path)

    def teardown_method(self):
        shutil.rmtree(self.repo_path, ignore_errors=True)

    def test_no_singleton_state_on_manager_after_fts(self):
        """
        BLOCKER 1 regression guard: _search_single_repository must NOT write
        _last_effective_search_mode or _last_effective_query_strategy on the manager.
        These were singleton attributes that caused cross-request data leaks.
        """
        manager = _make_manager()
        tantivy_ctx, _mock_tantivy = _patch_tantivy([_fts_hit()])

        with (
            patch.object(manager, "_both_providers_configured", return_value=True),
            tantivy_ctx,
        ):
            manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="myrepo",
                query_text="authenticate",
                limit=10,
                min_score=None,
                file_extensions=None,
                search_mode="fts",
            )

        assert not hasattr(manager, "_last_effective_search_mode"), (
            "Manager must NOT have _last_effective_search_mode -- "
            "singleton state causes cross-request data leaks under concurrent load"
        )
        assert not hasattr(manager, "_last_effective_query_strategy"), (
            "Manager must NOT have _last_effective_query_strategy -- "
            "singleton state causes cross-request data leaks under concurrent load"
        )

    def test_query_user_repositories_includes_effective_fields_fts(self):
        """
        query_user_repositories must return effective_search_mode='fts' and
        effective_query_strategy='primary_only' in query_metadata for an fts
        request on a dual-provider repo.
        """
        manager = _make_manager()
        tantivy_ctx, _mock_tantivy = _patch_tantivy([_fts_hit()])

        mock_arm = manager.activated_repo_manager
        mock_arm.list_activated_repositories.return_value = [
            {"user_alias": "myrepo", "repo_path": self.repo_path}
        ]

        with (
            patch.object(manager, "_both_providers_configured", return_value=True),
            tantivy_ctx,
        ):
            result = manager.query_user_repositories(
                username="testuser",
                query_text="authenticate",
                repository_alias="myrepo",
                limit=10,
                search_mode="fts",
            )

        qm = result.get("query_metadata", {})
        assert qm.get("effective_search_mode") == "fts", (
            f"Expected effective_search_mode='fts', got {qm.get('effective_search_mode')!r}"
        )
        assert qm.get("effective_query_strategy") == "primary_only", (
            f"Expected effective_query_strategy='primary_only' for fts+dual-provider, "
            f"got {qm.get('effective_query_strategy')!r}"
        )

    def test_query_user_repositories_includes_effective_fields_semantic_parallel(self):
        """
        For semantic mode + dual providers, effective_query_strategy='parallel'
        and effective_search_mode='semantic' must appear in query_metadata.
        """
        manager = _make_manager()

        mock_arm = manager.activated_repo_manager
        mock_arm.list_activated_repositories.return_value = [
            {"user_alias": "myrepo", "repo_path": self.repo_path}
        ]

        with (
            patch.object(manager, "_both_providers_configured", return_value=True),
            patch(
                "code_indexer.server.services.config_service.get_config_service"
            ) as mock_cfg_svc,
            patch(
                "code_indexer.server.services.search_service.SemanticSearchService"
            ) as MockSSS,
        ):
            mock_cfg = MagicMock()
            mock_cfg.query_orchestration = None
            mock_cfg_svc.return_value.get_config.return_value = mock_cfg

            mock_sss_instance = MagicMock()
            MockSSS.return_value = mock_sss_instance
            mock_sss_instance.search_repository_path_with_provider.return_value = (
                _semantic_response()
            )

            result = manager.query_user_repositories(
                username="testuser",
                query_text="auth",
                repository_alias="myrepo",
                limit=10,
                search_mode="semantic",
            )

        qm = result.get("query_metadata", {})
        assert qm.get("effective_search_mode") == "semantic", (
            f"Expected effective_search_mode='semantic', got {qm.get('effective_search_mode')!r}"
        )
        assert qm.get("effective_query_strategy") == "parallel", (
            f"Expected effective_query_strategy='parallel' for semantic+dual, "
            f"got {qm.get('effective_query_strategy')!r}"
        )

    def test_mcp_query_metadata_includes_effective_fields(self):
        """
        MCP search_code response query_metadata must include
        effective_search_mode and effective_query_strategy after the fix.

        effective_* arrive via QueryMetadata.to_dict() in query_user_repositories,
        not from singleton manager attributes. The qur_return dict already
        contains the fields in query_metadata -- no handler read-back needed.
        """
        from code_indexer.server.auth.user_manager import User, UserRole

        user = User(
            username="testuser",
            role=UserRole.ADMIN,
            email="test@example.com",
            password_hash="fakehash",
            created_at=datetime.now(timezone.utc),
        )

        # Simulate the response that query_user_repositories returns after the fix:
        # effective_* are already in query_metadata (from QueryMetadata.to_dict).
        qur_return = {
            "results": [
                {
                    "file_path": "src/auth.py",
                    "line_number": 10,
                    "code_snippet": "def authenticate(): pass",
                    "similarity_score": 0.95,
                    "repository_alias": "myrepo",
                    "source_provider": "fts",
                    "match_text": "authenticate",
                    "source_repo": None,
                }
            ],
            "total_results": 1,
            "query_metadata": {
                "query_text": "authenticate",
                "execution_time_ms": 5,
                "repositories_searched": 1,
                "timeout_occurred": False,
                # AC7: these fields now come from QueryMetadata.to_dict()
                "effective_search_mode": "fts",
                "effective_query_strategy": "primary_only",
            },
        }

        with patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app:
            mock_app.semantic_query_manager.query_user_repositories.return_value = (
                qur_return
            )
            mock_app.app.state = SimpleNamespace(payload_cache=None)
            mock_app.activated_repo_manager = MagicMock()

            from code_indexer.server.mcp.handlers import search_code

            _fake_rerank_meta = {
                "reranker_used": False,
                "reranker_provider": None,
                "rerank_time_ms": 0,
                "reranker_status": {"status": "disabled"},
            }
            with (
                patch(
                    "code_indexer.server.mcp.handlers.search._apply_rerank_and_filter",
                    return_value=(qur_return["results"], _fake_rerank_meta),
                ),
                patch(
                    "code_indexer.server.mcp.handlers.search._run_memory_retrieval",
                    return_value=None,
                ),
                patch(
                    "code_indexer.server.mcp.handlers.search._load_category_map",
                    return_value={},
                ),
                patch(
                    "code_indexer.server.mcp.handlers.search.get_config_service"
                ) as mock_cfg_svc,
            ):
                mock_mem_cfg = MagicMock()
                mock_mem_cfg.memory_retrieval_enabled = False
                mock_mem_cfg_obj = MagicMock()
                mock_mem_cfg_obj.memory_retrieval_config = mock_mem_cfg
                mock_cfg_svc.return_value.get_config.return_value = mock_mem_cfg_obj
                response = search_code(
                    {"query_text": "authenticate", "search_mode": "fts", "limit": 5},
                    user,
                )

        # Parse MCP response.
        # _mcp_response returns {"content": [{"type": "text", "text": "<json>"}]}
        if isinstance(response, dict) and "content" in response:
            content_list = response["content"]
            text_content = next(
                (c.get("text", "") for c in content_list if isinstance(c, dict)), ""
            )
            data = json.loads(text_content) if text_content else {}
        elif isinstance(response, list):
            text_content = next(
                (c.get("text", "") for c in response if isinstance(c, dict)), ""
            )
            data = json.loads(text_content) if text_content else {}
        elif isinstance(response, str):
            data = json.loads(response)
        else:
            data = {}

        results_section = data.get("results", data)
        qm = results_section.get("query_metadata", {})
        assert "effective_search_mode" in qm, (
            f"query_metadata must include effective_search_mode. Keys: {list(qm.keys())}"
        )
        assert "effective_query_strategy" in qm, (
            f"query_metadata must include effective_query_strategy. Keys: {list(qm.keys())}"
        )
        assert qm["effective_search_mode"] == "fts"
        assert qm["effective_query_strategy"] == "primary_only"

    def test_explicit_parallel_strategy_with_fts_mode_is_observable(self):
        """
        AC7 VISIBILITY: When query_strategy='parallel' is explicitly passed with
        search_mode='fts', the response query_metadata must reflect what actually
        ran -- making the silent override observable.

        (An explicit query_strategy='parallel' bypasses the auto-gate and forces
        parallel fusion even for fts mode. The effective fields document this.)
        """
        manager = _make_manager()

        mock_arm = manager.activated_repo_manager
        mock_arm.list_activated_repositories.return_value = [
            {"user_alias": "myrepo", "repo_path": self.repo_path}
        ]

        with (
            patch.object(manager, "_both_providers_configured", return_value=True),
            patch(
                "code_indexer.server.services.config_service.get_config_service"
            ) as mock_cfg_svc,
            patch(
                "code_indexer.server.services.search_service.SemanticSearchService"
            ) as MockSSS,
        ):
            mock_cfg = MagicMock()
            mock_cfg.query_orchestration = None
            mock_cfg_svc.return_value.get_config.return_value = mock_cfg

            mock_sss_instance = MagicMock()
            MockSSS.return_value = mock_sss_instance
            mock_sss_instance.search_repository_path_with_provider.return_value = (
                _semantic_response()
            )

            result = manager.query_user_repositories(
                username="testuser",
                query_text="auth",
                repository_alias="myrepo",
                limit=10,
                search_mode="fts",
                query_strategy="parallel",  # explicit override
            )

        qm = result.get("query_metadata", {})
        # With explicit query_strategy='parallel', the routing gate is bypassed
        # (the gate only fires when query_strategy is None).  effective_query_strategy
        # must reflect the explicitly-requested strategy.
        assert qm.get("effective_query_strategy") == "parallel", (
            f"Explicit query_strategy='parallel' must be reflected in effective_query_strategy, "
            f"got {qm.get('effective_query_strategy')!r}"
        )
        assert qm.get("effective_search_mode") == "fts", (
            f"effective_search_mode must echo the requested search_mode='fts', "
            f"got {qm.get('effective_search_mode')!r}"
        )


# ---------------------------------------------------------------------------
# AC4: preview_size_chars / rows_capped must reach query_metadata on
#       single-repo (activated) search_code calls
# ---------------------------------------------------------------------------


def _ac4_admin_user():
    """Minimal admin User for AC4 handler tests."""
    from datetime import datetime, timezone

    from code_indexer.server.auth.user_manager import User, UserRole

    return User(
        username="testuser",
        role=UserRole.ADMIN,
        email="test@example.com",
        password_hash="fakehash",
        created_at=datetime.now(timezone.utc),
    )


def _ac4_qur_return():
    """Minimal query_user_repositories return value for a single-repo fts call."""
    return {
        "results": [
            {
                "file_path": "src/auth.py",
                "line_number": 10,
                "code_snippet": "def authenticate(): pass",
                "similarity_score": 0.95,
                "repository_alias": "myrepo",
                "source_provider": "fts",
                "match_text": "authenticate",
                "source_repo": None,
            }
        ],
        "total_results": 1,
        "query_metadata": {
            "query_text": "authenticate",
            "execution_time_ms": 5,
            "repositories_searched": 1,
            "timeout_occurred": False,
            "effective_search_mode": "fts",
            "effective_query_strategy": "primary_only",
        },
    }


def _parse_mcp_qm(response: object) -> Dict[str, Any]:
    """Extract query_metadata from an MCP response dict/list/str."""
    raw: Dict[str, Any]
    if isinstance(response, dict) and "content" in response:
        content_list = response["content"]
        text = next(
            (c.get("text", "") for c in content_list if isinstance(c, dict)), ""
        )
        raw = cast(Dict[str, Any], json.loads(text) if text else {})
    elif isinstance(response, list):
        text = next((c.get("text", "") for c in response if isinstance(c, dict)), "")
        raw = cast(Dict[str, Any], json.loads(text) if text else {})
    elif isinstance(response, str):
        raw = cast(Dict[str, Any], json.loads(response))
    else:
        raw = {}
    results = cast(Dict[str, Any], raw.get("results", raw))
    return cast(Dict[str, Any], results.get("query_metadata", {}))


@_cm
def _ac4_patch_context(qur_return, truncation_meta):
    """
    Context manager that patches all external leaves for AC4 single-repo tests.

    Mocked leaves (not SUT internals):
      - app_module           : server app state / semantic_query_manager
      - _apply_search_truncation : leaf that produces (results, fts_meta) tuple
      - _mcp_reranking._apply_reranking_sync : reranker leaf (no reranking)
      - _run_memory_retrieval: memory-retrieval leaf (disabled)
      - _load_category_map   : category enrichment leaf (empty)
      - get_config_service   : config leaf (memory retrieval disabled)

    The real _apply_rerank_and_filter runs so its AC4 merge logic is exercised.
    """
    _base_rerank_meta = {
        "reranker_used": False,
        "reranker_provider": None,
        "rerank_time_ms": 0,
        "reranker_status": {"status": "disabled"},
    }
    mock_mem_cfg = MagicMock()
    mock_mem_cfg.memory_retrieval_enabled = False
    mock_cfg_obj = MagicMock()
    mock_cfg_obj.memory_retrieval_config = mock_mem_cfg

    with patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app:
        mock_app.semantic_query_manager.query_user_repositories.return_value = (
            qur_return
        )
        mock_app.app.state = SimpleNamespace(payload_cache=None)
        mock_app.activated_repo_manager = MagicMock()
        with (
            patch(
                "code_indexer.server.mcp.handlers.search._apply_search_truncation",
                return_value=(qur_return["results"], truncation_meta),
            ),
            patch(
                "code_indexer.server.mcp.handlers.search._mcp_reranking"
                "._apply_reranking_sync",
                return_value=(qur_return["results"], _base_rerank_meta),
            ),
            patch(
                "code_indexer.server.mcp.handlers.search._run_memory_retrieval",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers.search._load_category_map",
                return_value={},
            ),
            patch(
                "code_indexer.server.mcp.handlers.search.get_config_service"
            ) as mock_cfg_svc,
        ):
            mock_cfg_svc.return_value.get_config.return_value = mock_cfg_obj
            yield


class TestAC4_SingleRepoMetaDelivery:
    """
    AC4 FUNCTIONAL (Bug #1202): _search_activated_repo must propagate
    preview_size_chars and rows_capped from rerank_meta into qm.

    _apply_rerank_and_filter merges AC4 keys into rerank_meta via
    rerank_meta.update(fts_truncation_meta).  But _search_activated_repo only
    copies NAMED keys (reranker_used/provider/time_ms) into qm -- the AC4 fields
    are silently dropped on the single-repo path without an explicit propagation step.

    Test FAILS before the _search_activated_repo fix and PASSES after.
    """

    def setup_method(self):
        self.repo_path = tempfile.mkdtemp()
        _setup_fts_index(self.repo_path)

    def teardown_method(self):
        shutil.rmtree(self.repo_path, ignore_errors=True)

    def test_single_repo_fts_delivers_preview_size_and_rows_capped(self):
        """preview_size_chars and rows_capped must appear in query_metadata."""
        from code_indexer.server.mcp.handlers import search_code

        qur_return = _ac4_qur_return()
        truncation_meta = {"preview_size_chars": 2000, "rows_capped": 1}

        with _ac4_patch_context(qur_return, truncation_meta):
            response = search_code(
                {
                    "query_text": "authenticate",
                    "search_mode": "fts",
                    "limit": 5,
                    "repository_alias": "myrepo",
                },
                _ac4_admin_user(),
            )

        qm = _parse_mcp_qm(response)
        assert "preview_size_chars" in qm, (
            f"AC4: query_metadata missing preview_size_chars on single-repo fts. "
            f"Keys present: {list(qm.keys())}"
        )
        assert "rows_capped" in qm, (
            f"AC4: query_metadata missing rows_capped on single-repo fts. "
            f"Keys present: {list(qm.keys())}"
        )
        assert qm["preview_size_chars"] == 2000
        assert qm["rows_capped"] == 1
