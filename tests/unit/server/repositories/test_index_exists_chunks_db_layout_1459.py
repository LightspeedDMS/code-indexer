"""Tests for GoldenRepoManager._index_exists() CHUNKS_DB layout-awareness
(Issue #1459 AC1/AC5).

Bug: both the "semantic" and "temporal" branches of ``_index_exists`` used a
bare ``rglob("*.json")`` (or ``rglob("*.json")`` per collection) as an
existence check. ``collection_meta.json`` always exists in every collection
directory (consolidated or not) and alone satisfies that glob -- so the
check kept returning a misleading True even for a collection with ZERO real
chunk rows, and never actually inspected the new consolidated ``chunks.db``
store. This is a false POSITIVE bug (not a False/break).

Fix: dispatch through the canonical ``resolve_chunk_layout`` resolver -- for
CHUNKS_DB collections, check real row presence via ``ChunkStore.count()``;
for SHARDED_JSON collections, check specifically for ``vector_*.json`` files
(never a bare ``*.json`` glob).

All fixtures use real on-disk SQLite (``ChunkStore``) and real JSON files.
No method on the class under test (``GoldenRepoManager``) is mocked: a real
``GoldenRepo`` model is pre-populated into the real in-memory
``golden_repos`` cache dict so the genuine, unmocked ``get_actual_repo_path``
resolves the fixture path.
"""

from pathlib import Path

from code_indexer.storage.shared.chunk_layout import write_chunks_db_discriminator
from code_indexer.storage.sqlite_chunk_store import ChunkStore

_TEST_VECTOR_DIM = 4
_TEST_VECTOR = [0.1, 0.2, 0.3, 0.4]
_NONZERO_ROW_COUNT_SEMANTIC = 3
_NONZERO_ROW_COUNT_TEMPORAL = 2
_ZERO_ROW_COUNT = 0
_LEGACY_SHARD_POINT_ID = "point-legacy-0001"
_LEGACY_SHARD_VECTOR = [0.1]
_TEST_ALIAS = "test-repo"
_TEST_REPO_URL = "https://example.invalid/repo.git"
_TEST_CREATED_AT = "2026-01-01T00:00:00Z"


def _make_manager_with_real_repo(repo_dir: Path):
    """Build a real GoldenRepoManager whose golden_repos cache already
    contains a real GoldenRepo pointing at repo_dir, so the genuine,
    unmocked get_actual_repo_path() resolves it via Priority-1 metadata-path
    lookup (no filesystem/backend calls beyond os.path.exists/realpath).
    """
    from code_indexer.server.repositories.golden_repo_manager import (
        GoldenRepo,
        GoldenRepoManager,
    )

    manager = GoldenRepoManager.__new__(GoldenRepoManager)
    manager.golden_repos_dir = str(repo_dir.parent)
    manager.golden_repos = {}

    golden_repo = GoldenRepo(
        alias=_TEST_ALIAS,
        repo_url=_TEST_REPO_URL,
        default_branch="main",
        clone_path=str(repo_dir),
        created_at=_TEST_CREATED_AT,
    )
    manager.golden_repos[_TEST_ALIAS] = golden_repo
    return manager, golden_repo


def _write_base_collection_meta(coll_dir: Path) -> None:
    coll_dir.mkdir(parents=True, exist_ok=True)
    (coll_dir / "collection_meta.json").write_text(
        f'{{"name": "x", "vector_size": {_TEST_VECTOR_DIM}}}'
    )


def _make_chunks_db_collection(coll_dir: Path, *, row_count: int) -> None:
    """Build a real CHUNKS_DB-layout collection with `row_count` real rows."""
    _write_base_collection_meta(coll_dir)
    store = ChunkStore(coll_dir / "chunks.db")
    try:
        if row_count:
            store.write_batch(
                [{"id": f"point-{i}", "vector": _TEST_VECTOR} for i in range(row_count)]
            )
    finally:
        store.close()
    write_chunks_db_discriminator(coll_dir)


def _flag_chunks_db_without_real_file(coll_dir: Path) -> None:
    """Write the CHUNKS_DB discriminator into collection_meta.json WITHOUT
    creating a real chunks.db -- the exact "flagged but missing" crash-
    window state Issue #1459 remediation Finding 2 guards against."""
    from code_indexer.storage.shared.chunk_layout import (
        write_chunks_db_discriminator,
    )

    _write_base_collection_meta(coll_dir)
    write_chunks_db_discriminator(coll_dir)


class TestIndexExistsChunksDbMissingFileNoSideEffect:
    """Issue #1459 remediation Finding 2: a read-only status probe must
    never CREATE a missing chunks.db as a side effect."""

    def test_missing_chunks_db_does_not_create_file(self, tmp_path):
        from code_indexer.server.repositories.golden_repo_manager import (
            _collection_has_real_chunk_data,
        )

        repo_dir = tmp_path / "repo"
        coll = repo_dir / ".code-indexer" / "index" / "code-indexer-voyage-code-3-d1024"
        _flag_chunks_db_without_real_file(coll)
        db_path = coll / "chunks.db"
        assert not db_path.exists()

        result = _collection_has_real_chunk_data(coll)

        assert result is False
        assert not db_path.exists(), (
            "_collection_has_real_chunk_data must not create chunks.db as "
            "a side effect of a read-only status check"
        )


