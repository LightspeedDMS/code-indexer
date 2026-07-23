"""Story #1456: production write-path wiring for CHUNKS_DB collections.

This is the mechanism that makes the story's consolidation NOT inert: a
FilesystemVectorStore instance can be told (explicitly via constructor
param, or via the CIDX_CHUNKS_DB_NEW_COLLECTIONS env var for the CLI/daemon
call sites that are never individually threaded through) to build FRESH
collections using the consolidated chunks.db layout instead of sharded
vector_*.json files. Default is OFF everywhere (byte-identical existing
behavior) -- this is an explicit opt-in, not a fleet-wide flip (Story #1460
owns the rollout decision).
"""

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore


class TestUseChunksDbForNewCollectionsConstructorFlag:
    def test_default_is_false(self, tmp_path):
        store = FilesystemVectorStore(base_path=tmp_path)
        assert store._use_chunks_db_for_new_collections is False

    def test_explicit_true_overrides_default(self, tmp_path):
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        assert store._use_chunks_db_for_new_collections is True

    def test_env_var_enables_when_not_explicitly_passed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CIDX_CHUNKS_DB_NEW_COLLECTIONS", "1")
        store = FilesystemVectorStore(base_path=tmp_path)
        assert store._use_chunks_db_for_new_collections is True

    def test_explicit_false_wins_over_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CIDX_CHUNKS_DB_NEW_COLLECTIONS", "1")
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=False
        )
        assert store._use_chunks_db_for_new_collections is False
