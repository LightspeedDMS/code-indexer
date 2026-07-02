"""Provider-specific index management service (Story #490).

Shared service layer for managing per-provider semantic indexes.
Called by MCP tools, REST endpoints, and CLI commands.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ProviderIndexService:
    """Manages provider-specific semantic index operations."""

    def __init__(self, config=None):
        """Initialize with optional server config."""
        self._config = config

    def list_providers(self) -> List[Dict[str, Any]]:
        """List configured embedding providers with valid API keys.

        Returns list of dicts with: name, display_name, default_model,
        supports_batch, api_key_env.
        """
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        config = self._get_config()
        configured = EmbeddingProviderFactory.get_configured_providers(config)
        provider_info = EmbeddingProviderFactory.get_provider_info()

        result = []
        for name in configured:
            info = provider_info.get(name, {})
            result.append(
                {
                    "name": name,
                    "display_name": info.get("name", name),
                    "default_model": info.get("default_model", "unknown"),
                    "supports_batch": info.get("supports_batch", False),
                    "api_key_env": info.get("api_key_env", ""),
                }
            )
        return result

    def get_provider_index_status(
        self, repo_path: str, repo_alias: str
    ) -> Dict[str, Any]:
        """Get per-provider index status for a repository.

        Returns dict keyed by provider name with:
        - exists: bool
        - vector_count: int
        - last_indexed: str (ISO timestamp) or None
        - collection_name: str
        - model: str
        - error: str (only present on failure)
        """
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        config = self._get_config()
        configured = EmbeddingProviderFactory.get_configured_providers(config)

        index_dir = Path(repo_path) / ".code-indexer" / "index"

        status: Dict[str, Any] = {}
        for provider_name in configured:
            try:
                provider = EmbeddingProviderFactory.create(
                    config, provider_name=provider_name
                )
                model_name = provider.get_current_model()

                # Use resolve_collection_name for correct directory name
                from code_indexer.backends.backend_factory import BackendFactory

                backend = BackendFactory.create(
                    config=config, project_root=Path(repo_path)
                )
                vs_client = backend.get_vector_store_client()
                collection_name = vs_client.resolve_collection_name(config, provider)

                collection_dir = index_dir / collection_name
                exists = (
                    collection_dir.exists() and any(collection_dir.iterdir())
                    if collection_dir.exists()
                    else False
                )

                vector_count = 0
                last_indexed = None
                if exists:
                    import json

                    # Check per-provider metadata first, then default
                    ci_dir = Path(repo_path) / ".code-indexer"
                    meta_file = ci_dir / f"metadata-{provider_name}.json"
                    if not meta_file.exists():
                        meta_file = ci_dir / "metadata.json"
                    if meta_file.exists():
                        with open(meta_file) as f:
                            meta = json.load(f)
                        vector_count = meta.get("chunks_indexed", 0)
                        last_indexed = meta.get("indexed_at")

                status[provider_name] = {
                    "exists": exists,
                    "vector_count": vector_count,
                    "last_indexed": last_indexed,
                    "collection_name": collection_name,
                    "model": model_name,
                }
            except Exception as e:
                logger.warning("Failed to check %s index status: %s", provider_name, e)
                status[provider_name] = {
                    "exists": False,
                    "vector_count": 0,
                    "last_indexed": None,
                    "collection_name": "",
                    "model": "",
                    "error": str(e),
                }

        return status

    def validate_provider(self, provider_name: str) -> Optional[str]:
        """Validate provider name against configured providers.

        Returns None if valid, error message string if invalid.
        """
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        config = self._get_config()
        configured = EmbeddingProviderFactory.get_configured_providers(config)

        if provider_name not in configured:
            available = ", ".join(configured) if configured else "none"
            return (
                f"Provider '{provider_name}' is not configured. "
                f"Available providers: {available}"
            )
        return None

    def remove_provider_index(
        self, repo_path: str, provider_name: str
    ) -> Dict[str, Any]:
        """Remove a provider's collection from a repository.

        Returns dict with: removed (bool), collection_name, message.
        """
        import shutil
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory
        from code_indexer.backends.backend_factory import BackendFactory

        config = self._get_config()
        provider = EmbeddingProviderFactory.create(config, provider_name=provider_name)

        backend = BackendFactory.create(config=config, project_root=Path(repo_path))
        vs_client = backend.get_vector_store_client()
        collection_name = vs_client.resolve_collection_name(config, provider)

        index_dir = Path(repo_path) / ".code-indexer" / "index"
        collection_dir = index_dir / collection_name

        if not collection_dir.exists():
            return {
                "removed": False,
                "collection_name": collection_name,
                "message": f"Collection '{collection_name}' does not exist",
            }

        shutil.rmtree(collection_dir)

        metadata_file = (
            Path(repo_path) / ".code-indexer" / f"metadata-{provider_name}.json"
        )
        if metadata_file.exists():
            metadata_file.unlink()

        return {
            "removed": True,
            "collection_name": collection_name,
            "message": f"Removed collection '{collection_name}' for {provider_name}",
        }

    def get_additional_configured_providers(self) -> List[str]:
        """Return non-primary providers with valid API keys configured on the server.

        Returns all configured providers except voyage-ai (the primary provider).
        Used to determine which additional provider indexes to build automatically.
        """
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        config = self._get_config()
        all_configured = EmbeddingProviderFactory.get_configured_providers(config)
        return [p for p in all_configured if p != "voyage-ai"]

    def _get_config(self):
        """Get or create config."""
        if self._config:
            return self._config
        from code_indexer.config import ConfigManager

        return ConfigManager().load()
