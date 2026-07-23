"""
Shared repository-health discovery + aggregation helper (Bug #1394).

Consolidates logic that was duplicated nearly verbatim between
repository_health.py::get_repository_health and activated_repos.py::get_health:

- CollectionHealthResult / RepositoryHealthResult response models.
- _to_collection_health_result: HealthCheckResult -> CollectionHealthResult mapping.
- discover_health_collections: scan a `.code-indexer/index` directory for every
  collection subdirectory containing an hnsw_index.bin, classifying its index
  type (semantic/temporal/multimodal).
- discover_incomplete_collections: scan the same directory for the partially
  built collections discover_health_collections cannot see -- vector shards on
  disk but no hnsw_index.bin (EVO-64245). They are reported unhealthy rather
  than silently skipped, which previously made a broken index look healthy.
- get_shared_health_service: ONE HNSWHealthService singleton (5-minute TTL)
  shared by both routers, replacing activated_repos.py's previous
  fresh-cache-less-instance-per-request pattern. HNSWHealthService guards its
  internal cache with its own threading.RLock (see hnsw_health_service.py),
  so sharing one instance across concurrent requests is already safe -- no
  additional synchronization is introduced here.
- compute_repository_health: the aggregation entry point used by both routers'
  GET handlers AND the new async job workers (Bug #1394 section 4).

This module does NOT raise HTTPException -- that stays the router's job
(404 resolution, path resolution, etc. happen in the router BEFORE calling
compute_repository_health).
"""

from pathlib import Path
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field

from code_indexer.services.hnsw_health_service import (
    HealthCheckResult,
    HNSWHealthService,
    check_health_batch,
)

# Bug #1394: shared health-check cache TTL, matching the 5-minute value both
# routers' previous separate HNSWHealthService instances already used.
HEALTH_CACHE_TTL_SECONDS = 300


class CollectionHealthResult(BaseModel):
    """Health result for a single collection/index."""

    collection_name: str = Field(description="Collection name (e.g., voyage-code-3)")
    index_type: str = Field(description="Index type: semantic, temporal, or multimodal")
    valid: bool = Field(description="Overall health status")
    file_exists: bool
    readable: bool
    loadable: bool
    element_count: Optional[int] = None
    connections_checked: Optional[int] = None
    min_inbound: Optional[int] = None
    max_inbound: Optional[int] = None
    orphan_count: Optional[int] = Field(
        None,
        description=(
            "Zero-tolerance orphan signal (Story #1359 AC4): 0 is OK, any "
            "value > 0 is ERROR (reflected in `valid`) -- no WARNING tier."
        ),
    )
    hnswlib_capability_available: Optional[bool] = Field(
        None,
        description=(
            "Bug #1415: True/False iff the installed hnswlib does/does not "
            "have the custom LightspeedDMS fork's check_integrity()/"
            "repair_orphans() methods. None if not evaluated. SEPARATE "
            "signal from orphan_count/valid -- never folded into that "
            "zero-tolerance binary."
        ),
    )
    file_size_bytes: Optional[int] = None
    errors: List[str] = Field(default_factory=list)
    check_duration_ms: float


class RepositoryHealthResult(BaseModel):
    """Aggregated health for all indexes in a repository."""

    repo_alias: str
    overall_healthy: bool = Field(description="True if ALL indexes are healthy")
    collections: List[CollectionHealthResult] = Field(default_factory=list)
    total_collections: int = 0
    healthy_count: int = 0
    unhealthy_count: int = 0
    from_cache: bool = False


def _to_collection_health_result(
    collection_name: str, index_type: str, health_result: HealthCheckResult
) -> CollectionHealthResult:
    """Map an HNSWHealthService HealthCheckResult onto the REST
    CollectionHealthResult model (Story #1359 AC4: propagates orphan_count
    unmodified -- `valid` remains the single zero-tolerance signal, no
    separate graded severity is introduced on this surface).
    """
    return CollectionHealthResult(
        collection_name=collection_name,
        index_type=index_type,
        valid=health_result.valid,
        file_exists=health_result.file_exists,
        readable=health_result.readable,
        loadable=health_result.loadable,
        element_count=health_result.element_count,
        connections_checked=health_result.connections_checked,
        min_inbound=health_result.min_inbound,
        max_inbound=health_result.max_inbound,
        orphan_count=health_result.orphan_count,
        hnswlib_capability_available=health_result.hnswlib_capability_available,
        file_size_bytes=health_result.file_size_bytes,
        errors=health_result.errors,
        check_duration_ms=health_result.check_duration_ms,
    )


