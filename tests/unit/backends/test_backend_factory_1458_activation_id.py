"""BackendFactory/FilesystemBackend thread activation_id through to
FilesystemVectorStore (Story #1458 AC11).

Real construction of BackendFactory.create() -> FilesystemBackend ->
get_vector_store_client() -> FilesystemVectorStore -- no mocking of any
class under test.
"""

from pathlib import Path

from code_indexer.backends.backend_factory import BackendFactory
from code_indexer.backends.filesystem_backend import FilesystemBackend
from code_indexer.config import Config


class TestBackendFactoryActivationIdThreading:
    def test_create_with_activation_id_threads_through_to_vector_store(
        self, tmp_path: Path
    ) -> None:
        config = Config(codebase_dir=str(tmp_path))
        backend = BackendFactory.create(
            config=config,
            project_root=tmp_path,
            activation_id="activation-uuid-123",
        )

        vector_store = backend.get_vector_store_client()

        assert vector_store.activation_id == "activation-uuid-123"

    def test_create_without_activation_id_defaults_to_none(
        self, tmp_path: Path
    ) -> None:
        config = Config(codebase_dir=str(tmp_path))
        backend = BackendFactory.create(config=config, project_root=tmp_path)

        vector_store = backend.get_vector_store_client()

        assert vector_store.activation_id is None


class TestFilesystemBackendActivationIdThreading:
    def test_get_vector_store_client_passes_activation_id(self, tmp_path: Path) -> None:
        backend = FilesystemBackend(
            project_root=tmp_path, activation_id="clone-generation-token"
        )

        vector_store = backend.get_vector_store_client()

        assert vector_store.activation_id == "clone-generation-token"

    def test_get_vector_store_client_defaults_to_none(self, tmp_path: Path) -> None:
        backend = FilesystemBackend(project_root=tmp_path)

        vector_store = backend.get_vector_store_client()

        assert vector_store.activation_id is None
