"""Regression tests for Bug #1378: temporal indexing progress resets every
quarterly shard instead of reflecting the whole (multi-shard) embedder run.

Root cause (confirmed by code trace): `TemporalIndexer._index_one_embedder`
buckets an embedder's reconciled commits into per-quarter shards and calls
`_index_shard_commits(_shard_commits, ...)` once per shard. Inside that
method, `total = len(commits)` is the PER-SHARD commit count and
`completed_count` restarts from 0 for every shard -- so on a multi-quarter
run the `current`/`total` values handed to `progress_callback` are never
consistent with the whole run: they reset (and shrink/grow) every time a
new shard starts.

Fix: `_index_one_embedder` now wraps the caller-supplied `progress_callback`
per shard via `_wrap_shard_progress_callback`, translating each shard-local
`(current, total)` pair into a whole-run `(offset + current, grand_total)`
pair before forwarding to the real callback -- WITHOUT touching
`_index_shard_commits`'s signature, internal indexing logic, or its `info`
string (which is not rendered verbatim by the CLI display, only parsed for
rate metrics), keeping the fix narrowly scoped to progress reporting.
"""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


def _make_commit(hash_val: str, year: int, month: int, day: int):
    """Create a minimal CommitInfo with a timestamp in the given quarter."""
    from code_indexer.services.temporal.models import CommitInfo

    ts = int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())
    return CommitInfo(
        hash=hash_val,
        timestamp=ts,
        author_name="Test Author",
        author_email="test@example.com",
        message="test commit",
        parent_hashes="",
    )


class TestWrapShardProgressCallback:
    """Pure unit tests for the new `_wrap_shard_progress_callback` helper --
    no TemporalIndexer/mocking required.
    """

    def test_none_callback_wraps_to_none(self):
        from code_indexer.services.temporal.temporal_indexer import (
            _wrap_shard_progress_callback,
        )

        assert _wrap_shard_progress_callback(None, offset=5, grand_total=10) is None

    def test_wrapped_callback_forwards_offset_current_and_grand_total(self):
        from code_indexer.services.temporal.temporal_indexer import (
            _wrap_shard_progress_callback,
        )

        calls = []

        def real_callback(current, total, path, info="", **kwargs):
            calls.append((current, total, path, info, kwargs))

        wrapped = _wrap_shard_progress_callback(
            real_callback, offset=90, grand_total=8008
        )
        wrapped(
            2,
            5,
            Path("deadbeef"),
            info="2/5 commits (40%)",
            concurrent_files=["x"],
            slot_tracker="tracker",
            item_type="commits",
        )

        assert len(calls) == 1
        current, total, path, info, kwargs = calls[0]
        # 90 (offset from prior shards) + 2 (this shard's local progress) = 92
        assert current == 92
        # Whole-run denominator, NOT this shard's local total of 5.
        assert total == 8008
        assert path == Path("deadbeef")
        assert kwargs["concurrent_files"] == ["x"]
        assert kwargs["slot_tracker"] == "tracker"
        assert kwargs["item_type"] == "commits"

    def test_wrapped_callback_propagates_type_error_for_fallback_path(self):
        """`_index_shard_commits` retries with fewer kwargs on TypeError --
        the wrapper must transparently propagate that TypeError rather than
        swallowing it.
        """
        from code_indexer.services.temporal.temporal_indexer import (
            _wrap_shard_progress_callback,
        )

        def picky_callback(current, total, path, info=""):
            # Does not accept concurrent_files/slot_tracker/item_type.
            return None

        wrapped = _wrap_shard_progress_callback(
            picky_callback, offset=0, grand_total=10
        )
        with pytest.raises(TypeError):
            wrapped(1, 10, Path(""), info="x", concurrent_files=[], item_type="c")


