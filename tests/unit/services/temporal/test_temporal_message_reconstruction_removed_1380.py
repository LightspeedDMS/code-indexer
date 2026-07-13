"""Tests for Bug #1380: remove per-candidate git-show commit-message
reconstruction from the temporal query recall hot path.

Root cause (confirmed via live PERF-TRACE instrumentation against a real
4-quarter temporal index): `query_temporal()` called
`_reconstruct_full_commit_message()` (a `git show -s --format=%B <hash>`
subprocess, 30s timeout) for EVERY deduped candidate whose winning chunk was
non-head, BEFORE the Phase-3 relevance truncation to `limit` -- this was
95-98% of wall-clock time on warm-cache queries (65-93s observed, HNSW
search itself only ~2s).

Fix (agreed with maintainer): remove `_reconstruct_full_commit_message()`
and its subprocess call entirely. Non-head dedup winners now use
`dedup_by_commit()`'s `_head_commit_message` stash (captured for free,
zero extra cost, whenever the head chunk was co-retrieved in the same
over-fetched batch) as their message source -- never git.

These tests assert:
1. `query_temporal()` NEVER invokes `subprocess.run` on this path, for any
   number of non-head-winning candidates.
2. Non-head winners source their message from the `_head_commit_message`
   stash when available.
3. Non-head winners with no stash (head chunk not co-retrieved) fall back
   to the empty per-AC5 `commit_message` on their own payload -- never a
   crash, never a git call.
4. `message_truncated` is always `True` for non-head winners (no full
   reconstruction path exists anymore) and `False` for is_head winners
   (unchanged).
5. `resolve_commit_timestamp()` (a SEPARATE function, Bug #1301 at_commit
   validation) is untouched by this fix -- still shells out to git when
   used, since it is out of scope for this bug.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchService,
)


@pytest.fixture
def mock_config_manager():
    manager = MagicMock()
    config = MagicMock()
    config.codebase_dir = Path("/tmp/test-1380")
    manager.get_config.return_value = config
    return manager


@pytest.fixture
def service(mock_config_manager):
    return TemporalSearchService(
        config_manager=mock_config_manager,
        project_root=Path("/tmp/test-1380"),
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


class TestNoGitReconstructionOnQueryPath:
    def test_non_head_winner_with_head_co_retrieved_uses_stash_no_git_call(
        self, service
    ):
        """Non-head winner whose head chunk WAS co-retrieved gets the
        head-chunk's short-capped message via the dedup stash, with ZERO
        subprocess calls -- this is the exact scenario that previously
        triggered a `git show` subprocess."""
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
            results = service.query_temporal(
                query="x",
                time_range=("2024-01-01", "2024-12-31"),
                limit=10,
            )
            mock_run.assert_not_called()

        assert len(results.results) == 1
        top = results.results[0]
        assert top.temporal_context["commit_message"] == "Short cap"
        assert top.temporal_context.get("message_truncated") is True

    def test_non_head_winner_with_no_head_co_retrieved_falls_back_empty_no_git_call(
        self, service
    ):
        """Non-head winner whose head chunk was NOT part of the same
        over-fetched batch has no stash to draw from -- falls back to its
        own (empty, per AC5) commit_message. Must never attempt a git call
        or raise."""
        service.vector_store_client.collection_exists.return_value = True
        service.vector_store_client.__class__.__name__ = "FilesystemClient"
        service.vector_store_client.search.return_value = [
            _mock_hit(
                _payload("abc123", False, "b.py", ["b.py"], 1704153600),
                0.95,
                "only a non-head chunk retrieved -- no head chunk in batch",
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

        assert len(results.results) == 1
        top = results.results[0]
        assert top.temporal_context["commit_message"] == ""
        assert top.temporal_context.get("message_truncated") is True

    def test_head_winner_message_truncated_false_no_git_call(self, service):
        """is_head winners are unaffected: message comes straight from the
        payload, message_truncated is False, and no git call ever fires."""
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

    def test_many_non_head_winners_zero_subprocess_calls(self, service):
        """Regression guard for the actual measured bug: 29-89 sequential
        git subprocess calls observed for a single query with many
        non-head-winning deduped candidates. With the fix, ANY number of
        non-head winners must produce ZERO subprocess calls."""
        service.vector_store_client.collection_exists.return_value = True
        service.vector_store_client.__class__.__name__ = "FilesystemClient"
        hits = []
        for i in range(50):
            commit_hash = f"commit{i}"
            hits.append(
                _mock_hit(
                    _payload(
                        commit_hash,
                        True,
                        f"a{i}.py",
                        [f"a{i}.py"],
                        1704153600 + i,
                        f"head msg {i}",
                    ),
                    0.5,
                    f"head chunk {i}",
                )
            )
            hits.append(
                _mock_hit(
                    _payload(
                        commit_hash, False, f"b{i}.py", [f"b{i}.py"], 1704153600 + i
                    ),
                    0.95,
                    f"diff chunk {i} beats head on score",
                )
            )
        service.vector_store_client.search.return_value = hits
        service.embedding_provider.get_embedding.return_value = [0.1] * 1024

        with patch("subprocess.run") as mock_run:
            results = service.query_temporal(
                query="x",
                time_range=("2024-01-01", "2024-12-31"),
                limit=50,
            )
            mock_run.assert_not_called()

        assert len(results.results) == 50
        for result in results.results:
            assert result.temporal_context.get("message_truncated") is True
            assert result.temporal_context["commit_message"].startswith("head msg")

    def test_reconstruct_full_commit_message_method_removed(self, service):
        """The method itself must no longer exist on TemporalSearchService --
        full removal was the agreed fix scope, not a dead/unused method."""
        assert not hasattr(service, "_reconstruct_full_commit_message")

    def test_resolve_commit_timestamp_untouched_still_uses_git(self, tmp_path):
        """resolve_commit_timestamp (Bug #1301 at_commit validation) is a
        SEPARATE function from the commit-message reconstruction removed by
        this fix -- it must remain untouched and still shell out to git."""
        from code_indexer.services.temporal.temporal_search_service import (
            resolve_commit_timestamp,
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="deadbeef\n"),
                MagicMock(returncode=0, stdout="1704153600\n"),
            ]
            ts = resolve_commit_timestamp(tmp_path, "HEAD")

        assert ts == 1704153600
        assert mock_run.call_count == 2
        first_call_cmd = mock_run.call_args_list[0].args[0]
        assert first_call_cmd[0] == "git"
