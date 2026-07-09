"""Tests for Bug #1299: relevance-based (score) truncation in query_temporal.

Bug #1299: TemporalSearchService.query_temporal's Phase 3 previously sorted
the ENTIRE deduped candidate set by commit_timestamp (newest first) and THEN
truncated to `[:limit]`. That made truncation recency-based instead of
relevance-based: a highly-relevant OLDER commit could be dropped before the
user ever saw it, while several weakly-relevant NEWER commits survived,
purely because they were newer. This was proven against real production data
(front-door E2E): the ground-truth best match (cosine 0.69) was dropped from
top-10 while commits scoring 0.33-0.41 were returned instead.

The fix: select the top-`limit` results by SCORE descending first, THEN
re-sort only that selected subset reverse-chronologically for display (the
existing, intentional, tested display behavior --
test_default_order_is_reverse_chronological_across_commits in
test_temporal_recall_dedup_1290.py -- must keep passing unchanged).
"""

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.services.temporal.temporal_search_service import TemporalSearchService


@pytest.fixture
def mock_config_manager():
    manager = MagicMock()
    config = MagicMock()
    config.codebase_dir = Path("/tmp/test-1299")
    manager.get_config.return_value = config
    return manager


@pytest.fixture
def service(mock_config_manager):
    return TemporalSearchService(
        config_manager=mock_config_manager,
        project_root=Path("/tmp/test-1299"),
        vector_store_client=MagicMock(),
        embedding_provider=MagicMock(),
    )


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


def _mock_hit(payload, score, chunk_text):
    hit = MagicMock()
    hit.payload = payload
    hit.score = score
    hit.chunk_text = chunk_text
    return hit


# Base timestamp for the "best" (highest-scoring but OLDEST) commit. Anchored
# inside the queried "2024-01-01..2024-12-31" time_range (well clear of the
# range boundaries) so the post-filter in _filter_by_time_range never
# excludes any pool member -- all 60 candidates must survive filtering so
# the truncation-by-score assertion exercises the full pool.
_BEST_TIMESTAMP = int(datetime.strptime("2024-01-05", "%Y-%m-%d").timestamp())
_ONE_DAY = 86400
_POOL_SIZE = 59  # plus 1 "best" == 60 total candidates


def _build_candidate_pool():
    """Build a pool of 60 candidates where the single highest-scoring
    commit ("best") is the OLDEST of the pool, and every other candidate
    is both lower-scoring AND strictly newer than "best".

    Under the buggy (pre-fix) behavior -- sort by commit_timestamp
    descending across the FULL set, then truncate to `limit` -- "best" is
    dead last in time order among 60 candidates, so it is excluded from
    the returned results at every limit strictly less than 60.

    Under the fixed behavior -- select top-`limit` by score descending,
    THEN re-sort that subset by time for display -- "best" (highest score)
    survives at every limit >= 1.
    """
    hits = [
        _mock_hit(
            _payload("best", True, "best.py", ["best.py"], _BEST_TIMESTAMP, "Best fix"),
            0.99,
            "the best matching chunk",
        )
    ]
    for i in range(_POOL_SIZE):
        score = 0.50 - i * 0.001
        timestamp = _BEST_TIMESTAMP + (i + 1) * _ONE_DAY
        hits.append(
            _mock_hit(
                _payload(
                    f"c{i}",
                    True,
                    f"c{i}.py",
                    [f"c{i}.py"],
                    timestamp,
                    f"commit {i}",
                ),
                score,
                f"chunk text {i}",
            )
        )
    return hits


class TestRelevanceBasedTruncation:
    """Bug #1299: truncation must be by score, not by commit_timestamp."""

    @pytest.mark.parametrize("limit", [3, 5, 8, 10, 30, 50])
    def test_highest_scoring_commit_survives_at_every_limit(self, service, limit):
        service.vector_store_client.collection_exists.return_value = True
        service.vector_store_client.__class__.__name__ = "FilesystemClient"
        service.vector_store_client.search.return_value = _build_candidate_pool()
        service.embedding_provider.get_embedding.return_value = [0.1] * 1024

        results = service.query_temporal(
            query="best fix",
            time_range=("2024-01-01", "2024-12-31"),
            limit=limit,
        )

        commit_hashes = [r.metadata["commit_hash"] for r in results.results]

        # (a) The highest-scoring commit must survive truncation regardless
        # of how many newer-but-lower-scoring commits compete for the slots.
        assert "best" in commit_hashes, (
            f"limit={limit}: highest-scoring commit 'best' (score=0.99) was "
            f"dropped in favor of lower-scoring newer commits: {commit_hashes}"
        )

        # (b) Returned results are still displayed reverse-chronologically
        # (newest first) among the results actually returned.
        timestamps = [r.temporal_context["commit_timestamp"] for r in results.results]
        assert timestamps == sorted(timestamps, reverse=True), (
            f"limit={limit}: results not in reverse-chronological display "
            f"order: {timestamps}"
        )

        # total_found must remain the full pre-truncation candidate count.
        assert results.total_found == _POOL_SIZE + 1

    def test_limit_sweep_is_non_monotonic_bug_reproduction(self, service):
        """Directly targets the reported limit-non-monotonic symptom: the
        ground-truth best match must be present at EVERY tested limit, not
        just some of them."""
        for limit in (3, 5, 8, 10, 30, 50):
            service.vector_store_client.collection_exists.return_value = True
            service.vector_store_client.__class__.__name__ = "FilesystemClient"
            service.vector_store_client.search.return_value = _build_candidate_pool()
            service.embedding_provider.get_embedding.return_value = [0.1] * 1024

            results = service.query_temporal(
                query="best fix",
                time_range=("2024-01-01", "2024-12-31"),
                limit=limit,
            )
            commit_hashes = {r.metadata["commit_hash"] for r in results.results}
            assert "best" in commit_hashes, f"limit={limit} dropped 'best'"
