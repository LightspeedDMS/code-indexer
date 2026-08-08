from pathlib import Path

from code_indexer.server.services.temporal_legacy_migration.mover import (
    migrate_temporal_shards,
)


def test_empty_fixed_root_is_published_atomically_and_second_run_is_noop(
    tmp_path: Path,
):
    legacy = tmp_path / "repo" / ".code-indexer" / "index"
    fixed = tmp_path / ".temporal" / "repo"
    shard = legacy / "code-indexer-temporal-embedder-2026Q1"
    shard.mkdir(parents=True)
    (shard / "chunks.db").write_bytes(b"sqlite-data")
    (shard / "collection_meta.json").write_text('{"name":"q1"}')
    (shard / "hnsw_index.bin").write_bytes(b"hnsw-data")

    first = migrate_temporal_shards(legacy, fixed, relocation_enabled=True)
    assert first.published == 1
    assert (fixed / shard.name / "chunks.db").read_bytes() == b"sqlite-data"
    assert shard.exists()

    second = migrate_temporal_shards(legacy, fixed, relocation_enabled=True)
    assert second.published == 0
    assert second.already_complete == 1
    assert shard.exists()


def test_new_shard_wins_collision_and_cleanup_is_separate(tmp_path: Path):
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    legacy_shard = legacy / "code-indexer-temporal-e-2026Q1"
    fixed_shard = fixed / legacy_shard.name
    legacy_shard.mkdir(parents=True)
    fixed_shard.mkdir(parents=True)
    (legacy_shard / "chunks.db").write_bytes(b"old")
    (fixed_shard / "chunks.db").write_bytes(b"new")

    result = migrate_temporal_shards(legacy, fixed, relocation_enabled=True)
    assert result.already_complete == 1
    assert result.collisions == 0
    assert (fixed_shard / "chunks.db").read_bytes() == b"new"
    assert legacy_shard.exists()

    cleanup = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=False, cleanup_authorized=True
    )
    assert cleanup.deleted == 1
    assert not legacy_shard.exists()


def test_cleanup_never_happens_without_explicit_authorization(tmp_path: Path):
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    shard = legacy / "code-indexer-temporal-e-2026Q1"
    shard.mkdir(parents=True)
    (shard / "chunks.db").write_bytes(b"data")

    migrate_temporal_shards(legacy, fixed, relocation_enabled=True)
    result = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=False, cleanup_authorized=False
    )
    assert result.deleted == 0
    assert shard.exists()
