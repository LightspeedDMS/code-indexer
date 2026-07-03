"""Provider-aware temporal collection naming.

Story #628: Replace hardcoded 'code-indexer-temporal' with provider-aware naming
so that different embedding providers store vectors in separate collections,
preventing cross-provider contamination.

Collection name format: code-indexer-temporal-{model_slug}
Legacy format (backward compat): code-indexer-temporal
"""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

TEMPORAL_COLLECTION_PREFIX = "code-indexer-temporal-"
LEGACY_TEMPORAL_COLLECTION = "code-indexer-temporal"

# Story #1290: marker filename kept as a local constant (decoupled from the
# now-deleted temporal_migration_service.py) so has_real_monolith() can still
# recognize a pre-hard-cut monolith left over on disk from an OLD deployment.
_MIGRATION_COMPLETE_MARKER = "migration_complete.marker"

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


_MONTH_TO_QUARTER = {
    1: 1,
    2: 1,
    3: 1,
    4: 2,
    5: 2,
    6: 2,
    7: 3,
    8: 3,
    9: 3,
    10: 4,
    11: 4,
    12: 4,
}


def quarter_suffix(commit_timestamp: datetime) -> str:
    """Return e.g. '2024Q3' for a datetime in 2024 Q3."""
    q = _MONTH_TO_QUARTER[commit_timestamp.month]
    return f"{commit_timestamp.year}Q{q}"


def get_shard_collection_name(model_name: str, commit_timestamp: datetime) -> str:
    """Return 'code-indexer-temporal-{model_slug}-{YYYY}Q{N}'."""
    base = resolve_temporal_collection_name(model_name)
    return f"{base}-{quarter_suffix(commit_timestamp)}"


def is_sharded_temporal_collection(collection_name: str) -> bool:
    """Return True iff the name ends with -{YYYY}Q{N} (e.g. -2024Q3)."""
    return bool(re.search(r"-\d{4}Q[1-4]$", collection_name))


def base_collection_name(shard_name: str) -> str:
    """Strip the quarter suffix from a sharded collection name.

    Returns the base (monolith) collection name by removing the trailing
    ``-YYYYQn`` segment.  Non-sharded names are returned unchanged.

    This is the single source of truth for the quarter-suffix strip pattern
    (``-\\d{4}Q[1-4]$``) shared by :func:`is_sharded_temporal_collection`
    and any caller that needs to derive the base collection from a shard name.

    Args:
        shard_name: e.g. ``'code-indexer-temporal-voyage_code_3-2024Q1'``

    Returns:
        Base name, e.g. ``'code-indexer-temporal-voyage_code_3'``.
        Unchanged when the name is not a sharded temporal collection.
    """
    return re.sub(r"-\d{4}Q[1-4]$", "", shard_name)


def get_quarter_range(year: int, quarter: int) -> Tuple[datetime, datetime]:
    """Return (start_inclusive, end_exclusive) for the given quarter in UTC.

    Q1: Jan1-Apr1, Q2: Apr1-Jul1, Q3: Jul1-Oct1, Q4: Oct1-(next year)Jan1
    """
    start_month = (quarter - 1) * 3 + 1
    start = datetime(year, start_month, 1, tzinfo=timezone.utc)
    if quarter == 4:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, start_month + 3, 1, tzinfo=timezone.utc)
    return start, end


def has_real_monolith(coll_dir: Path) -> bool:
    """Return True iff the directory contains a real monolithic HNSW that has not been migrated.

    A collection directory is considered a real monolith when ALL of:
    - ``hnsw_index.bin`` exists inside the directory (there is actual data to query)
    - ``migration_complete.marker`` does NOT exist (migration has not already run)

    This is the single source-of-truth predicate used by
    ``get_overlapping_shards()`` (query fan-out — Bug #1207 Fix 2). Story
    #1290: the migration/conversion system that used to also depend on this
    predicate has been removed as part of the per-commit hard cut; a legacy
    monolith is now handled by blank-out (hard-deleted for lacking a v2
    marker), not migrated.

    Args:
        coll_dir: Path to the collection directory (e.g. index/code-indexer-temporal-X/).

    Returns:
        True if the collection contains a real, not-yet-cleaned-up monolithic HNSW.
    """
    coll_dir = Path(coll_dir)
    if not coll_dir.is_dir():
        return False
    if (coll_dir / _MIGRATION_COMPLETE_MARKER).exists():
        return False
    return (coll_dir / "hnsw_index.bin").exists()


def get_overlapping_shards(
    model_name: str,
    index_path: Path,
    start: Optional[datetime],
    end: Optional[datetime],
) -> List[str]:
    """Return collection names whose date range overlaps [start, end].

    Includes quarterly shards that overlap the range AND the legacy monolithic
    collection if it exists on disk.  Returns shards in ascending chronological
    order (lexicographic on YYYYQN suffix), with legacy collection appended last.
    None start or end means open-ended (all time on that side).
    """
    base_name = resolve_temporal_collection_name(model_name)
    index_path = Path(index_path)
    if not index_path.exists():
        return []

    def _utc(dt: Optional[datetime]) -> Optional[datetime]:
        if dt is None:
            return None
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    norm_start = _utc(start)
    norm_end = _utc(end)

    shards: List[str] = []
    has_legacy = False

    for entry in index_path.iterdir():
        if not entry.is_dir():
            continue
        name = entry.name
        # Quarterly shard: base_name-YYYYQN
        m = re.match(rf"^{re.escape(base_name)}-(\d{{4}})Q([1-4])$", name)
        if m:
            year, quarter = int(m.group(1)), int(m.group(2))
            shard_start, shard_end = get_quarter_range(year, quarter)
            # Ranges overlap unless one ends at or before the other starts
            overlaps = True
            if norm_end is not None and norm_end <= shard_start:
                overlaps = False
            if norm_start is not None and norm_start >= shard_end:
                overlaps = False
            if overlaps:
                shards.append(name)
        elif name == base_name:
            # Include as legacy monolith ONLY when a real unmirgated HNSW is present.
            # Bug #1207 Fix 2: the original marker-absence-only check incorrectly included
            # the base dir after CLI sharding (when HNSW was never written there to begin
            # with, or was already deleted), causing spurious HNSW-stale warnings and
            # potential stale-read mixing.  has_real_monolith() requires hnsw_index.bin
            # to exist AND migration_complete.marker to be absent.
            if has_real_monolith(entry):
                has_legacy = True

    shards.sort()  # YYYYQN is lexicographically == chronologically
    if has_legacy:
        shards.append(base_name)
    return shards


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
