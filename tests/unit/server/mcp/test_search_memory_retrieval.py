"""Tests for Story #883 — MemoryRetrievalPipeline.

Declared scenarios (exactly 10):
  1. Kill-switch: get_memory_candidates returns [] without calling retriever
  2. Kill-switch: build_relevant_memories returns [] (empty payload)
  3. FTS mode: get_memory_candidates returns [] without calling retriever
  4. Retriever called exactly once per semantic query
  5. Voyage floor excludes candidate below min_score
  6. Voyage floor keeps candidate at exact threshold
  7. Cohere floor excludes low rerank-score item
  8. Cohere floor not applied when reranker disabled
  9. Memories sorted by hnsw_score desc when reranker disabled
  10. Body truncated with marker

TDD: tests are written BEFORE the implementation.

External dependency mocked:
  - MemoryCandidateRetriever.retrieve — patched at the class boundary
    via `code_indexer.server.services.memory_candidate_retriever.MemoryCandidateRetriever.retrieve`
    (HNSW I/O boundary; the only patched dependency in this file)
"""

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.memory_candidate_retriever import MemoryCandidate

# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

_FAKE_STORE_PATH = "/fake/cidx-meta"
_FAKE_MEMORY_PATH_PREFIX = "/fake/cidx-meta/memories"
_RETRIEVER_PATCH_PATH = (
    "code_indexer.server.services.memory_candidate_retriever"
    ".MemoryCandidateRetriever.retrieve"
)
# FilesystemVectorStore is patched at the class boundary so MemoryCandidateRetriever
# can be instantiated without touching /fake on disk.  The MemoryRetrievalPipeline
# tests exercise only filter/floor/truncate logic; real disk I/O is the retriever's
# responsibility and is separately mocked via _RETRIEVER_PATCH_PATH.
_VECTOR_STORE_PATCH_PATH = (
    "code_indexer.server.services.memory_candidate_retriever.FilesystemVectorStore"
)


@pytest.fixture(autouse=True)
def _mock_filesystem_vector_store():
    """Prevent PermissionError on /fake by replacing FilesystemVectorStore with a MagicMock.

    MemoryCandidateRetriever calls FilesystemVectorStore(base_path=...) in __init__,
    which in turn calls base_path.mkdir().  Patching the class means the constructor
    returns a MagicMock instead of touching the real filesystem.
    """
    with patch(_VECTOR_STORE_PATCH_PATH, return_value=MagicMock()):
        yield


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_pipeline(
    enabled: bool = True,
    voyage_min_score: float = 0.5,
    cohere_min_score: float = 0.4,
    k_multiplier: int = 5,
    max_body_chars: int = 2000,
):
    """Construct a MemoryRetrievalPipeline with injected config values."""
    from code_indexer.server.mcp.memory_retrieval_pipeline import (
        MemoryRetrievalPipeline,
        MemoryRetrievalPipelineConfig,
    )

    cfg = MemoryRetrievalPipelineConfig(
        memory_retrieval_enabled=enabled,
        memory_voyage_min_score=voyage_min_score,
        memory_cohere_min_score=cohere_min_score,
        memory_retrieval_k_multiplier=k_multiplier,
        memory_retrieval_max_body_chars=max_body_chars,
    )
    return MemoryRetrievalPipeline(config=cfg, store_base_path=_FAKE_STORE_PATH)


def _make_candidate(memory_id: str, hnsw_score: float) -> MemoryCandidate:
    return MemoryCandidate(
        memory_id=memory_id,
        hnsw_score=hnsw_score,
        memory_path=f"{_FAKE_MEMORY_PATH_PREFIX}/{memory_id}.md",
        title=f"Title {memory_id}",
        summary=f"Summary {memory_id}",
    )


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestKillSwitch:
    """Scenarios 1-2: memory_retrieval_enabled=False."""

    def test_kill_switch_returns_empty_without_calling_retriever(self):
        """Scenario 1: kill-switch off → retriever never called, returns []."""
        pipeline = _make_pipeline(enabled=False)

        with patch(_RETRIEVER_PATCH_PATH, return_value=[]) as mock_retrieve:
            candidates = pipeline.get_memory_candidates(
                query_vector=[0.1, 0.2, 0.3],
                user_id="user-1",
                requested_limit=10,
                search_mode="semantic",
            )
            mock_retrieve.assert_not_called()

        assert candidates == []

    def test_kill_switch_build_relevant_memories_returns_empty(self):
        """Scenario 2: kill-switch off → build_relevant_memories returns []."""
        pipeline = _make_pipeline(enabled=False)

        result = pipeline.build_relevant_memories(
            memory_candidates=[],
            query="test query",
            config_service=None,
            reranker_status="disabled",
        )

        assert result == []