def _classify_index_type(collection_name: str) -> str:
    """Classify a collection's index type from its directory name.

    Substring classification, exactly as both routers previously did:
    "temporal" in name.lower() -> "temporal", "multimodal" in name.lower() ->
    "multimodal", else "semantic".

    Args:
        collection_name: Collection directory name (e.g. "voyage-code-3").

    Returns:
        "temporal", "multimodal", or "semantic".
    """
    name_lower = collection_name.lower()
    if "temporal" in name_lower:
        return "temporal"
    if "multimodal" in name_lower:
        return "multimodal"
    return "semantic"


def collection_has_vector_shards(collection_dir: Path) -> bool:
    """Return True if the collection holds at least one vector shard.

    Vector shards (``vector_*.json``) are written incrementally during
    indexing and live in nested subdirectories, so rglob is required. Their
    presence means indexing populated the collection -- as opposed to a
    genuinely empty / never-indexed collection directory, which has none.

    Args:
        collection_dir: Path to a single collection directory.

    Returns:
        True if any vector_*.json shard exists anywhere under collection_dir.
    """
    return next(collection_dir.rglob("vector_*.json"), None) is not None


def discover_health_collections(
    index_base_path: Path,
) -> List[Tuple[str, str, Path]]:
    """Scan an index base directory for every collection with an hnsw_index.bin.

    Args:
        index_base_path: Path to `.code-indexer/index` directory.

    Returns:
        List of (collection_name, index_type, hnsw_file_path) tuples, sorted
        by collection_name for deterministic ordering.

    Raises:
        ValueError: If index_base_path is None.
    """
    if index_base_path is None:
        raise ValueError("index_base_path must not be None")

    if not (index_base_path.exists() and index_base_path.is_dir()):
        return []

    discovered: List[Tuple[str, str, Path]] = []
    for collection_dir in sorted(index_base_path.iterdir(), key=lambda p: p.name):
        if not collection_dir.is_dir():
            continue

        hnsw_file = collection_dir / "hnsw_index.bin"
        if not hnsw_file.exists():
            continue

        discovered.append(
            (
                collection_dir.name,
                _classify_index_type(collection_dir.name),
                hnsw_file,
            )
        )

    return discovered


def discover_incomplete_collections(index_base_path: Path) -> List[Path]:
    """Scan for collections that hold vector shards but no HNSW graph.

    Such a collection is partially built: indexing wrote the shards but was
    interrupted (OOM/crash/timeout) before ``hnsw_index.bin`` was renamed into
    place. It is populated yet permanently unqueryable until rebuilt.
    discover_health_collections() skips it -- it has no hnsw_index.bin to check
    -- so on its own the repository reports healthy-with-zero-collections, a
    false green that hides a broken index.

    A collection directory with neither shards nor a graph is genuinely empty /
    never indexed and is NOT returned here, so this does not false-alarm.

    Args:
        index_base_path: Path to `.code-indexer/index` directory.

    Returns:
        Collection directories with shards but no hnsw_index.bin, sorted by
        name for deterministic ordering.

    Raises:
        ValueError: If index_base_path is None.
    """
    if index_base_path is None:
        raise ValueError("index_base_path must not be None")

    if not (index_base_path.exists() and index_base_path.is_dir()):
        return []

    incomplete: List[Path] = []
    for collection_dir in sorted(index_base_path.iterdir(), key=lambda p: p.name):
        if not collection_dir.is_dir():
            continue
        if (collection_dir / "hnsw_index.bin").exists():
            continue
        if collection_has_vector_shards(collection_dir):
            incomplete.append(collection_dir)

    return incomplete


def build_incomplete_collection_result(
    collection_dir: Path,
) -> CollectionHealthResult:
    """Build the unhealthy result for a partially-built collection.

    There is no graph to load, so every liveness flag is False and the errors
    list carries the rebuild instruction.

    Args:
        collection_dir: A collection directory with shards but no HNSW graph.

    Returns:
        A CollectionHealthResult with valid=False and a clear rebuild reason.
    """
    return CollectionHealthResult(
        collection_name=collection_dir.name,
        index_type=_classify_index_type(collection_dir.name),
        valid=False,
        file_exists=False,
        readable=False,
        loadable=False,
        # No graph exists, so there is nothing to count orphans in -- None
        # (unknown), not 0 (checked and clean).
        orphan_count=None,
        # No graph exists, so no capability check was even attempted (Bug
        # #1415) -- None (not evaluated), distinct from True/False.
        hnswlib_capability_available=None,
        errors=[
            "Vector shards present but HNSW graph missing (hnsw_index.bin) — "
            "indexing was interrupted before the graph was built. "
            "Rebuild needed: cidx index --rebuild-index"
        ],
        check_duration_ms=0.0,
    )


