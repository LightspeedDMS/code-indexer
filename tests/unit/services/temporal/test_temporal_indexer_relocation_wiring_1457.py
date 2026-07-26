"""TemporalIndexer._index_one_embedder wiring to
maybe_relocate_shard_to_sister_location (Story #1457 AC1 relocation
trigger).

maybe_relocate_shard_to_sister_location()'s OWN behavior is already
proven correct with real infra (test_temporal_relocation_trigger_1457.py).
This file proves ONLY the WIRING: _index_one_embedder calls it, once per
finalized quarter shard, with the correct (codebase_dir, shard_name,
local_shard_dir, new_commit_hashes, vector_dim) arguments -- following the
established collaborator-boundary-mock pattern from
test_temporal_embedder_override_server_wiring_1291.py and this file's own
sibling test_temporal_whole_run_progress_1378.py (which already patches
_index_shard_commits, a real external collaborator, to drive
_index_one_embedder's OWN orchestration logic for real).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import Mock, patch


def _make_commit(hash_val: str, year: int, month: int, day: int):
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


def _make_indexer(tmp_path):
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

    index_dir = tmp_path / ".code-indexer" / "index"
    mock_vector_store = Mock()
    mock_vector_store.project_root = tmp_path
    mock_vector_store.base_path = index_dir
    mock_vector_store.collection_exists.return_value = True
    mock_vector_store.load_id_index.return_value = set()
    mock_vector_store.begin_indexing.return_value = None
    mock_vector_store.end_indexing.return_value = {"status": "ok"}
    mock_vector_store.upsert_points.return_value = None
    # Real Path (not a bare Mock) so _use_stale_barrier's isinstance check
    # takes the durable-barrier branch, matching real production shape.
    mock_vector_store._get_collection_path.side_effect = (
        lambda name, *a, **kw: index_dir / name
    )

    base_collection = "code-indexer-temporal-voyage_code_3"
    indexer = TemporalIndexer(
        mock_config_manager, mock_vector_store, collection_name=base_collection
    )
    return indexer, index_dir


def _fake_index_shard_commits(self_ref, shard_commits, vec_manager, prog_cb, reconcile):
    return len(shard_commits), len(shard_commits), len(shard_commits) * 2


def test_relocation_trigger_called_once_per_shard_with_correct_args(tmp_path):
    indexer, index_dir = _make_indexer(tmp_path)
    indexer._processed_shards = []

    commits = [
        _make_commit("q1a", 2024, 1, 10),
        _make_commit("q2a", 2024, 4, 1),
    ]

    from code_indexer.services.temporal.temporal_incremental_gate import (
        bucket_commits_by_shard,
    )

    shard_commit_map = bucket_commits_by_shard(commits, "voyage-context-4")

    # Pre-create each shard's on-disk directory -- begin_indexing is a
    # no-op Mock here, so it never creates the real directory
    # _ensure_shard_has_projection_matrix needs to write into.
    for shard_name in shard_commit_map:
        (index_dir / shard_name).mkdir(parents=True, exist_ok=True)

    with (
        patch.object(
            indexer,
            "_index_shard_commits",
            _fake_index_shard_commits.__get__(indexer),
        ),
        patch(
            "code_indexer.services.temporal.temporal_indexer."
            "maybe_relocate_shard_to_sister_location"
        ) as mock_relocate,
    ):
        embedder_instance = Mock()
        embedder_instance.dimensions = 1024
        indexer._index_one_embedder(
            "voyage-context-4",
            embedder_instance,
            shard_commit_map,
            vector_manager=Mock(),
            progress_callback=None,
        )

    assert mock_relocate.call_count == len(shard_commit_map), (
        f"expected one relocation-trigger call per shard "
        f"({len(shard_commit_map)}), got {mock_relocate.call_count}"
    )

    called_shard_names = set()
    for call in mock_relocate.call_args_list:
        kwargs = call.kwargs
        assert kwargs["codebase_dir"] == tmp_path
        assert kwargs["local_shard_dir"] == index_dir / kwargs["shard_name"]
        assert kwargs["vector_dim"] == 1024
        expected_hashes = {c.hash for c in shard_commit_map[kwargs["shard_name"]]}
        assert set(kwargs["new_commit_hashes"]) == expected_hashes
        called_shard_names.add(kwargs["shard_name"])

    assert called_shard_names == set(shard_commit_map.keys())


def test_force_rebuild_forwarded_true_when_shard_was_stale(tmp_path):
    """Story #1457 HIGH #11 (2026-07-23 code review): the REAL was_stale
    signal (Bug #1407's HNSWIndexManager.is_stale(), unmocked -- a virgin
    shard directory with no collection_meta.json defaults to stale=True)
    must reach maybe_relocate_shard_to_sister_location as force_rebuild.

    The SUT here is `_index_one_embedder`'s stale-barrier orchestration
    (was_stale computation via the REAL HNSWIndexManager, and its
    forwarding into the relocation-trigger call) -- NOT
    `_index_shard_commits`, a different method entirely (the per-commit
    embedding/write engine). Patching `_index_shard_commits` reuses this
    file's OWN already-established collaborator-boundary-mock pattern
    (see `test_relocation_trigger_called_once_per_shard_with_correct_args`
    above and the module docstring: "already patches _index_shard_commits,
    a real external collaborator, to drive _index_one_embedder's OWN
    orchestration logic for real")."""
    indexer, index_dir = _make_indexer(tmp_path)
    indexer._processed_shards = []

    commits = [_make_commit("q1a", 2024, 1, 10)]

    from code_indexer.services.temporal.temporal_incremental_gate import (
        bucket_commits_by_shard,
    )

    shard_commit_map = bucket_commits_by_shard(commits, "voyage-context-4")
    shard_name = next(iter(shard_commit_map))

    # Virgin shard directory -- no collection_meta.json -- the REAL
    # HNSWIndexManager.is_stale() defaults True for a never-built shard.
    (index_dir / shard_name).mkdir(parents=True, exist_ok=True)

    with (
        patch.object(
            indexer,
            "_index_shard_commits",
            _fake_index_shard_commits.__get__(indexer),
        ),
        patch(
            "code_indexer.services.temporal.temporal_indexer."
            "maybe_relocate_shard_to_sister_location"
        ) as mock_relocate,
    ):
        embedder_instance = Mock()
        embedder_instance.dimensions = 1024
        indexer._index_one_embedder(
            "voyage-context-4",
            embedder_instance,
            shard_commit_map,
            vector_manager=Mock(),
            progress_callback=None,
        )

    assert mock_relocate.call_count == 1
    kwargs = mock_relocate.call_args.kwargs
    assert kwargs["force_rebuild"] is True, (
        "a virgin (never-built) shard is stale by HNSWIndexManager."
        "is_stale()'s own default -- was_stale must be forwarded as "
        f"force_rebuild=True, got kwargs: {kwargs}"
    )


def test_force_rebuild_forwarded_false_when_shard_was_not_stale(tmp_path):
    """Story #1457 HIGH #11 (2026-07-23 code review): a shard that was NOT
    stale before this run (real collection_meta.json with
    hnsw_index.is_stale=False, the REAL HNSWIndexManager.is_stale() read
    path) must forward force_rebuild=False -- proving this is a genuine
    pass-through of was_stale, not a hardcoded True.

    Same collaborator-boundary-mock pattern as the sibling test above and
    `test_relocation_trigger_called_once_per_shard_with_correct_args`
    (patches `_index_shard_commits`, a different method from the SUT under
    test here -- the stale-barrier orchestration in `_index_one_embedder`)."""
    indexer, index_dir = _make_indexer(tmp_path)
    indexer._processed_shards = []

    commits = [_make_commit("q1a", 2024, 1, 10)]

    from code_indexer.services.temporal.temporal_incremental_gate import (
        bucket_commits_by_shard,
    )

    shard_commit_map = bucket_commits_by_shard(commits, "voyage-context-4")
    shard_name = next(iter(shard_commit_map))

    shard_path = index_dir / shard_name
    shard_path.mkdir(parents=True, exist_ok=True)
    import json

    (shard_path / "collection_meta.json").write_text(
        json.dumps({"hnsw_index": {"is_stale": False, "vector_count": 0}})
    )

    with (
        patch.object(
            indexer,
            "_index_shard_commits",
            _fake_index_shard_commits.__get__(indexer),
        ),
        patch(
            "code_indexer.services.temporal.temporal_indexer."
            "maybe_relocate_shard_to_sister_location"
        ) as mock_relocate,
    ):
        embedder_instance = Mock()
        embedder_instance.dimensions = 1024
        indexer._index_one_embedder(
            "voyage-context-4",
            embedder_instance,
            shard_commit_map,
            vector_manager=Mock(),
            progress_callback=None,
        )

    assert mock_relocate.call_count == 1
    kwargs = mock_relocate.call_args.kwargs
    assert kwargs["force_rebuild"] is False, (
        "a shard that was NOT stale before this run must forward "
        f"force_rebuild=False, got kwargs: {kwargs}"
    )