class TestFTSModeBypass:
    """Scenario 3: non-semantic search modes skip memory retrieval."""

    def test_fts_mode_skips_memory_retrieval(self):
        """Scenario 3: FTS mode → get_memory_candidates returns [] without retriever call."""
        pipeline = _make_pipeline(enabled=True)

        with patch(_RETRIEVER_PATCH_PATH, return_value=[]) as mock_retrieve:
            candidates = pipeline.get_memory_candidates(
                query_vector=[0.1, 0.2],
                user_id="user-2",
                requested_limit=5,
                search_mode="fts",
            )
            mock_retrieve.assert_not_called()

        assert candidates == []


class TestVoyageCalledOnce:
    """Scenario 4: retriever called exactly once per semantic query."""

    def test_retriever_called_exactly_once_per_semantic_query(self):
        """Scenario 4: semantic mode → retriever.retrieve called exactly once."""
        pipeline = _make_pipeline(enabled=True)

        with patch(_RETRIEVER_PATCH_PATH, return_value=[]) as mock_retrieve:
            pipeline.get_memory_candidates(
                query_vector=[0.5] * 8,
                user_id="user-3",
                requested_limit=10,
                search_mode="semantic",
            )
            assert mock_retrieve.call_count == 1


class TestVoyageFloorFilter:
    """Scenarios 5-6: Voyage HNSW score floor."""

    def test_voyage_floor_excludes_below_min_score(self):
        """Scenario 5: candidate with hnsw_score < voyage_min_score is excluded."""
        pipeline = _make_pipeline(enabled=True, voyage_min_score=0.5)
        low_score_candidate = _make_candidate("mem-low", hnsw_score=0.42)

        filtered = pipeline.apply_voyage_floor([low_score_candidate])

        assert filtered == []

    def test_voyage_floor_keeps_candidate_at_exact_threshold(self):
        """Scenario 6: candidate with hnsw_score == voyage_min_score is kept."""
        pipeline = _make_pipeline(enabled=True, voyage_min_score=0.5)
        exact_threshold_candidate = _make_candidate("mem-exact", hnsw_score=0.5)

        filtered = pipeline.apply_voyage_floor([exact_threshold_candidate])

        assert len(filtered) == 1
        assert filtered[0].memory_id == "mem-exact"


class TestCohereFloorFilter:
    """Scenarios 7-8: Cohere post-rerank floor."""

    def test_cohere_floor_excludes_low_rerank_score(self):
        """Scenario 7: memory item with rerank_score < cohere_min_score is excluded."""
        pipeline = _make_pipeline(enabled=True, cohere_min_score=0.4)
        pool_item = {
            "_source_tag": "memory",
            "memory_id": "mem-1",
            "rerank_score": 0.31,
        }

        filtered = pipeline.apply_cohere_floor(
            memory_pool_items=[pool_item], reranker_status="success"
        )

        assert filtered == []

    def test_cohere_floor_not_applied_when_reranker_disabled(self):
        """Scenario 8: reranker disabled → cohere floor not applied, item kept."""
        pipeline = _make_pipeline(enabled=True, cohere_min_score=0.4)
        pool_item = {
            "_source_tag": "memory",
            "memory_id": "mem-2",
            "rerank_score": 0.31,  # would be excluded if floor applied
        }

        filtered = pipeline.apply_cohere_floor(
            memory_pool_items=[pool_item], reranker_status="disabled"
        )

        assert len(filtered) == 1
        assert filtered[0]["memory_id"] == "mem-2"


class TestRerankerDisabledOrdering:
    """Scenario 9: when reranker disabled, sort by hnsw_score descending."""

    def test_reranker_disabled_orders_by_hnsw_score_desc(self):
        """Scenario 9: sort memory items by hnsw_score desc when reranker disabled."""
        pipeline = _make_pipeline(enabled=True)
        pool_items = [
            {"_source_tag": "memory", "memory_id": "mem-a", "hnsw_score": 0.61},
            {"_source_tag": "memory", "memory_id": "mem-b", "hnsw_score": 0.85},
            {"_source_tag": "memory", "memory_id": "mem-c", "hnsw_score": 0.73},
        ]

        ordered = pipeline.order_memory_items(
            memory_pool_items=pool_items, reranker_status="disabled"
        )

        assert [item["memory_id"] for item in ordered] == ["mem-b", "mem-c", "mem-a"]