# Bug #1394: ONE shared HNSWHealthService instance with the 5-minute TTL
# cache, reused by BOTH repository_health.py and activated_repos.py routers.
# Previously activated_repos.py built a fresh, cache-less HNSWHealthService()
# per request -- wasteful, no caching benefit at all. HNSWHealthService's
# cache access is already guarded internally by its own threading.RLock, so
# sharing this single instance across concurrent request-handling threads is
# safe without any additional synchronization here.
_shared_health_service = HNSWHealthService(cache_ttl_seconds=HEALTH_CACHE_TTL_SECONDS)


def get_shared_health_service() -> HNSWHealthService:
    """Return the shared, process-wide HNSWHealthService singleton (5-min TTL)."""
    return _shared_health_service


def _empty_repository_health_result(repo_alias: str) -> RepositoryHealthResult:
    """Build the empty, overall_healthy=True result shared by every
    no-collections-found path in compute_repository_health (missing/non-dir
    index_base_path, and an index dir with zero discovered collections).
    """
    return RepositoryHealthResult(
        repo_alias=repo_alias,
        overall_healthy=True,
        collections=[],
        total_collections=0,
        healthy_count=0,
        unhealthy_count=0,
        from_cache=False,
    )


def compute_repository_health(
    repo_alias: str,
    index_base_path: Path,
    health_service: HNSWHealthService,
    *,
    force_refresh: bool = False,
    max_workers: int = 4,
) -> RepositoryHealthResult:
    """Discover and aggregate health for every collection in a repository.

    Shared aggregation entry point used by both routers' GET handlers AND the
    new async job workers (Bug #1394). Does not raise HTTPException -- 404
    resolution and path resolution are the router's responsibility.

    Args:
        repo_alias: Repository alias to embed in the result. Must be a
            non-empty string.
        index_base_path: Path to `.code-indexer/index` directory.
        health_service: HNSWHealthService instance to use for checks.
        force_refresh: If True, bypass cache for every collection check.
        max_workers: Maximum concurrent health checks (passed through to
            check_health_batch).

    Returns:
        RepositoryHealthResult with per-collection health and aggregated
        status. If index_base_path doesn't exist/isn't a directory, or no
        collections are discovered, returns an empty, overall_healthy=True
        result (matches both routers' pre-existing empty-dir behavior).

    Raises:
        ValueError: If repo_alias is empty, index_base_path is None, or
            health_service is None.
    """
    if not repo_alias:
        raise ValueError("repo_alias must be a non-empty string")
    if index_base_path is None:
        raise ValueError("index_base_path must not be None")
    if health_service is None:
        raise ValueError("health_service must not be None")

    if not (index_base_path.exists() and index_base_path.is_dir()):
        return _empty_repository_health_result(repo_alias)

    discovered = discover_health_collections(index_base_path)
    incomplete = discover_incomplete_collections(index_base_path)
    if not discovered and not incomplete:
        return _empty_repository_health_result(repo_alias)

    collections: List[CollectionHealthResult] = []
    any_from_cache = False

    if discovered:
        batch_results = check_health_batch(
            health_service,
            [str(path) for _name, _index_type, path in discovered],
            force_refresh=force_refresh,
            max_workers=max_workers,
        )
        for collection_name, index_type, hnsw_file in discovered:
            health_result = batch_results[str(hnsw_file)]
            if health_result.from_cache:
                any_from_cache = True
            collections.append(
                _to_collection_health_result(collection_name, index_type, health_result)
            )

    # A partially-built collection has no graph to health-check, so it never
    # reaches check_health_batch; report it directly as unhealthy instead of
    # letting it vanish from the result (see discover_incomplete_collections).
    collections.extend(
        build_incomplete_collection_result(collection_dir)
        for collection_dir in incomplete
    )
    collections.sort(key=lambda c: c.collection_name)

    healthy_count = sum(1 for c in collections if c.valid)
    unhealthy_count = len(collections) - healthy_count
    overall_healthy = unhealthy_count == 0

    return RepositoryHealthResult(
        repo_alias=repo_alias,
        overall_healthy=overall_healthy,
        collections=collections,
        total_collections=len(collections),
        healthy_count=healthy_count,
        unhealthy_count=unhealthy_count,
        from_cache=any_from_cache,
    )
