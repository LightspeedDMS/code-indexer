"""Bug #1575 Part C cleanup: wire the orphaned ``abort_indexing()`` method
into ``TemporalIndexer._index_one_embedder``'s per-shard loop.

Context: ``FilesystemVectorStore.abort_indexing()`` discards THIS process's
in-memory ``HNSWSyncSession``/session-change tracking for a collection --
intended for a shard whose indexing attempt raised an exception between
``begin_indexing()`` and ``end_indexing()``. Every OTHER begin/end pair in
this codebase (smart_indexer.py, high_throughput_processor.py) is already
bracketed by a try/finally that unconditionally calls ``end_indexing()``.
``_index_one_embedder``'s per-shard loop was the one real gap: it calls
``begin_indexing(shard)`` then does real work, then ``end_indexing(shard)``,
with NO try/except in between -- an exception raised by
``_index_shard_commits`` skips ``end_indexing()`` entirely.

This matters because ``self.vector_store`` is not always a throwaway
per-call object: ``cli_temporal_watch_handler.py`` constructs ONE
``TemporalIndexer`` (and hence one ``FilesystemVectorStore``) and calls
``index_commits()`` repeatedly across watch-loop ticks, catching and
logging exceptions per tick rather than crashing the process. Without
``abort_indexing()`` wired in, a failed shard's abandoned session would
silently carry into the NEXT tick's ``begin_indexing()`` call for the SAME
shard (``_get_or_create_hnsw_sync_session`` reuses an existing session
as-is), corrupting the next incremental decision.

This test module exercises the REAL ``_index_one_embedder`` method (not a
mocked version of it). The one internal call mocked is
``_index_shard_commits`` -- the external dependency that performs the
actual embedding/upsert work -- forced to raise, which is exactly the
failure boundary abort_indexing() must guard.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.services.temporal.temporal_indexer import TemporalIndexer


def _make_config_manager(tmp_path: Path) -> MagicMock:
    """Build a minimal config manager mock for TemporalIndexer construction."""
    from code_indexer.config import TemporalConfig

    config = MagicMock()
    config.codebase_dir = tmp_path
    config.embedding_provider = "voyage-ai"
    config.file_extensions = None
    config.temporal = TemporalConfig()
    config.override_config = None

    voyage_ai = MagicMock()
    voyage_ai.model = "voyage-code-3"
    voyage_ai.parallel_requests = 4
    config.voyage_ai = voyage_ai

    config_manager = MagicMock()
    config_manager.get_config.return_value = config
    config_manager.config_path = tmp_path / ".code-indexer" / "config.json"
    return config_manager


def _make_vector_store(tmp_path: Path) -> MagicMock:
    """Build a vector store mock whose ``_get_collection_path()`` returns a
    non-``Path`` sentinel so the per-shard loop's ``isinstance(..., Path)``
    guards short-circuit the durable stale-barrier machinery (out of scope
    here) and take the simplest, no-op code paths.
    """
    index_dir = tmp_path / ".code-indexer" / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    vector_store = MagicMock()
    vector_store.project_root = tmp_path
    vector_store.base_path = index_dir
    vector_store.collection_exists.return_value = False
    vector_store._get_collection_path.return_value = "not-a-path-sentinel"
    return vector_store


def _make_indexer(tmp_path: Path) -> TemporalIndexer:
    config_manager = _make_config_manager(tmp_path)
    vector_store = _make_vector_store(tmp_path)
    indexer = TemporalIndexer(
        config_manager,
        vector_store,
        collection_name="code-indexer-temporal-voyage_code_3",
    )
    # index_commits() (the real caller of _index_one_embedder) always
    # initializes this before dispatching to any embedder; tests call
    # _index_one_embedder directly, so it must be set up here too.
    indexer._processed_shards = []
    return indexer


class TestIndexOneEmbedderAbortsOnShardFailure:
    """A shard failure between begin_indexing()/end_indexing() must discard
    the in-memory session via abort_indexing() and still fail loud."""

    def test_shard_failure_between_begin_and_end_indexing_calls_abort_indexing(
        self, tmp_path
    ):
        indexer = _make_indexer(tmp_path)
        shard_name = "code-indexer-temporal-voyage_code_3-2024Q1"

        embedder_instance = MagicMock()
        embedder_instance.dimensions = 16

        # Force the real per-shard work to blow up AFTER begin_indexing()
        # (called inside _index_one_embedder) but BEFORE end_indexing().
        indexer._index_shard_commits = MagicMock(
            side_effect=RuntimeError("boom-mid-shard")
        )

        with pytest.raises(RuntimeError, match="boom-mid-shard"):
            indexer._index_one_embedder(
                embedder_name="voyage_code_3",
                embedder_instance=embedder_instance,
                shard_commit_map={shard_name: ["deadbeef"]},
                vector_manager=MagicMock(),
                progress_callback=None,
            )

        indexer.vector_store.begin_indexing.assert_called_once_with(shard_name)
        indexer.vector_store.abort_indexing.assert_called_once_with(shard_name)
        indexer.vector_store.end_indexing.assert_not_called()

    def test_successful_shard_does_not_call_abort_indexing(self, tmp_path):
        """The happy path must be byte-identical -- abort_indexing() is
        ONLY for the failure path, never called alongside a clean
        end_indexing()."""
        indexer = _make_indexer(tmp_path)
        shard_name = "code-indexer-temporal-voyage_code_3-2024Q1"

        embedder_instance = MagicMock()
        embedder_instance.dimensions = 16

        indexer._index_shard_commits = MagicMock(return_value=(1, 1, 1))

        commits_processed, blobs_processed, vectors_created = (
            indexer._index_one_embedder(
                embedder_name="voyage_code_3",
                embedder_instance=embedder_instance,
                shard_commit_map={shard_name: ["deadbeef"]},
                vector_manager=MagicMock(),
                progress_callback=None,
            )
        )

        assert (commits_processed, blobs_processed, vectors_created) == (1, 1, 1)
        indexer.vector_store.begin_indexing.assert_called_once_with(shard_name)
        indexer.vector_store.end_indexing.assert_called_once()
        indexer.vector_store.abort_indexing.assert_not_called()
