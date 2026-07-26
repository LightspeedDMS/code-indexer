"""AC6's three-branch publish dispatch (Story #1457).

Given `resolver.resolve(embedder_slug, quarter)`'s existing pointer-first /
in-repo-fallback-second decision (AC8), the SAME result determines which of
AC6's three build branches to take:
  - SISTER_POINTER  -> Branch A  (copy current + apply delta, then swap_alias)
  - IN_REPO_LEGACY  -> Branch B-bootstrap (build fresh from
                         [legacy_rows, new_delta], then create_alias)
  - None            -> Branch B-fresh (build fresh from [new_delta] only,
                         then create_alias)

Row-sourcing (reading actual legacy `vector_*.json` file content) is
deliberately injectable via `legacy_row_reader` -- a separate, pluggable
concern from the branch DECISION this dispatcher owns.

Real resolver, real AliasManager, real ChunkStore, real HNSW build -- no
mocking of the code under test.
"""

from __future__ import annotations

from unittest.mock import patch

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)
from code_indexer.services.temporal.temporal_refresh_dispatch import (
    execute_temporal_refresh_branch,
)
from code_indexer.services.temporal.temporal_shard_resolver import (
    TemporalShardResolver,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore


def _fake_records(count: int, prefix: str, dim: int = 4):
    return [
        {"id": f"{prefix}-{i}", "vector": [float(i)] * dim, "payload": {}}
        for i in range(count)
    ]


def _make_harness(tmp_path, repo_alias="evolution"):
    aliases_dir = tmp_path / "aliases"
    sister_root = tmp_path / "sister"
    legacy_index_path = tmp_path / "clone" / ".code-indexer" / "index"
    legacy_index_path.mkdir(parents=True)
    alias_manager = AliasManager(str(aliases_dir))
    resolver = TemporalShardResolver(
        alias_manager=alias_manager,
        repo_alias=repo_alias,
        sister_root=sister_root,
        legacy_index_path=legacy_index_path,
    )
    snapshot_manager = VersionedSnapshotManager(versioned_base=str(sister_root))
    return alias_manager, resolver, snapshot_manager, legacy_index_path, sister_root


def test_branch_b_fresh_when_nothing_exists(tmp_path):
    (
        alias_manager,
        resolver,
        snapshot_manager,
        _legacy_index_path,
        sister_root,
    ) = _make_harness(tmp_path)
    new_delta_rows = _fake_records(2, "delta")

    published_path = execute_temporal_refresh_branch(
        resolver=resolver,
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        repo_alias="evolution",
        sister_root=sister_root,
        embedder_slug="voyage_code_3",
        quarter="2024Q1",
        new_delta_rows=new_delta_rows,
        legacy_row_reader=lambda _path: [],
        vector_dim=4,
    )

    assert published_path.is_dir()
    assert alias_manager.read_alias("evolution-temporal-voyage_code_3-2024Q1") == str(
        published_path
    )

    store = ChunkStore(published_path / "chunks.db", immutable=True)
    try:
        assert store.count() == 2
    finally:
        store.close()


def test_branch_b_bootstrap_when_legacy_rows_exist_and_no_pointer(tmp_path):
    """No sister pointer, but the in-repo legacy shard has real committed
    rows -- Branch B-bootstrap consolidates BOTH the legacy rows AND the
    new delta into one fresh version, publishing via create_alias (first
    publish). No data loss: both historical and new rows must be present."""
    (
        alias_manager,
        resolver,
        snapshot_manager,
        legacy_index_path,
        sister_root,
    ) = _make_harness(tmp_path)

    legacy_shard_dir = legacy_index_path / "code-indexer-temporal-voyage_code_3-2024Q1"
    nested = legacy_shard_dir / "a"
    nested.mkdir(parents=True)
    (nested / "vector_legacy.json").write_text('{"point_id": "legacy-marker"}')

    legacy_rows_to_return = _fake_records(3, "legacy")
    new_delta_rows = _fake_records(2, "delta")

    published_path = execute_temporal_refresh_branch(
        resolver=resolver,
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        repo_alias="evolution",
        sister_root=sister_root,
        embedder_slug="voyage_code_3",
        quarter="2024Q1",
        new_delta_rows=new_delta_rows,
        legacy_row_reader=lambda _path: legacy_rows_to_return,
        vector_dim=4,
    )

    store = ChunkStore(published_path / "chunks.db", immutable=True)
    try:
        assert store.count() == 5  # 3 legacy + 2 delta, no data loss
        assert store.read("legacy-0") is not None
        assert store.read("delta-0") is not None
    finally:
        store.close()

    assert alias_manager.read_alias("evolution-temporal-voyage_code_3-2024Q1") == str(
        published_path
    )


def test_branch_a_when_pointer_already_exists(tmp_path):
    """Pointer EXISTS -- Branch A copies the CURRENT version and applies
    ONLY the new delta, publishing via swap_alias (subsequent publish).
    Historical rows from the first refresh must survive alongside the new
    delta -- and the OLD version's pointer value must genuinely change."""
    (
        alias_manager,
        resolver,
        snapshot_manager,
        _legacy_index_path,
        sister_root,
    ) = _make_harness(tmp_path)

    # First refresh (Branch B-fresh) establishes the pointer.
    first_delta = _fake_records(2, "first")
    first_version = execute_temporal_refresh_branch(
        resolver=resolver,
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        repo_alias="evolution",
        sister_root=sister_root,
        embedder_slug="voyage_code_3",
        quarter="2024Q1",
        new_delta_rows=first_delta,
        legacy_row_reader=lambda _path: [],
        vector_dim=4,
    )

    # Second refresh: pointer now exists -- must take Branch A.
    second_delta = _fake_records(1, "second")
    second_version = execute_temporal_refresh_branch(
        resolver=resolver,
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        repo_alias="evolution",
        sister_root=sister_root,
        embedder_slug="voyage_code_3",
        quarter="2024Q1",
        new_delta_rows=second_delta,
        legacy_row_reader=lambda _path: [],
        vector_dim=4,
    )

    assert second_version != first_version
    assert alias_manager.read_alias("evolution-temporal-voyage_code_3-2024Q1") == str(
        second_version
    )
    assert alias_manager.get_previous_path(
        "evolution-temporal-voyage_code_3-2024Q1"
    ) == str(first_version)

    store = ChunkStore(second_version / "chunks.db", immutable=True)
    try:
        assert store.count() == 3  # 2 from first refresh + 1 new delta
        assert store.read("first-0") is not None  # historical survived
        assert store.read("second-0") is not None  # new delta present
    finally:
        store.close()


def test_branch_a_forwards_force_rebuild_to_copy_and_extend(tmp_path):
    """Story #1457 HIGH #11 (2026-07-23 code review): a locally-repaired
    shard's `was_stale` signal must reach Branch A's
    `copy_and_extend_consolidated_temporal_version` call as `force_rebuild`
    -- otherwise a repair with zero new-commit delta rows republishes a
    stale sister HNSW index forever. `copy_and_extend_consolidated_temporal_version`
    itself already has its own dedicated unit coverage (rebuild-when-forced
    behavior) in test_temporal_consolidated_build_1457.py; this test proves
    only that the DISPATCH layer forwards the argument on the Branch A path."""
    (
        alias_manager,
        resolver,
        snapshot_manager,
        _legacy_index_path,
        sister_root,
    ) = _make_harness(tmp_path)

    # First refresh (Branch B-fresh) establishes the pointer.
    first_delta = _fake_records(2, "first")
    execute_temporal_refresh_branch(
        resolver=resolver,
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        repo_alias="evolution",
        sister_root=sister_root,
        embedder_slug="voyage_code_3",
        quarter="2024Q1",
        new_delta_rows=first_delta,
        legacy_row_reader=lambda _path: [],
        vector_dim=4,
    )

    # Second refresh: pointer now exists -- Branch A -- with force_rebuild
    # signalled and zero new delta rows (the exact locally-repaired-but-
    # commit-delta-empty scenario HIGH #11 targets).
    with patch(
        "code_indexer.services.temporal.temporal_refresh_dispatch"
        ".copy_and_extend_consolidated_temporal_version",
        wraps=None,
    ) as mock_copy_and_extend:
        mock_copy_and_extend.return_value = sister_root / "unused"
        (sister_root / "unused").mkdir(parents=True, exist_ok=True)
        execute_temporal_refresh_branch(
            resolver=resolver,
            snapshot_manager=snapshot_manager,
            alias_manager=alias_manager,
            repo_alias="evolution",
            sister_root=sister_root,
            embedder_slug="voyage_code_3",
            quarter="2024Q1",
            new_delta_rows=[],
            legacy_row_reader=lambda _path: [],
            vector_dim=4,
            force_rebuild=True,
        )

    assert mock_copy_and_extend.call_count == 1
    _, call_kwargs = mock_copy_and_extend.call_args
    assert call_kwargs.get("force_rebuild") is True, (
        "execute_temporal_refresh_branch must forward force_rebuild=True "
        "to copy_and_extend_consolidated_temporal_version on Branch A -- "
        f"got kwargs: {call_kwargs}"
    )
