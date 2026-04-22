"""Tests for Phase B (Story #883) Component 2 — MemoryCandidateRetriever.

Declared test list (exactly 4):
  1. test_retrieve_returns_empty_when_no_index
  2. test_retrieve_returns_candidates_with_hnsw_scores
  3. test_missing_index_logs_info_only_once
  4. test_retrieve_requires_query_vector_and_user_id

TDD: these tests are written BEFORE the implementation.

External dependency mocked: FilesystemVectorStore.search — the only I/O boundary.
Internal methods of MemoryCandidateRetriever are NOT mocked.
"""

import logging
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Module-level isolation helper
# ---------------------------------------------------------------------------


def _make_retriever(store_path: str = "/fake/cidx-meta"):
    """Construct a fresh MemoryCandidateRetriever for each test.

    We must reset the class-level _missing_logged sentinel between tests so
    that logging behaviour is predictable (the sentinel is per-process but
    tests run in the same process).
    """
    from code_indexer.server.services.memory_candidate_retriever import (
        MemoryCandidateRetriever,
    )

    MemoryCandidateRetriever._missing_logged = False
    return MemoryCandidateRetriever(store_base_path=store_path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMemoryCandidateRetriever:
    """Unit tests for MemoryCandidateRetriever (Story #883 Component 2)."""

    def test_retrieve_returns_empty_when_no_index(self, tmp_path):
        """retrieve() returns [] when FilesystemVectorStore.search() finds no collection.

        FilesystemVectorStore.search() already returns [] when the collection
        does not exist. MemoryCandidateRetriever must propagate that empty list.
        """
        retriever = _make_retriever(str(tmp_path))
        query_vector = [0.1, 0.2, 0.3]
        user_id = "user-1"

        with patch(
            "code_indexer.server.services.memory_candidate_retriever"
            ".FilesystemVectorStore.search",
            return_value=[],
        ):
            candidates = retriever.retrieve(
                query_vector=query_vector, user_id=user_id, k=10
            )

        assert candidates == []

    def test_retrieve_returns_candidates_with_hnsw_scores(self, tmp_path):
        """retrieve() converts store results into MemoryCandidate objects with hnsw_score.

        Each result dict from FilesystemVectorStore.search() has at least:
          {"id": str, "score": float, "payload": {...}}
        MemoryCandidateRetriever must produce one MemoryCandidate per result with
        memory_id=id and hnsw_score=score.
        """
        retriever = _make_retriever(str(tmp_path))
        query_vector = [0.5] * 8
        user_id = "user-2"

        fake_results = [
            {
                "id": "mem-abc",
                "score": 0.82,
                "payload": {"path": "memories/mem-abc.md"},
            },
            {
                "id": "mem-xyz",
                "score": 0.61,
                "payload": {"path": "memories/mem-xyz.md"},
            },
        ]

        with patch(
            "code_indexer.server.services.memory_candidate_retriever"
            ".FilesystemVectorStore.search",
            return_value=fake_results,
        ):
            candidates = retriever.retrieve(
                query_vector=query_vector, user_id=user_id, k=10
            )

        assert len(candidates) == 2
        assert candidates[0].memory_id == "mem-abc"
        assert candidates[0].hnsw_score == pytest.approx(0.82)
        assert candidates[1].memory_id == "mem-xyz"
        assert candidates[1].hnsw_score == pytest.approx(0.61)

    def test_missing_index_logs_info_only_once(self, tmp_path, caplog):
        """When the HNSW index is absent, INFO is logged exactly once per process.

        Subsequent calls with no index must NOT emit another INFO log.
        The once-per-process guard is the class-level _missing_logged flag.
        """
        retriever = _make_retriever(str(tmp_path))
        query_vector = [0.1] * 4
        user_id = "user-3"

        # Both calls return [] (simulating missing collection/index)
        with patch(
            "code_indexer.server.services.memory_candidate_retriever"
            ".FilesystemVectorStore.search",
            return_value=[],
        ):
            with caplog.at_level(logging.INFO, logger="code_indexer"):
                caplog.clear()
                retriever.retrieve(query_vector=query_vector, user_id=user_id, k=5)
                first_info_count = sum(
                    1
                    for r in caplog.records
                    if r.levelno == logging.INFO and "memor" in r.message.lower()
                )
                caplog.clear()
                retriever.retrieve(query_vector=query_vector, user_id=user_id, k=5)
                second_info_count = sum(
                    1
                    for r in caplog.records
                    if r.levelno == logging.INFO and "memor" in r.message.lower()
                )

        assert first_info_count == 1, (
            "Expected exactly 1 INFO log on first missing-index call"
        )
        assert second_info_count == 0, (
            "Expected 0 INFO logs on subsequent missing-index calls (once-per-process guard)"
        )

    def test_retrieve_requires_query_vector_and_user_id(self, tmp_path):
        """retrieve() raises ValueError when query_vector or user_id is None or empty."""
        retriever = _make_retriever(str(tmp_path))

        with pytest.raises(ValueError, match="query_vector"):
            retriever.retrieve(query_vector=None, user_id="user-x", k=5)

        with pytest.raises(ValueError, match="query_vector"):
            retriever.retrieve(query_vector=[], user_id="user-x", k=5)

        with pytest.raises(ValueError, match="user_id"):
            retriever.retrieve(query_vector=[0.1, 0.2], user_id=None, k=5)

        with pytest.raises(ValueError, match="user_id"):
            retriever.retrieve(query_vector=[0.1, 0.2], user_id="", k=5)

    def test_retrieve_populates_memory_path(self, tmp_path):
        """retrieve() populates memory_path from the HNSW payload's 'path' field.

        The HNSW payload stores the disk path of the memory file under the 'path'
        key.  MemoryCandidateRetriever must expose this so Phase D (_tag_and_pool)
        can inject it into the pooled item dict for later hydration (Stage 8).
        """
        retriever = _make_retriever(str(tmp_path))
        query_vector = [0.5] * 4
        user_id = "user-path-test"

        fake_results = [
            {
                "id": "mem-123",
                "score": 0.75,
                "payload": {"path": "/cidx-meta/memories/mem-123.md"},
            },
        ]

        with patch(
            "code_indexer.server.services.memory_candidate_retriever"
            ".FilesystemVectorStore.search",
            return_value=fake_results,
        ):
            candidates = retriever.retrieve(
                query_vector=query_vector, user_id=user_id, k=5
            )

        assert len(candidates) == 1
        assert candidates[0].memory_id == "mem-123"
        assert candidates[0].memory_path == "/cidx-meta/memories/mem-123.md"
