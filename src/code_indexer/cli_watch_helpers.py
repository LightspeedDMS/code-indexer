"""Watch mode helper: auto-detection of existing indexes (semantic, FTS,
temporal) for `cidx watch`.

Story: 02_Feat_WatchModeAutoDetection/01_Story_WatchModeAutoUpdatesAllIndexes.md
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Dict

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger(__name__)


def _any_temporal_collection_exists(index_base: Path, project_root: Path) -> bool:
    """Return True if any temporal collection directory exists under
    index_base, OR (GitHub Issue #1482 extension) this repo's temporal data
    lives at the golden-owned FIXED root outside the clone -- the ONE genuine
    standalone case where project_root structurally IS a golden repo's own
    clone. An ordinary standalone repo (the common case) has no such
    structure: resolve_golden_repo_coordinates() returns None and this falls
    back to the pre-existing local-scan-only result, unchanged.

    Bug #1529 note: the "sister location" this originally described, and the
    sister-root detection module it called, are both retired. The fixed root
    (`{golden_repos_dir}/.temporal/{alias}/`) is the only non-local location
    consulted today.
    """
    from .services.temporal.temporal_collection_naming import is_temporal_collection

    if index_base.exists() and any(
        entry.is_dir() and is_temporal_collection(entry.name)
        for entry in index_base.iterdir()
    ):
        return True

    try:
        # Bug #1529: the same structural golden-repo detection, now provided
        # by temporal_server_paths (the single authority on temporal data
        # location) instead of the retired sister-root-detection module.
        from .services.temporal.temporal_server_paths import (
            resolve_golden_repo_coordinates,
        )

        coordinates = resolve_golden_repo_coordinates(project_root)
        if coordinates is None:
            return False
        golden_repos_dir, repo_alias = coordinates

        from .services.temporal.temporal_status import get_temporal_repo_status

        status = get_temporal_repo_status(golden_repos_dir, repo_alias, index_base)
        return status.has_data
    except Exception:
        logger.warning(
            "_any_temporal_collection_exists: fixed-root temporal "
            "detection failed for %s (isolated, non-fatal); using "
            "local-scan-only result",
            project_root,
            exc_info=True,
        )
        return False


def semantic_collection_name(config: "Config") -> str:
    """The semantic collection `cidx index` writes for `config`: the
    configured provider's embedding model, made filesystem-safe exactly as
    FilesystemVectorStore.resolve_collection_name() does.

    Raises:
        ValueError: unknown embedding provider.
    """
    if config.embedding_provider == "voyage-ai":
        model = config.voyage_ai.model
    elif config.embedding_provider == "cohere":
        model = config.cohere.model
    else:
        raise ValueError(f"Unknown embedding provider: {config.embedding_provider}")
    return model.replace("/", "_").replace(":", "_")


def detect_existing_indexes(project_root: Path, config: "Config") -> Dict[str, bool]:
    """Detect which indexes exist and should be watched.

    Args:
        project_root: Path to project root directory
        config: The project's configuration (its embedding model names the
            semantic collection)

    Returns:
        Dict mapping index type to existence boolean:
        {
            "semantic": bool,  # .code-indexer/index/<embedding model>
            "fts": bool,       # .code-indexer/tantivy_index (meta.json)
            "temporal": bool,  # a temporal collection
        }
    """
    from .services.fts_lifecycle import fts_index_dir_for_repo

    index_base = project_root / ".code-indexer" / "index"

    return {
        "semantic": (index_base / semantic_collection_name(config)).is_dir(),
        "fts": (fts_index_dir_for_repo(project_root) / "meta.json").exists(),
        "temporal": _any_temporal_collection_exists(index_base, project_root),
    }
