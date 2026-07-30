"""BackendFactory/FilesystemBackend thread use_chunks_db_for_new_collections
through to FilesystemVectorStore (Story #1488).

Real construction of BackendFactory.create() -> FilesystemBackend ->
get_vector_store_client() -> FilesystemVectorStore -- no mocking of any
class under test. This is the wiring that lets `cidx index
--new-collection-layout=chunks_db` (and the server's explicit
--new-collection-layout=chunks_db child arg) actually reach the store's
fresh-collection layout decision.
"""

from pathlib import Path

import pytest

from code_indexer.backends.backend_factory import BackendFactory
from code_indexer.backends.filesystem_backend import FilesystemBackend
from code_indexer.config import Config


class TestBackendFactoryLayoutThreading:
    @pytest.mark.parametrize("param, expected", [(True, True), (False, False)])
    def test_create_threads_explicit_layout_to_vector_store(
        self, tmp_path: Path, monkeypatch, param: bool, expected: bool
    ) -> None:
        monkeypatch.delenv("CIDX_CHUNKS_DB_NEW_COLLECTIONS", raising=False)
        config = Config(codebase_dir=str(tmp_path))

        backend = BackendFactory.create(
            config=config,
            project_root=tmp_path,
            use_chunks_db_for_new_collections=param,
        )
        vector_store = backend.get_vector_store_client()

        assert vector_store._use_chunks_db_for_new_collections is expected

    def test_create_without_layout_falls_back_to_env_default_false(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("CIDX_CHUNKS_DB_NEW_COLLECTIONS", raising=False)
        config = Config(codebase_dir=str(tmp_path))

        backend = BackendFactory.create(config=config, project_root=tmp_path)
        vector_store = backend.get_vector_store_client()

        assert vector_store._use_chunks_db_for_new_collections is False

    def test_create_without_layout_honors_truthy_env(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("CIDX_CHUNKS_DB_NEW_COLLECTIONS", "true")
        config = Config(codebase_dir=str(tmp_path))

        backend = BackendFactory.create(config=config, project_root=tmp_path)
        vector_store = backend.get_vector_store_client()

        assert vector_store._use_chunks_db_for_new_collections is True


class TestFilesystemBackendLayoutThreading:
    @pytest.mark.parametrize("param, expected", [(True, True), (False, False)])
    def test_get_vector_store_client_passes_explicit_layout(
        self, tmp_path: Path, monkeypatch, param: bool, expected: bool
    ) -> None:
        monkeypatch.delenv("CIDX_CHUNKS_DB_NEW_COLLECTIONS", raising=False)

        backend = FilesystemBackend(
            project_root=tmp_path, use_chunks_db_for_new_collections=param
        )
        vector_store = backend.get_vector_store_client()

        assert vector_store._use_chunks_db_for_new_collections is expected

    def test_get_vector_store_client_defaults_to_env_false(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("CIDX_CHUNKS_DB_NEW_COLLECTIONS", raising=False)

        backend = FilesystemBackend(project_root=tmp_path)
        vector_store = backend.get_vector_store_client()

        assert vector_store._use_chunks_db_for_new_collections is False
