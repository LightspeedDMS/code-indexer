"""Factory for creating vector storage backends."""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from .vector_store_backend import VectorStoreBackend
from .filesystem_backend import FilesystemBackend

if TYPE_CHECKING:
    from ..config import Config

logger = logging.getLogger(__name__)


class BackendFactory:
    """Factory for creating vector storage backend from configuration."""

    @staticmethod
    def create(
        config: "Config",
        project_root: Path,
        hnsw_cache: Optional[Any] = None,
        memory_governor: Optional[Any] = None,
        activation_id: Optional[str] = None,
        use_chunks_db_for_new_collections: Optional[bool] = None,
        index_dir: Optional[Path] = None,
    ) -> VectorStoreBackend:
        """Create appropriate backend from configuration.

        Args:
            config: Configuration object
            project_root: Root directory of the project being indexed
            hnsw_cache: Optional HNSW cache instance (server mode passes this)
            memory_governor: Optional MemoryGovernor for Story #1213 Story 3.
                Server mode passes get_memory_governor(); CLI leaves it None.
            activation_id: Story #1458 AC11 -- optional per-clone generation/
                identity token for an ACTIVATED repo query, threaded into
                FilesystemVectorStore's cache-key construction. None
                (default) for the CLI/solo/golden-repo path, preserving
                today's pure path-derived cache key byte-for-byte.
            use_chunks_db_for_new_collections: Story #1488 -- optional
                explicit new-collection chunk-storage layout choice
                (True=CHUNKS_DB, False=SHARDED_JSON) forwarded to the
                FilesystemBackend/FilesystemVectorStore. None (default)
                falls back to the CIDX_CHUNKS_DB_NEW_COLLECTIONS env var
                (default SHARDED_JSON), so every existing call site is
                byte-identical. Set by the CLI `--new-collection-layout`
                flag and the server's explicit spawn-site child arg.
            index_dir: Bug #1529 -- optional explicit index root, overriding
                `project_root/.code-indexer/index`. Used by the temporal read
                path, whose data lives at a fixed location OUTSIDE the
                queried repo's own tree. None (default) is byte-identical to
                before for every other caller.

        Returns:
            FilesystemBackend instance

        Raises:
            ValueError: If configuration is invalid
        """
        # ServerConfig has no vector_store attribute; default to filesystem (only supported backend)
        if not hasattr(config, "vector_store") or config.vector_store is None:
            logger.debug("Creating FilesystemBackend (no vector_store config)")
            return FilesystemBackend(
                project_root=project_root,
                hnsw_index_cache=hnsw_cache,
                memory_governor=memory_governor,
                activation_id=activation_id,
                use_chunks_db_for_new_collections=use_chunks_db_for_new_collections,
                index_dir=index_dir,
            )

        provider = config.vector_store.provider

        if provider == "filesystem":
            logger.debug("Creating FilesystemBackend")
            return FilesystemBackend(
                project_root=project_root,
                hnsw_index_cache=hnsw_cache,
                memory_governor=memory_governor,
                activation_id=activation_id,
                use_chunks_db_for_new_collections=use_chunks_db_for_new_collections,
                index_dir=index_dir,
            )
        else:
            raise ValueError(f"Unsupported vector store provider: {provider}")