class TestBodyTruncation:
    """Scenario 10: body truncated with [...truncated] marker."""

    def test_body_truncated_with_marker(self):
        """Scenario 10: text > max_body_chars is sliced and marker appended."""
        pipeline = _make_pipeline(enabled=True, max_body_chars=100)
        long_body = "x" * 200

        truncated = pipeline.truncate_body(long_body)

        assert truncated.startswith("x" * 100)
        assert truncated.endswith("[...truncated]")


# ---------------------------------------------------------------------------
# Handler integration tests (Scenarios A-C)
# ---------------------------------------------------------------------------
# These tests verify that search_code() is correctly wired to the memory
# retrieval pipeline (Messi Rule #12: wire it or don't write it).
#
# Design:
#   - _invoke_search_code_with_mocks: single shared helper taking only the
#     knowable test parameters; config_service_mock is passed directly so
#     each scenario controls its own config without an unused "enabled" flag.
#   - Scenario A uses TWO distinct sentinels (raw_candidates, filtered_candidates)
#     so apply_voyage_floor can be asserted with raw and build_relevant_memories
#     with filtered — proving exact inter-stage data handoff.
#   - Scenarios B/C assert the class was never instantiated.

_PIPELINE_CLS_PATCH = "code_indexer.server.mcp.handlers.search.MemoryRetrievalPipeline"
_CONFIG_SVC_PATCH = "code_indexer.server.mcp.handlers.search.get_config_service"
# _get_golden_repos_dir is imported directly into search.py's namespace via
# `from ._utils import ..., _get_golden_repos_dir, ...`, so patching the _utils
# module attribute would not affect the already-bound name in search.py.
# We must patch the name as bound in search.py instead.
_GOLDEN_DIR_PATCH = "code_indexer.server.mcp.handlers.search._get_golden_repos_dir"
_APP_MODULE_PATCH = "code_indexer.server.mcp.handlers._utils.app_module"

_FAKE_GOLDEN_DIR = "/fake/golden-repos"
_EXPECTED_STORE_PATH = _FAKE_GOLDEN_DIR + "/cidx-meta"
_EXPECTED_USERNAME = "test-user"
_EXPECTED_QUERY = "foo"
_EXPECTED_LIMIT = 5
_EXPECTED_SEARCH_MODE = "semantic"
# When no rerank_query param is passed, reranking is disabled; the handler
# must forward reranker_status="disabled" to build_relevant_memories.
_EXPECTED_RERANKER_STATUS = "disabled"


def _make_mock_user():
    """Return a minimal User mock for handler tests."""
    from code_indexer.server.auth.user_manager import User, UserRole

    user = MagicMock(spec=User)
    user.username = _EXPECTED_USERNAME
    user.role = UserRole.NORMAL_USER
    user.has_permission = MagicMock(return_value=True)
    return user


def _make_mock_config_service(enabled: bool = True):
    """Return a mock config service with a MemoryRetrievalConfig."""
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


def _extract_query_metadata(result: dict) -> dict:
    """Parse the MCP response envelope and return the inner query_metadata dict."""
    import json

    content = result.get("content", [])
    assert content, "Expected non-empty MCP response content"
    payload = json.loads(content[0]["text"])
    return dict(payload.get("results", {}).get("query_metadata", {}))