class TestIndexOneEmbedderWholeRunProgress:
    """End-to-end (within `_index_one_embedder`) verification using a real
    `TemporalIndexer` across THREE synthetic quarterly shards of uneven size
    -- exactly the "quarterly-sharded" multi-quarter scenario from Bug #1378.
    """

    def _make_indexer(self, tmp_path):
        from code_indexer.services.temporal.temporal_indexer import TemporalIndexer

        mock_config = Mock()
        mock_config.voyage_ai = Mock()
        mock_config.voyage_ai.model = "voyage-code-3"
        mock_config.voyage_ai.parallel_requests = 4
        mock_config.voyage_ai.temporal_parallel_requests = None
        mock_config.voyage_ai.max_concurrent_batches_per_commit = 10
        mock_config.cohere = Mock()
        mock_config.cohere.parallel_requests = 4
        mock_config.cohere.temporal_parallel_requests = None
        mock_config.embedding_provider = "voyage-ai"
        mock_config.temporal = Mock()
        mock_config.temporal.diff_context_lines = 3
        mock_config.temporal.embedders = ["voyage-context-4"]
        mock_config.temporal.active_embedder = "voyage-context-4"
        mock_config.temporal.aggregation_chunk_chars = 4096
        mock_config.file_extensions = []
        mock_config.override_config = None
        mock_config.codebase_dir = tmp_path

        mock_config_manager = Mock()
        mock_config_manager.get_config.return_value = mock_config
        mock_config_manager.config_path = tmp_path / ".code-indexer" / "config.json"

        mock_vector_store = Mock()
        mock_vector_store.project_root = tmp_path
        mock_vector_store.base_path = tmp_path / ".code-indexer" / "index"
        mock_vector_store.collection_exists.return_value = True
        mock_vector_store.load_id_index.return_value = set()
        mock_vector_store.begin_indexing.return_value = None
        mock_vector_store.end_indexing.return_value = {"status": "ok"}
        mock_vector_store.upsert_points.return_value = None

        base_collection = "code-indexer-temporal-voyage_code_3"
        indexer = TemporalIndexer(
            mock_config_manager, mock_vector_store, collection_name=base_collection
        )
        return indexer

    def test_progress_current_and_total_reflect_whole_embedder_run(self, tmp_path):
        """Q1: 2 commits, Q2: 5 commits, Q3: 1 commit -- grand_total=8. Every
        single progress_callback invocation across ALL three shards must
        report total=8 (never a shard-local 2, 5, or 1), and `current` must
        be monotonically non-decreasing across shard boundaries, ending
        exactly at 8/8 -- never exceeding the total (no false-100% clamp).
        """
        indexer = self._make_indexer(tmp_path)

        commits = [
            _make_commit("q1a", 2024, 1, 10),
            _make_commit("q1b", 2024, 2, 20),
            _make_commit("q2a", 2024, 4, 1),
            _make_commit("q2b", 2024, 4, 15),
            _make_commit("q2c", 2024, 5, 1),
            _make_commit("q2d", 2024, 5, 15),
            _make_commit("q2e", 2024, 6, 1),
            _make_commit("q3a", 2024, 7, 1),
        ]

        progress_calls = []

        def fake_index_shard_commits(
            self_ref, shard_commits, vec_manager, prog_cb, reconcile
        ):
            # Simulate real per-commit progress emission for this shard.
            for i, c in enumerate(shard_commits, start=1):
                if prog_cb is not None:
                    prog_cb(
                        i,
                        len(shard_commits),
                        Path(c.hash),
                        info=f"{i}/{len(shard_commits)} commits",
                        concurrent_files=[],
                        slot_tracker=None,
                        item_type="commits",
                    )
            return len(shard_commits), len(shard_commits), len(shard_commits) * 2

        def progress_callback(current, total, path, info="", **kwargs):
            progress_calls.append((current, total))

        # Normally initialized by index_commits() before the embedder loop;
        # set directly here since this test drives _index_one_embedder().
        indexer._processed_shards = []

        with patch.object(
            indexer,
            "_index_shard_commits",
            fake_index_shard_commits.__get__(indexer),
        ):
            embedder_instance = Mock()
            embedder_instance.dimensions = 1024
            commits_processed, blobs, vectors = indexer._index_one_embedder(
                "voyage-context-4",
                embedder_instance,
                commits,
                vector_manager=Mock(),
                progress_callback=progress_callback,
            )

        assert commits_processed == 8
        assert len(progress_calls) == 8

        grand_total = 8
        # EVERY call must report the WHOLE-RUN total, never a shard-local
        # one (2, 5, or 1).
        totals_seen = {total for _current, total in progress_calls}
        assert totals_seen == {grand_total}, (
            f"BUG #1378: expected every progress_callback call to report "
            f"total={grand_total}, but saw totals {totals_seen} (shard-local "
            f"resets)"
        )

        # `current` must be monotonically non-decreasing across shard
        # boundaries and must never exceed grand_total.
        currents = [c for c, _t in progress_calls]
        assert currents == sorted(currents), (
            f"BUG #1378: current must never reset/decrease across shards: {currents}"
        )
        assert all(c <= grand_total for c in currents), (
            f"current must never exceed grand_total={grand_total}: {currents}"
        )
        # True completion: last call is exactly grand_total/grand_total.
        assert progress_calls[-1] == (grand_total, grand_total)