class TestIndexExistsSemanticChunksDb:
    def test_semantic_chunks_db_with_real_rows_returns_true(self, tmp_path):
        repo_dir = tmp_path / "repo"
        coll = repo_dir / ".code-indexer" / "index" / "code-indexer-voyage-code-3-d1024"
        _make_chunks_db_collection(coll, row_count=_NONZERO_ROW_COUNT_SEMANTIC)

        manager, golden_repo = _make_manager_with_real_repo(repo_dir)

        assert manager._index_exists(golden_repo, "semantic") is True

    def test_semantic_chunks_db_empty_returns_false(self, tmp_path):
        """CHUNKS_DB collection with ZERO rows: collection_meta.json alone
        must NOT trigger a false positive."""
        repo_dir = tmp_path / "repo"
        coll = repo_dir / ".code-indexer" / "index" / "code-indexer-voyage-code-3-d1024"
        _make_chunks_db_collection(coll, row_count=_ZERO_ROW_COUNT)

        manager, golden_repo = _make_manager_with_real_repo(repo_dir)

        assert manager._index_exists(golden_repo, "semantic") is False

    def test_semantic_sharded_json_metadata_only_returns_false(self, tmp_path):
        """SHARDED_JSON collection with only collection_meta.json (no
        vector_*.json shards): must NOT be a false positive either."""
        repo_dir = tmp_path / "repo"
        coll = repo_dir / ".code-indexer" / "index" / "code-indexer-voyage-code-3-d1024"
        _write_base_collection_meta(coll)
        # No chunks_db discriminator written -> resolves SHARDED_JSON.
        # No vector_*.json shard written.

        manager, golden_repo = _make_manager_with_real_repo(repo_dir)

        assert manager._index_exists(golden_repo, "semantic") is False

    def test_semantic_sharded_json_with_real_shard_returns_true(self, tmp_path):
        """Regression: legacy SHARDED_JSON collection with a real
        vector_*.json shard still detects correctly."""
        repo_dir = tmp_path / "repo"
        coll = repo_dir / ".code-indexer" / "index" / "code-indexer-voyage-code-3-d1024"
        _write_base_collection_meta(coll)
        (coll / "vector_0001.json").write_text(
            f'{{"id": "{_LEGACY_SHARD_POINT_ID}", "vector": {_LEGACY_SHARD_VECTOR}}}'
        )

        manager, golden_repo = _make_manager_with_real_repo(repo_dir)

        assert manager._index_exists(golden_repo, "semantic") is True

    def test_semantic_absent_returns_false(self, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True, exist_ok=True)

        manager, golden_repo = _make_manager_with_real_repo(repo_dir)

        assert manager._index_exists(golden_repo, "semantic") is False


class TestIndexExistsTemporalChunksDb:
    def test_temporal_chunks_db_with_real_rows_returns_true(self, tmp_path):
        repo_dir = tmp_path / "repo"
        coll = repo_dir / ".code-indexer" / "index" / "code-indexer-temporal-voyage_3"
        _make_chunks_db_collection(coll, row_count=_NONZERO_ROW_COUNT_TEMPORAL)

        manager, golden_repo = _make_manager_with_real_repo(repo_dir)

        assert manager._index_exists(golden_repo, "temporal") is True

    def test_temporal_chunks_db_empty_returns_false(self, tmp_path):
        repo_dir = tmp_path / "repo"
        coll = repo_dir / ".code-indexer" / "index" / "code-indexer-temporal-voyage_3"
        _make_chunks_db_collection(coll, row_count=_ZERO_ROW_COUNT)

        manager, golden_repo = _make_manager_with_real_repo(repo_dir)

        assert manager._index_exists(golden_repo, "temporal") is False

    def test_temporal_sharded_json_metadata_only_returns_false(self, tmp_path):
        """collection_meta.json alone (no vector_*.json) must not be a false
        positive for the temporal branch either."""
        repo_dir = tmp_path / "repo"
        coll = repo_dir / ".code-indexer" / "index" / "code-indexer-temporal-voyage_3"
        _write_base_collection_meta(coll)

        manager, golden_repo = _make_manager_with_real_repo(repo_dir)

        assert manager._index_exists(golden_repo, "temporal") is False

    def test_temporal_sharded_json_with_real_shard_returns_true(self, tmp_path):
        """Regression: legacy SHARDED_JSON temporal collection with a real
        vector_*.json shard still detects correctly."""
        repo_dir = tmp_path / "repo"
        coll = repo_dir / ".code-indexer" / "index" / "code-indexer-temporal-voyage_3"
        _write_base_collection_meta(coll)
        (coll / "vector_0001.json").write_text(
            f'{{"id": "{_LEGACY_SHARD_POINT_ID}", "vector": {_LEGACY_SHARD_VECTOR}}}'
        )

        manager, golden_repo = _make_manager_with_real_repo(repo_dir)

        assert manager._index_exists(golden_repo, "temporal") is True

    def test_temporal_absent_returns_false(self, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True, exist_ok=True)

        manager, golden_repo = _make_manager_with_real_repo(repo_dir)

        assert manager._index_exists(golden_repo, "temporal") is False