def _invoke_search_code_with_mocks(
    search_mode: str,
    config_service_mock: MagicMock,
    pipeline_cls_mock: MagicMock,
    pipeline_instance: MagicMock,
) -> dict:
    """Invoke search_code() with all external dependencies mocked.

    Sets pipeline_cls_mock.return_value = pipeline_instance so the handler
    gets the pre-built stub.  The caller is responsible for supplying a
    config_service_mock that reflects the desired memory_retrieval_enabled
    state — this keeps the helper parameter list unambiguous.

    Returns:
        The raw dict returned by search_code().
    """
    pipeline_cls_mock.return_value = pipeline_instance
    mock_user = _make_mock_user()

    with (
        patch(_APP_MODULE_PATCH) as mock_app,
        patch(_CONFIG_SVC_PATCH, return_value=config_service_mock),
        patch(_GOLDEN_DIR_PATCH, return_value=_FAKE_GOLDEN_DIR),
        patch(_PIPELINE_CLS_PATCH, pipeline_cls_mock),
    ):
        # Prevent _get_access_filtering_service() from returning a truthy MagicMock
        # (which would make filter_query_results() return a MagicMock that fails JSON
        # serialization).  Setting the attribute to None makes getattr(..., None) return
        # None, so access-filtering is skipped in _apply_rerank_and_filter.
        mock_app.app.state.access_filtering_service = None
        # Prevent _apply_payload_truncation from using the payload cache (which is a
        # MagicMock by default, causing truncate_result() to inject MagicMock values
        # for 'preview'/'cache_handle'/'total_size' into result dicts).  Setting to None
        # makes getattr(..., None) return None, so the cache-based path is skipped.
        mock_app.app.state.payload_cache = None
        # Prevent _load_category_map from returning MagicMock values that fail JSON
        # serialization.  Returning {} means no category enrichment, all dicts stay clean.
        mock_app.golden_repo_manager._repo_category_service.get_repo_category_map.return_value = {}
        mock_app.semantic_query_manager.query_user_repositories.return_value = (
            _make_activated_repo_result()
        )

        from code_indexer.server.mcp.handlers import search_code

        return dict(
            search_code(
                {
                    "query_text": _EXPECTED_QUERY,
                    "search_mode": search_mode,
                    "limit": _EXPECTED_LIMIT,
                },
                mock_user,
            )
        )


