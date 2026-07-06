"""Tests for Story #1290 recall pipeline: coalesce-before-truncate + dedup-by-commit.

Covers AC10 (dedup-by-commit pipeline order), AC11 (fail-loud full-message
reconstruction), AC12 (canonical chunk_type mapping), AC13 (filters + default
reverse-chronological order), AC14 (query embedding purpose/lane/cache key).
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.services.temporal.temporal_fusion import dedup_by_commit
from code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchResult,
    TemporalSearchService,
)


def make_result(
    commit_hash: str,
    score: float,
    is_head: bool,
    primary_path: str,
    paths,
    chunk_index: int = 0,
    commit_message: str = "",
    commit_timestamp: int = 1704153600,
) -> TemporalSearchResult:
    payload = {
        "type": "commit_chunk",
        "is_head": is_head,
        "commit_hash": commit_hash,
        "commit_timestamp": commit_timestamp,
        "commit_date": "2024-01-02",
        "author_name": "Test User",
        "author_email": "test@example.com",
        "paths": paths,
        "primary_path": primary_path,
        "chunk_index": chunk_index,
        "commit_message": commit_message,
    }
    return TemporalSearchResult(
        file_path=primary_path,
        chunk_index=chunk_index,
        content=f"chunk {chunk_index} of {commit_hash}",
        score=score,
        metadata=payload,
        temporal_context={
            "commit_hash": commit_hash,
            "commit_timestamp": commit_timestamp,
            "commit_message": commit_message,
        },
    )


# ---------------------------------------------------------------------------
# AC10: dedup_by_commit primitive
# ---------------------------------------------------------------------------


class TestDedupByCommit:
    def test_multiple_chunks_same_commit_collapse_to_one(self):
        r1 = make_result("abc", 0.5, is_head=True, primary_path="a.py", paths=["a.py"])
        r2 = make_result("abc", 0.9, is_head=False, primary_path="b.py", paths=["b.py"])
        r3 = make_result("abc", 0.3, is_head=False, primary_path="c.py", paths=["c.py"])

        deduped = dedup_by_commit([r1, r2, r3])

        assert len(deduped) == 1
        assert deduped[0].metadata["commit_hash"] == "abc"

    def test_top_chunk_is_max_scoring(self):
        r1 = make_result("abc", 0.5, is_head=True, primary_path="a.py", paths=["a.py"])
        r2 = make_result("abc", 0.9, is_head=False, primary_path="b.py", paths=["b.py"])

        deduped = dedup_by_commit([r1, r2])

        assert len(deduped) == 1
        assert deduped[0].score == 0.9
        assert deduped[0].file_path == "b.py"

    def test_paths_are_unioned_from_all_retained_chunks(self):
        r1 = make_result("abc", 0.5, is_head=True, primary_path="a.py", paths=["a.py"])
        r2 = make_result(
            "abc", 0.9, is_head=False, primary_path="b.py", paths=["b.py", "c.py"]
        )

        deduped = dedup_by_commit([r1, r2])

        assert len(deduped) == 1
        assert deduped[0].metadata["paths"] == ["a.py", "b.py", "c.py"]

    def test_distinct_commits_are_not_merged(self):
        r1 = make_result("abc", 0.9, is_head=True, primary_path="a.py", paths=["a.py"])
        r2 = make_result("def", 0.8, is_head=True, primary_path="b.py", paths=["b.py"])

        deduped = dedup_by_commit([r1, r2])

        assert len(deduped) == 2
        hashes = {r.metadata["commit_hash"] for r in deduped}
        assert hashes == {"abc", "def"}

    def test_low_ranked_chunk_still_produces_a_result_after_coalesce(self):
        """AC10 dedicated test: a commit whose only matching chunk is LOW in the
        raw retrieval order must still surface after dedup — it must never be
        truncated away before dedup-by-commit runs."""
        high = [
            make_result(f"high{i}", 0.99 - i * 0.001, True, f"h{i}.py", [f"h{i}.py"])
            for i in range(50)
        ]
        low_ranked = make_result(
            "lowrank", 0.01, True, "low.py", ["low.py"], commit_timestamp=1
        )
        raw_hits = high + [low_ranked]

        deduped = dedup_by_commit(raw_hits)

        commit_hashes = {r.metadata["commit_hash"] for r in deduped}
        assert "lowrank" in commit_hashes

    def test_empty_input_returns_empty_list(self):
        assert dedup_by_commit([]) == []


# ---------------------------------------------------------------------------
# query_temporal() end-to-end pipeline (AC10, AC12, AC13)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_config_manager():
    manager = MagicMock()
    config = MagicMock()
    config.codebase_dir = Path("/tmp/test-1290")
    manager.get_config.return_value = config
    return manager


@pytest.fixture
def service(mock_config_manager):
    return TemporalSearchService(
        config_manager=mock_config_manager,
        project_root=Path("/tmp/test-1290"),
        vector_store_client=MagicMock(),
        embedding_provider=MagicMock(),
    )


def _mock_hit(payload, score, chunk_text):
    hit = MagicMock()
    hit.payload = payload
    hit.score = score
    hit.chunk_text = chunk_text
    return hit


def _payload(
    commit_hash,
    is_head,
    primary_path,
    paths,
    commit_timestamp,
    commit_message="",
    chunk_index=0,
):
    return {
        "type": "commit_chunk",
        "is_head": is_head,
        "commit_hash": commit_hash,
        "commit_timestamp": commit_timestamp,
        "commit_date": "2024-01-01",
        "author_name": "Alice",
        "author_email": "alice@example.com",
        "paths": paths,
        "primary_path": primary_path,
        "chunk_index": chunk_index,
        "commit_message": commit_message,
    }


class TestQueryTemporalDedupPipeline:
    def test_two_chunks_same_commit_yield_one_result(self, service):
        service.vector_store_client.collection_exists.return_value = True
        service.vector_store_client.__class__.__name__ = "FilesystemClient"
        service.vector_store_client.search.return_value = [
            _mock_hit(
                _payload("abc123", True, "a.py", ["a.py"], 1704153600, "Fix bug"),
                0.9,
                "head chunk text",
            ),
            _mock_hit(
                _payload("abc123", False, "b.py", ["b.py"], 1704153600),
                0.95,
                "diff chunk text",
            ),
        ]
        service.embedding_provider.get_embedding.return_value = [0.1] * 1024

        results = service.query_temporal(
            query="authentication",
            time_range=("2024-01-01", "2024-12-31"),
            limit=10,
        )

        assert len(results.results) == 1
        assert results.results[0].metadata["commit_hash"] == "abc123"
        # paths unioned across retained chunks
        assert set(results.results[0].metadata["paths"]) == {"a.py", "b.py"}

    def test_chunk_type_commit_message_maps_to_is_head_only(self, service):
        service.vector_store_client.collection_exists.return_value = True
        service.vector_store_client.__class__.__name__ = "FilesystemClient"
        service.vector_store_client.search.return_value = [
            _mock_hit(
                _payload("abc123", True, "a.py", ["a.py"], 1704153600, "Fix bug"),
                0.9,
                "head chunk text",
            ),
            _mock_hit(
                _payload("def456", False, "b.py", ["b.py"], 1704240000),
                0.95,
                "diff chunk text",
            ),
        ]
        service.embedding_provider.get_embedding.return_value = [0.1] * 1024

        results = service.query_temporal(
            query="authentication",
            time_range=("2024-01-01", "2024-12-31"),
            limit=10,
            chunk_type="commit_message",
        )

        assert len(results.results) == 1
        assert results.results[0].metadata["commit_hash"] == "abc123"
        assert results.results[0].metadata["is_head"] is True

    def test_chunk_type_commit_diff_accepts_all_chunks(self, service):
        service.vector_store_client.collection_exists.return_value = True
        service.vector_store_client.__class__.__name__ = "FilesystemClient"
        service.vector_store_client.search.return_value = [
            _mock_hit(
                _payload("abc123", True, "a.py", ["a.py"], 1704153600, "Fix bug"),
                0.9,
                "head chunk text",
            ),
            _mock_hit(
                _payload("def456", False, "b.py", ["b.py"], 1704240000),
                0.95,
                "diff chunk text",
            ),
        ]
        service.embedding_provider.get_embedding.return_value = [0.1] * 1024

        results = service.query_temporal(
            query="authentication",
            time_range=("2024-01-01", "2024-12-31"),
            limit=10,
            chunk_type="commit_diff",
        )

        assert len(results.results) == 2

    def test_chunk_type_invalid_value_raises(self, service):
        service.vector_store_client.collection_exists.return_value = True
        with pytest.raises(ValueError):
            service.query_temporal(
                query="x",
                time_range=("2024-01-01", "2024-12-31"),
                limit=10,
                chunk_type="bogus",
            )

    def test_default_order_is_reverse_chronological_across_commits(self, service):
        service.vector_store_client.collection_exists.return_value = True
        service.vector_store_client.__class__.__name__ = "FilesystemClient"
        service.vector_store_client.search.return_value = [
            _mock_hit(
                _payload("older", True, "a.py", ["a.py"], 1704153600, "old"),
                0.5,
                "old chunk",
            ),
            _mock_hit(
                _payload("newer", True, "b.py", ["b.py"], 1704999999, "new"),
                0.4,
                "new chunk",
            ),
        ]
        service.embedding_provider.get_embedding.return_value = [0.1] * 1024

        results = service.query_temporal(
            query="x",
            time_range=("2024-01-01", "2024-12-31"),
            limit=10,
        )

        assert [r.metadata["commit_hash"] for r in results.results] == [
            "newer",
            "older",
        ]

    def test_author_filter_still_works_after_dedup(self, service):
        service.vector_store_client.collection_exists.return_value = True
        service.vector_store_client.__class__.__name__ = "FilesystemClient"
        service.vector_store_client.search.return_value = [
            _mock_hit(
                _payload("abc", True, "a.py", ["a.py"], 1704153600, "m"),
                0.9,
                "chunk a",
            ),
            _mock_hit(
                {
                    **_payload("def", True, "b.py", ["b.py"], 1704153600, "m"),
                    "author_name": "Bob",
                },
                0.8,
                "chunk b",
            ),
        ]
        service.embedding_provider.get_embedding.return_value = [0.1] * 1024

        results = service.query_temporal(
            query="x",
            time_range=("2024-01-01", "2024-12-31"),
            limit=10,
            author="Alice",
        )

        assert len(results.results) == 1
        assert results.results[0].metadata["commit_hash"] == "abc"


# ---------------------------------------------------------------------------
# AC11: fail-loud full-message reconstruction for non-head top match
# ---------------------------------------------------------------------------


class TestFullMessageReconstruction:
    def test_non_head_top_match_reconstructs_full_message_on_success(self, service):
        service.vector_store_client.collection_exists.return_value = True
        service.vector_store_client.__class__.__name__ = "FilesystemClient"
        service.vector_store_client.search.return_value = [
            _mock_hit(
                _payload("abc123", True, "a.py", ["a.py"], 1704153600, "Short cap"),
                0.5,
                "head chunk",
            ),
            _mock_hit(
                _payload("abc123", False, "b.py", ["b.py"], 1704153600),
                0.95,
                "diff chunk beats head on score",
            ),
        ]
        service.embedding_provider.get_embedding.return_value = [0.1] * 1024

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="Full multi-line\ncommit message body\n"
            )
            results = service.query_temporal(
                query="x",
                time_range=("2024-01-01", "2024-12-31"),
                limit=10,
            )

        assert len(results.results) == 1
        top = results.results[0]
        assert (
            top.temporal_context["commit_message"]
            == "Full multi-line\ncommit message body"
        )
        assert top.temporal_context.get("message_truncated") is False

    def test_non_head_top_match_flags_degraded_on_git_failure(self, service):
        service.vector_store_client.collection_exists.return_value = True
        service.vector_store_client.__class__.__name__ = "FilesystemClient"
        service.vector_store_client.search.return_value = [
            _mock_hit(
                _payload("abc123", True, "a.py", ["a.py"], 1704153600, "Short cap"),
                0.5,
                "head chunk",
            ),
            _mock_hit(
                _payload("abc123", False, "b.py", ["b.py"], 1704153600),
                0.95,
                "diff chunk beats head on score",
            ),
        ]
        service.embedding_provider.get_embedding.return_value = [0.1] * 1024

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=128, stdout="", stderr="fatal: bad object"
            )
            results = service.query_temporal(
                query="x",
                time_range=("2024-01-01", "2024-12-31"),
                limit=10,
            )

        assert len(results.results) == 1
        top = results.results[0]
        # never silently substitutes the capped copy as if it were the full message
        assert top.temporal_context.get("message_truncated") is True
        assert top.temporal_context["commit_message"] == "Short cap"

    def test_non_head_top_match_flags_degraded_on_git_timeout(self, service):
        """AC11: a `git show` subprocess.TimeoutExpired must produce the SAME
        explicitly-flagged degraded result as any other reconstruction
        failure -- never a silent/unflagged substitution, and never an
        uncaught exception propagating out of query_temporal."""
        service.vector_store_client.collection_exists.return_value = True
        service.vector_store_client.__class__.__name__ = "FilesystemClient"
        service.vector_store_client.search.return_value = [
            _mock_hit(
                _payload("abc123", True, "a.py", ["a.py"], 1704153600, "Short cap"),
                0.5,
                "head chunk",
            ),
            _mock_hit(
                _payload("abc123", False, "b.py", ["b.py"], 1704153600),
                0.95,
                "diff chunk beats head on score",
            ),
        ]
        service.embedding_provider.get_embedding.return_value = [0.1] * 1024

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd=["git", "show", "-s", "--format=%B", "abc123"], timeout=30
            )
            results = service.query_temporal(
                query="x",
                time_range=("2024-01-01", "2024-12-31"),
                limit=10,
            )

        assert len(results.results) == 1
        top = results.results[0]
        # never silently substitutes the capped copy as if it were the full message
        assert top.temporal_context.get("message_truncated") is True
        assert top.temporal_context["commit_message"] == "Short cap"

    def test_head_top_match_does_not_attempt_reconstruction(self, service):
        service.vector_store_client.collection_exists.return_value = True
        service.vector_store_client.__class__.__name__ = "FilesystemClient"
        service.vector_store_client.search.return_value = [
            _mock_hit(
                _payload("abc123", True, "a.py", ["a.py"], 1704153600, "Head msg"),
                0.9,
                "head chunk",
            ),
        ]
        service.embedding_provider.get_embedding.return_value = [0.1] * 1024

        with patch("subprocess.run") as mock_run:
            results = service.query_temporal(
                query="x",
                time_range=("2024-01-01", "2024-12-31"),
                limit=10,
            )
            mock_run.assert_not_called()

        assert results.results[0].temporal_context["commit_message"] == "Head msg"
        assert results.results[0].temporal_context.get("message_truncated") is False


# ---------------------------------------------------------------------------
# AC14: contextualized endpoint used for temporal queries
# ---------------------------------------------------------------------------


class TestContextualQueryEmbedding:
    def test_voyage_context_4_get_embedding_uses_contextualized_endpoint(self):
        from code_indexer.config import VoyageAIConfig
        from code_indexer.services.voyage_ai import VoyageAIClient

        client = VoyageAIClient(VoyageAIConfig(model="voyage-context-4"))
        with patch.object(
            client, "get_contextualized_embeddings"
        ) as mock_contextualized:
            mock_contextualized.return_value = [[[0.1] * 1024]]
            vec = client.get_embedding("some query", embedding_purpose="query")

        assert vec == [0.1] * 1024
        mock_contextualized.assert_called_once()
        _, kwargs = mock_contextualized.call_args
        assert kwargs.get("input_type") == "query"

    def test_regular_model_get_embedding_does_not_use_contextualized_endpoint(self):
        from code_indexer.config import VoyageAIConfig
        from code_indexer.services.voyage_ai import VoyageAIClient

        client = VoyageAIClient(VoyageAIConfig(model="voyage-code-3"))
        with patch.object(client, "get_contextualized_embeddings") as mock_ctx:
            with patch.object(client, "_make_sync_request") as mock_std:
                mock_std.return_value = {
                    "data": [{"embedding": [0.2] * 1024}],
                    "usage": {"total_tokens": 5},
                }
                client.get_embedding("some query")

        mock_ctx.assert_not_called()
        mock_std.assert_called_once()

    def test_query_embedding_cache_qualifier_resolves_voyage_context_4_tuple(self):
        """AC14: the query-embedding cache key axis for a voyage-context-4
        temporal query resolves to the EXACT tuple
        (provider="voyage-ai", model="voyage-context-4", dimension=1024) --
        never a different provider/model/dimension that would silently
        collide with (or diverge from) the regular voyage-code-3 index."""
        import os

        from code_indexer.config import VoyageAIConfig
        from code_indexer.services.voyage_ai import VoyageAIClient
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        with patch.dict(os.environ, {"VOYAGE_API_KEY": "PLACEHOLDER"}):
            client = VoyageAIClient(VoyageAIConfig(model="voyage-context-4"))

        cache = QueryEmbeddingCache(backend=MagicMock())
        qualifier = cache.qualifier(client)

        assert qualifier == ("voyage-ai", "voyage-context-4", 1024)
        assert qualifier.provider == "voyage-ai"
        assert qualifier.model == "voyage-context-4"
        assert qualifier.dimension == 1024

    def test_get_embeddings_batch_query_purpose_uses_contextualized_endpoint(self):
        """Bug (Story #1292, found via real server front-door e2e testing):
        the server's EmbeddingCoalescer calls get_embeddings_batch() directly
        (NOT get_embedding()) for every query -- including single-query
        "batches" of size 1 -- so AC14's contextual-endpoint special-casing,
        which previously lived ONLY in get_embedding(), never fired for
        server-mode temporal queries against voyage-context-4. This produced
        a real HTTP 400 from the plain /v1/embeddings endpoint (which
        rejects voyage-context-4), breaking temporal search server-side
        while CLI/solo mode (which calls get_embedding() directly) worked
        fine. get_embeddings_batch() must ALSO route
        embedding_purpose="query" + a contextual model through
        get_contextualized_embeddings.
        """
        from code_indexer.config import VoyageAIConfig
        from code_indexer.services.voyage_ai import VoyageAIClient

        client = VoyageAIClient(VoyageAIConfig(model="voyage-context-4"))
        with patch.object(
            client, "get_contextualized_embeddings"
        ) as mock_contextualized:
            mock_contextualized.return_value = [
                [[0.1] * 1024],
                [[0.2] * 1024],
            ]
            vectors = client.get_embeddings_batch(
                ["query one", "query two"], embedding_purpose="query"
            )

        assert vectors == [[0.1] * 1024, [0.2] * 1024]
        mock_contextualized.assert_called_once()
        args, kwargs = mock_contextualized.call_args
        assert args[0] == [["query one"], ["query two"]]
        assert kwargs.get("input_type") == "query"

    def test_get_embeddings_batch_document_purpose_unaffected(self):
        """Regression guard: indexing-purpose batches (the common case, ALL
        non-contextual models, and any caller not passing
        embedding_purpose="query") are BYTE-IDENTICAL to pre-fix behavior --
        the plain batch endpoint, never the contextualized one."""
        from code_indexer.config import VoyageAIConfig
        from code_indexer.services.voyage_ai import VoyageAIClient

        client = VoyageAIClient(VoyageAIConfig(model="voyage-code-3"))
        with patch.object(client, "get_contextualized_embeddings") as mock_ctx:
            with patch.object(client, "_make_sync_request") as mock_std:
                mock_std.return_value = {
                    "data": [{"embedding": [0.3] * 1024}],
                    "usage": {"total_tokens": 5},
                }
                client.get_embeddings_batch(["some text"], embedding_purpose="document")

        mock_ctx.assert_not_called()
        mock_std.assert_called_once()
