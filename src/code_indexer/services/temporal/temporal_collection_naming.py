"""Provider-aware temporal collection naming.

Story #628: Replace hardcoded 'code-indexer-temporal' with provider-aware naming
so that different embedding providers store vectors in separate collections,
preventing cross-provider contamination.

Collection name format: code-indexer-temporal-{model_slug}
Legacy format (backward compat): code-indexer-temporal
"""

import logging
import re
from pathlib import Path
from typing import Any, List, Tuple

logger = logging.getLogger(__name__)

TEMPORAL_COLLECTION_PREFIX = "code-indexer-temporal-"
LEGACY_TEMPORAL_COLLECTION = "code-indexer-temporal"

_SUPPORTED_PROVIDERS = {"voyage-ai", "cohere"}


def sanitize_model_name(model_name: str) -> str:
    """Sanitize model name for use in collection names and filesystem paths.

    Lowercases the model name and replaces all characters outside [a-zA-Z0-9_]
    with underscores.

    Args:
        model_name: Embedding model name, e.g. 'voyage-code-3' or 'embed-v4.0'

    Returns:
        Sanitized slug, e.g. 'voyage_code_3' or 'embed_v4_0'
    """
    return re.sub(r"[^a-zA-Z0-9_]", "_", model_name.lower())


def resolve_temporal_collection_name(model_name: str) -> str:
    """Build a provider-aware temporal collection name from a model name.

    Sanitizes the model name by lowercasing it and replacing all characters
    outside [a-zA-Z0-9_] with underscores.

    Args:
        model_name: Embedding model name, e.g. 'voyage-code-3' or 'embed-v4.0'

    Returns:
        Collection name, e.g. 'code-indexer-temporal-voyage_code_3'
    """
    slug = sanitize_model_name(model_name)
    return f"{TEMPORAL_COLLECTION_PREFIX}{slug}"


def is_temporal_collection(collection_name: str) -> bool:
    """Return True if collection_name is a temporal collection (legacy or provider-aware).

    Args:
        collection_name: Collection name to test

    Returns:
        True for 'code-indexer-temporal' (legacy) and 'code-indexer-temporal-*' (provider-aware)
    """
    if not collection_name:
        return False
    return collection_name == LEGACY_TEMPORAL_COLLECTION or collection_name.startswith(
        TEMPORAL_COLLECTION_PREFIX
    )


def collection_display_name(collection_name: str) -> str:
    """Extract a short display name from a temporal collection name.

    Examples:
        "code-indexer-temporal-voyage_code_3" -> "voyage_code_3"
        "code-indexer-temporal-embed_v4_0" -> "embed_v4_0"
        "code-indexer-temporal" -> "temporal"
    """
    if collection_name.startswith(TEMPORAL_COLLECTION_PREFIX):
        return collection_name[len(TEMPORAL_COLLECTION_PREFIX) :]
    if collection_name == LEGACY_TEMPORAL_COLLECTION:
        return "temporal"
    return collection_name


def get_model_name_for_provider(provider_name: str, config) -> str:
    """Read the embedding model name from config for the given provider.

    Args:
        provider_name: Provider identifier, e.g. 'voyage-ai' or 'cohere'
        config: CIDXConfig instance with voyage_ai and cohere sub-configs

    Returns:
        Model name string

    Raises:
        ValueError: If provider_name is not a known provider
    """
    if provider_name == "voyage-ai":
        return str(config.voyage_ai.model)
    if provider_name == "cohere":
        return str(config.cohere.model)
    raise ValueError(
        f"Unknown provider '{provider_name}'. "
        f"Supported providers: {sorted(_SUPPORTED_PROVIDERS)}"
    )


def resolve_temporal_collection_from_config(config) -> str:
    """Convenience: resolve provider-aware temporal collection name from config.

    Reads `config.embedding_provider`, looks up the model, and returns the
    sanitized collection name.

    Args:
        config: CIDXConfig instance

    Returns:
        Provider-aware temporal collection name
    """
    model_name = get_model_name_for_provider(config.embedding_provider, config)
    return resolve_temporal_collection_name(model_name)


def get_temporal_collections(config, index_path: Path) -> List[Tuple[str, Path]]:
    """Enumerate temporal collection directories found on disk under index_path.

    Returns all subdirectories that are recognized as temporal (legacy or
    provider-aware) by is_temporal_collection().

    Args:
        config: CIDXConfig instance (reserved for future use)
        index_path: Directory containing collection subdirectories

    Returns:
        List of (collection_name, path) tuples for each temporal collection found.
        Returns empty list if index_path does not exist.
    """
    index_path = Path(index_path)
    if not index_path.exists():
        return []

    results: List[Tuple[str, Path]] = []
    for entry in sorted(index_path.iterdir()):
        if entry.is_dir() and is_temporal_collection(entry.name):
            results.append((entry.name, entry))
    return results


def clear_all_temporal_collections(index_path: Path, vector_store: Any) -> int:
    """Clear all temporal collections (configured + orphaned) for --force re-index.

    Uses glob enumeration as primary strategy to catch orphaned collections
    that may no longer match the current provider configuration.

    Args:
        index_path: Path to .code-indexer/index/ directory
        vector_store: FilesystemVectorStore instance

    Returns:
        Number of collections cleared
    """
    index_path = Path(index_path)
    if not index_path.is_dir():
        return 0

    cleared = 0
    for subdir in sorted(index_path.iterdir()):
        if not subdir.is_dir() or not is_temporal_collection(subdir.name):
            continue
        logger.info("Clearing temporal collection: %s", subdir.name)
        vector_store.clear_collection(collection_name=subdir.name)
        for fname in ("temporal_progress.json", "temporal_meta.json"):
            fpath = subdir / fname
            if fpath.exists():
                fpath.unlink()
        cleared += 1

    return cleared