class TestHandlerMemoryRetrieval:
    """Scenarios A-C: search_code handler wired to MemoryRetrievalPipeline.

    _invoke_search_code_with_mocks eliminates repeated patch setup.
    Scenario A uses two distinct sentinels and assert_called_once_with() to
    verify the exact inter-stage data flow (raw → apply_voyage_floor → filtered
    → build_relevant_memories).  query_vector uses ANY (runtime-computed).
    Scenarios B/C assert the class was never instantiated.
    """

    def test_semantic_query_instantiates_pipeline_and_verifies_full_stage_data_flow(
        self,
    ):
        """Scenario A: full pipeline data-flow for a semantic query.

        Verifies:
        1. Constructor called with exact (config=<MemoryRetrievalPipelineConfig>, store_base_path)
        2. get_memory_candidates called with exact kwargs; returns raw_candidates sentinel
        3. apply_voyage_floor called with raw_candidates; returns filtered_candidates sentinel
        4. build_relevant_memories called with filtered_candidates (not raw) + exact other kwargs
        5. relevant_memories attached to query_metadata
        """
        from unittest.mock import ANY
        from code_indexer.server.mcp.memory_retrieval_pipeline import (
            MemoryRetrievalPipelineConfig,
        )
        from code_indexer.server.utils.config_manager import MemoryRetrievalConfig

        # Build the exact MemoryRetrievalPipelineConfig the handler will construct
        _defaults = MemoryRetrievalConfig()
        expected_pipeline_config = MemoryRetrievalPipelineConfig(
            memory_retrieval_enabled=True,
            memory_voyage_min_score=_defaults.memory_voyage_min_score,
            memory_cohere_min_score=_defaults.memory_cohere_min_score,
            memory_retrieval_k_multiplier=_defaults.memory_retrieval_k_multiplier,
            memory_retrieval_max_body_chars=_defaults.memory_retrieval_max_body_chars,
        )

        # Two distinct sentinels prove inter-stage data handoff:
        # raw_candidates: output of get_memory_candidates (pre-floor)
        # filtered_candidates: output of apply_voyage_floor (post-floor)
        raw_candidates = [object()]
        filtered_candidates = [object()]
        assert raw_candidates is not filtered_candidates, (
            "sentinels must be distinct objects"
        )

        pipeline_instance = MagicMock()
        pipeline_instance.get_memory_candidates.return_value = raw_candidates
        pipeline_instance.apply_voyage_floor.return_value = filtered_candidates
        pipeline_instance.build_relevant_memories.return_value = []
        # GAP 2: handler now calls order_memory_items and apply_cohere_floor;
        # stub both to return a list so _hydrate_memory_bodies receives a valid input.
        pipeline_instance.order_memory_items.return_value = []
        pipeline_instance.apply_cohere_floor.return_value = []
        pipeline_cls = MagicMock()

        cfg_svc = _make_mock_config_service(enabled=True)

        result = _invoke_search_code_with_mocks(
            search_mode=_EXPECTED_SEARCH_MODE,
            config_service_mock=cfg_svc,
            pipeline_cls_mock=pipeline_cls,
            pipeline_instance=pipeline_instance,
        )

        # 1. Constructor: exact config equality + exact store_base_path
        pipeline_cls.assert_called_once_with(
            config=expected_pipeline_config,
            store_base_path=_EXPECTED_STORE_PATH,
        )

        # 2. get_memory_candidates: exact kwargs; query_vector=ANY (runtime-computed)
        pipeline_instance.get_memory_candidates.assert_called_once_with(
            query_vector=ANY,
            user_id=_EXPECTED_USERNAME,
            requested_limit=_EXPECTED_LIMIT,
            search_mode=_EXPECTED_SEARCH_MODE,
        )

        # 3. apply_voyage_floor: called with the raw sentinel (pre-floor)
        pipeline_instance.apply_voyage_floor.assert_called_once_with(raw_candidates)

        # 4. build_relevant_memories: receives the filtered sentinel (post-floor),
        #    not the raw one; exact config_service reference; exact reranker_status
        pipeline_instance.build_relevant_memories.assert_called_once_with(
            memory_candidates=filtered_candidates,
            query=_EXPECTED_QUERY,
            config_service=cfg_svc,
            reranker_status=_EXPECTED_RERANKER_STATUS,
        )

        # 5. Response: relevant_memories attached
        qm = _extract_query_metadata(result)
        assert "relevant_memories" in qm, (
            "query_metadata must contain 'relevant_memories' for semantic/enabled queries"
        )

    def test_fts_mode_does_not_instantiate_pipeline(self):
        """Scenario B: FTS mode must not instantiate MemoryRetrievalPipeline."""
        pipeline_instance = MagicMock()
        pipeline_cls = MagicMock()

        result = _invoke_search_code_with_mocks(
            search_mode="fts",
            config_service_mock=_make_mock_config_service(enabled=True),
            pipeline_cls_mock=pipeline_cls,
            pipeline_instance=pipeline_instance,
        )

        pipeline_cls.assert_not_called()
        pipeline_instance.get_memory_candidates.assert_not_called()
        pipeline_instance.build_relevant_memories.assert_not_called()

        qm = _extract_query_metadata(result)
        assert "relevant_memories" not in qm

    def test_kill_switch_off_does_not_instantiate_pipeline(self):
        """Scenario C: memory_retrieval_enabled=False must not instantiate the pipeline."""
        pipeline_instance = MagicMock()
        pipeline_cls = MagicMock()

        result = _invoke_search_code_with_mocks(
            search_mode=_EXPECTED_SEARCH_MODE,
            config_service_mock=_make_mock_config_service(enabled=False),
            pipeline_cls_mock=pipeline_cls,
            pipeline_instance=pipeline_instance,
        )

        pipeline_cls.assert_not_called()
        pipeline_instance.get_memory_candidates.assert_not_called()
        pipeline_instance.build_relevant_memories.assert_not_called()

        qm = _extract_query_metadata(result)
        assert "relevant_memories" not in qm

    def test_query_vector_passed_to_get_memory_candidates_is_non_empty(self):
        """Scenario J (GAP 1): query_vector passed to get_memory_candidates must be a
        non-empty list — not the empty [] stub that crashes the retriever with ValueError.
        """
        pipeline_instance = MagicMock()
        pipeline_instance.get_memory_candidates.return_value = []
        pipeline_instance.apply_voyage_floor.return_value = []
        pipeline_instance.build_relevant_memories.return_value = []
        pipeline_instance.order_memory_items.return_value = []
        pipeline_instance.apply_cohere_floor.return_value = []
        pipeline_cls = MagicMock()

        _invoke_search_code_with_mocks(
            search_mode=_EXPECTED_SEARCH_MODE,
            config_service_mock=_make_mock_config_service(enabled=True),
            pipeline_cls_mock=pipeline_cls,
            pipeline_instance=pipeline_instance,
        )

        call_kwargs = pipeline_instance.get_memory_candidates.call_args
        assert call_kwargs is not None, "get_memory_candidates must be called"
        query_vector_arg = call_kwargs.kwargs.get("query_vector")
        assert query_vector_arg is not None, "query_vector kwarg must be present"
        assert isinstance(query_vector_arg, list), "query_vector must be a list"
        assert len(query_vector_arg) > 0, (
            "query_vector must be non-empty — the empty [] stub is not allowed"
        )


