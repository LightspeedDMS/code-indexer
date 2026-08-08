from pathlib import Path

from code_indexer.server.services.temporal_legacy_migration.mover import (
    migrate_temporal_shards,
)
from code_indexer.server.services.temporal_legacy_migration.verification import (
    VerificationError,
    verify_shard_copy,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore


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


def test_verifier_detects_field_corruption_after_copy(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    record = {"id": "p1", "vector": [1.0], "payload": {"text": "source"}}
    import json

    (source / "vector_p1.json").write_text(json.dumps(record))
    (target / "vector_p1.json").write_text(
        json.dumps({**record, "payload": {"text": "corrupt"}})
    )
    try:
        verify_shard_copy(source, target)
    except VerificationError:
        pass
    else:
        raise AssertionError("field corruption must fail verification")


def test_metadata_scope_is_copied_on_publish_and_deleted_only_on_cleanup(
    tmp_path: Path,
):
    class RealScopeBackend:
        def __init__(self):
            self.copied = []
            self.deleted = 0

        def copy_collection_scope(self, target_collection_path: Path) -> None:
            self.copied.append(target_collection_path)

        def delete_collection_scope(self) -> None:
            self.deleted += 1

    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    shard = legacy / "code-indexer-temporal-e-2026Q1"
    shard.mkdir(parents=True)
    (shard / "vector_p1.json").write_text('{"id":"p1","vector":[1]}')
    backend = RealScopeBackend()
    migrate_temporal_shards(
        legacy,
        fixed,
        relocation_enabled=True,
        metadata_backend_factory=lambda _: backend,
    )
    assert backend.copied == [fixed / shard.name]
    assert backend.deleted == 0
    migrate_temporal_shards(
        legacy,
        fixed,
        cleanup_authorized=True,
        metadata_backend_factory=lambda _: backend,
    )
    assert backend.deleted == 1


def test_verifier_compares_real_chunks_db_records(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    for root in (source, target):
        (root / "collection_meta.json").write_text('{"chunks_db":{"version":1}}')
        store = ChunkStore(root / "chunks.db")
        store.write_batch(
            [{"id": "p1", "vector": [1.0, 2.0], "payload": {"text": "ok"}}]
        )
        store.close()
    verify_shard_copy(source, target)
    target_store = ChunkStore(target / "chunks.db")
    target_store.write_batch(
        [{"id": "p1", "vector": [1.0, 2.0], "payload": {"text": "bad"}}]
    )
    target_store.close()
    try:
        verify_shard_copy(source, target)
    except VerificationError:
        pass
    else:
        raise AssertionError("chunk record corruption must fail verification")