class TestOrderAndCohereWiring:
    """Scenarios D-E (GAP 2): order_memory_items and apply_cohere_floor wired in handler."""

    def test_order_memory_items_called_after_build_relevant_memories(self):
        """Scenario D: handler calls order_memory_items with build_relevant_memories output."""
        build_output = [{"memory_id": "m1", "hnsw_score": 0.7}]
        ordered_output = [{"memory_id": "m1", "hnsw_score": 0.7}]
        pipeline_instance = MagicMock()
        pipeline_instance.get_memory_candidates.return_value = []
        pipeline_instance.apply_voyage_floor.return_value = []
        pipeline_instance.build_relevant_memories.return_value = build_output
        pipeline_instance.order_memory_items.return_value = ordered_output
        pipeline_instance.apply_cohere_floor.return_value = ordered_output
        pipeline_cls = MagicMock()

        _invoke_search_code_with_mocks(
            search_mode=_EXPECTED_SEARCH_MODE,
            config_service_mock=_make_mock_config_service(enabled=True),
            pipeline_cls_mock=pipeline_cls,
            pipeline_instance=pipeline_instance,
        )

        pipeline_instance.order_memory_items.assert_called_once()
        call_args = pipeline_instance.order_memory_items.call_args
        assert call_args is not None
        actual_pool = (
            call_args.args[0]
            if call_args.args
            else call_args.kwargs.get("memory_pool_items")
        )
        assert actual_pool == build_output, (
            "order_memory_items must receive the output of build_relevant_memories"
        )

    def test_apply_cohere_floor_called_after_order_memory_items(self):
        """Scenario E: handler calls apply_cohere_floor with ordered output and reranker_status."""
        build_output = [{"memory_id": "m2", "hnsw_score": 0.8}]
        ordered_output = [{"memory_id": "m2", "hnsw_score": 0.8}]
        pipeline_instance = MagicMock()
        pipeline_instance.get_memory_candidates.return_value = []
        pipeline_instance.apply_voyage_floor.return_value = []
        pipeline_instance.build_relevant_memories.return_value = build_output
        pipeline_instance.order_memory_items.return_value = ordered_output
        pipeline_instance.apply_cohere_floor.return_value = ordered_output
        pipeline_cls = MagicMock()

        _invoke_search_code_with_mocks(
            search_mode=_EXPECTED_SEARCH_MODE,
            config_service_mock=_make_mock_config_service(enabled=True),
            pipeline_cls_mock=pipeline_cls,
            pipeline_instance=pipeline_instance,
        )

        pipeline_instance.apply_cohere_floor.assert_called_once()
        call_args = pipeline_instance.apply_cohere_floor.call_args
        actual_pool = (
            call_args.args[0]
            if call_args.args
            else call_args.kwargs.get("memory_pool_items")
        )
        assert actual_pool == ordered_output, (
            "apply_cohere_floor must receive the output of order_memory_items"
        )
        reranker_status_arg = (
            call_args.args[1]
            if len(call_args.args) > 1
            else call_args.kwargs.get("reranker_status")
        )
        assert reranker_status_arg == _EXPECTED_RERANKER_STATUS, (
            "apply_cohere_floor must receive the correct reranker_status"
        )


class TestNudgeInjection:
    """Scenarios F-G (GAP 3): empty-state nudge injected when pipeline returns no candidates."""

    def _invoke_with_empty_pipeline(self, search_mode: str, enabled: bool) -> dict:
        """Helper: run handler with pipeline always returning empty lists."""
        pipeline_instance = MagicMock()
        pipeline_instance.get_memory_candidates.return_value = []
        pipeline_instance.apply_voyage_floor.return_value = []
        pipeline_instance.build_relevant_memories.return_value = []
        pipeline_instance.order_memory_items.return_value = []
        pipeline_instance.apply_cohere_floor.return_value = []
        pipeline_cls = MagicMock()

        return _invoke_search_code_with_mocks(
            search_mode=search_mode,
            config_service_mock=_make_mock_config_service(enabled=enabled),
            pipeline_cls_mock=pipeline_cls,
            pipeline_instance=pipeline_instance,
        )

    def test_empty_candidates_inject_nudge_for_semantic_mode(self):
        """Scenario F: zero candidates + enabled + semantic → nudge in relevant_memories."""
        result = self._invoke_with_empty_pipeline(search_mode="semantic", enabled=True)

        qm = _extract_query_metadata(result)
        assert "relevant_memories" in qm, (
            "relevant_memories must be present even when pipeline returns empty"
        )
        memories = qm["relevant_memories"]
        assert isinstance(memories, list) and len(memories) == 1, (
            "exactly one nudge entry expected when pipeline returns empty"
        )
        nudge = memories[0]
        assert nudge.get("memory_id") == "__empty_nudge__", (
            "nudge entry must have memory_id == '__empty_nudge__'"
        )
        assert nudge.get("is_nudge") is True, "nudge entry must have is_nudge=True"
        assert isinstance(nudge.get("body"), str) and len(nudge["body"]) > 0, (
            "nudge body must be a non-empty string loaded from the .md file"
        )

    def test_nudge_not_injected_when_kill_switch_off(self):
        """Scenario G: kill-switch off → no pipeline call → no nudge → no relevant_memories key."""
        result = self._invoke_with_empty_pipeline(search_mode="semantic", enabled=False)

        qm = _extract_query_metadata(result)
        assert "relevant_memories" not in qm, (
            "relevant_memories must not appear when memory_retrieval_enabled=False"
        )


class TestBodyHydration:
    """Scenarios H-I (GAP 4): body hydration from read_memory_file and error skip."""

    def test_body_hydrated_from_read_memory_file_for_real_candidate(self):
        """Scenario H: a real memory_id candidate is hydrated via read_memory_file.

        _hydrate_memory_bodies must call read_memory_file for each non-nudge candidate
        and populate the returned dict's 'body' with the file content.
        """
        from unittest.mock import patch

        from code_indexer.server.mcp.memory_retrieval_pipeline import (
            _hydrate_memory_bodies,
        )

        candidate = {"memory_id": "real-id", "hnsw_score": 0.9}
        expected_body = "This is the real memory body text."

        def _fake_read(path):
            return ({"title": "Real Memory"}, expected_body, "deadbeef")

        with patch(
            "code_indexer.server.mcp.memory_retrieval_pipeline.read_memory_file",
            side_effect=_fake_read,
        ):
            result = _hydrate_memory_bodies(
                candidates=[candidate],
                store_base_path="/fake/store",
            )

        assert len(result) == 1
        assert result[0]["memory_id"] == "real-id"
        assert result[0]["body"] == expected_body, (
            "body must be populated from read_memory_file output"
        )

    def test_corrupt_memory_file_skipped_with_warning_log(self):
        """Scenario I: when read_memory_file raises, candidate is skipped and WARNING logged."""
        from unittest.mock import patch

        from code_indexer.server.mcp.memory_retrieval_pipeline import (
            _hydrate_memory_bodies,
        )

        good_candidate = {"memory_id": "good-id", "hnsw_score": 0.9}
        bad_candidate = {"memory_id": "bad-id", "hnsw_score": 0.85}

        def _fake_read(path):
            if "bad-id" in str(path):
                raise ValueError("Simulated corrupt file")
            return ({"title": "Good Memory"}, "good body text", "abc123")

        with patch(
            "code_indexer.server.mcp.memory_retrieval_pipeline.read_memory_file",
            side_effect=_fake_read,
        ):
            with patch(
                "code_indexer.server.mcp.memory_retrieval_pipeline.logger"
            ) as mock_logger:
                result = _hydrate_memory_bodies(
                    candidates=[good_candidate, bad_candidate],
                    store_base_path="/fake/store",
                )

        assert len(result) == 1, "corrupt candidate must be dropped"
        assert result[0]["memory_id"] == "good-id"
        assert result[0]["body"] == "good body text"

        mock_logger.warning.assert_called_once()
        warning_msg = mock_logger.warning.call_args[0][0]
        assert (
            "bad-id" in warning_msg
            or "corrupt" in warning_msg.lower()
            or "skip" in warning_msg.lower()
        ), "warning must mention the bad candidate or the skip action"
