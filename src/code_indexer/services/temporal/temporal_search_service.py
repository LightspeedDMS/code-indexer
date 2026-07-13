"""TemporalSearchService - Temporal queries with time-range filtering.

Provides semantic search with temporal filtering capabilities:
- Time-range queries using JSON payloads (no SQLite)
- Diff-based temporal indexing support
- Performance-optimized batch queries
- Query-time git reconstruction for added/deleted files
"""

import time
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any, cast
from dataclasses import dataclass

# GitBlobReader removed - diff-based indexing doesn't use blob reading

logger = logging.getLogger(__name__)

# Default "all time" range used when no time filtering is desired
# This range represents minimal temporal filtering (1970-2100)
ALL_TIME_RANGE = ("1970-01-01", "2100-12-31")


def parse_date_range(date_range: str) -> Tuple[str, str]:
    """Parse and validate a date range string.

    Args:
        date_range: Date range string in format YYYY-MM-DD..YYYY-MM-DD

    Returns:
        Tuple of (start_date, end_date) as validated strings

    Raises:
        ValueError: If date range format or dates are invalid
    """
    if ".." not in date_range:
        raise ValueError(
            "Time range must use '..' separator (format: YYYY-MM-DD..YYYY-MM-DD)"
        )

    parts = date_range.split("..")
    if len(parts) != 2:
        raise ValueError(
            "Time range must use '..' separator (format: YYYY-MM-DD..YYYY-MM-DD)"
        )

    start_date, end_date = parts

    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        raise ValueError("Invalid date format. Use YYYY-MM-DD (e.g., 2023-01-01)")

    if start_date != start_dt.strftime("%Y-%m-%d") or end_date != end_dt.strftime(
        "%Y-%m-%d"
    ):
        raise ValueError(
            "Invalid date format. Use YYYY-MM-DD with zero-padded month/day (e.g., 2023-01-01)"
        )

    if end_dt < start_dt:
        raise ValueError("End date must be after start date")

    return start_date, end_date


def resolve_commit_timestamp(project_root: Path, ref: str) -> int:
    """Resolve a git ref/commit hash to its commit's UNIX timestamp (Bug #1301).

    Implements `at_commit` point-in-time scoping: the caller uses the
    returned timestamp as an upper bound on `commit_timestamp`, the exact
    same mechanism `time_range`'s upper bound already uses (see
    `query_temporal`'s `at_commit_ts` parameter). This function performs
    the VALIDATION half of the fix -- a ref/hash that cannot be resolved
    to a real commit in this repository raises ValueError instead of being
    silently accepted (Bug #1301: previously a bogus `at_commit` returned
    HTTP 200 with the full unfiltered result set).

    Args:
        project_root: Repository working directory (subprocess cwd)
        ref: Commit hash (full or abbreviated) or git ref/branch name

    Returns:
        The resolved commit's UNIX timestamp (seconds), as reported by
        `git show -s --format=%ct`.

    Raises:
        ValueError: If `ref` cannot be resolved to a commit in this
            repository (non-existent hash/ref, git failure, timeout).
    """
    try:
        rev_parse = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            cwd=project_root,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise ValueError(f"at_commit '{ref}' could not be resolved: {exc}") from exc

    resolved_hash = rev_parse.stdout.strip()
    if rev_parse.returncode != 0 or not resolved_hash:
        raise ValueError(
            f"at_commit '{ref}' does not resolve to a commit in this repository"
        )

    try:
        show_ts = subprocess.run(
            ["git", "show", "-s", "--format=%ct", resolved_hash],
            cwd=project_root,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise ValueError(
            f"at_commit '{ref}' resolved to {resolved_hash} but its commit "
            f"timestamp could not be read: {exc}"
        ) from exc

    ts_text = show_ts.stdout.strip()
    if show_ts.returncode != 0 or not ts_text:
        raise ValueError(
            f"at_commit '{ref}' resolved to {resolved_hash} but its commit "
            f"timestamp could not be read"
        )

    try:
        return int(ts_text)
    except ValueError as exc:
        raise ValueError(
            f"at_commit '{ref}' resolved to {resolved_hash} but returned a "
            f"non-numeric commit timestamp {ts_text!r}"
        ) from exc


@dataclass
class TemporalSearchResult:
    """Single temporal search result with temporal context."""

    file_path: str
    chunk_index: int
    content: str
    score: float
    metadata: Dict[str, Any]
    temporal_context: Dict[str, Any]
    # Fusion fields (Story #633) — Optional with defaults to avoid breaking existing callers
    temporal_chunk_id: Optional[str] = None
    source_provider: Optional[str] = None
    fusion_score: Optional[float] = None
    contributing_providers: Optional[List[str]] = None


@dataclass
class TemporalSearchResults:
    """Complete temporal search results with metadata."""

    results: List[TemporalSearchResult]
    query: str
    filter_type: str
    filter_value: Any
    total_found: int = 0
    performance: Optional[Dict[str, float]] = None
    warning: Optional[str] = None


class TemporalSearchService:
    """Service for temporal semantic search with date filtering."""

    def __init__(
        self,
        config_manager,
        project_root: Path,
        vector_store_client=None,
        embedding_provider=None,
        collection_name: Optional[str] = None,
    ):
        """Initialize temporal search service.

        Args:
            config_manager: ConfigManager instance
            project_root: Project root directory
            vector_store_client: Vector store client (FilesystemVectorStore or FilesystemClient), optional for checking index
            embedding_provider: Embedding provider for generating query embeddings, optional for checking index
            collection_name: Collection name for vector search, optional for checking index
        """
        self.config_manager = config_manager
        self.project_root = Path(project_root)
        self.temporal_dir = self.project_root / ".code-indexer" / "index" / "temporal"
        # commits_db_path removed - Story 2: No SQLite, all data from JSON payloads
        self.vector_store_client = vector_store_client
        self.embedding_provider = embedding_provider
        # Ensure collection_name is always a string (empty string if None)
        self.collection_name = collection_name or ""

    def _get_file_path_from_payload(
        self, payload: Dict[str, Any], default: str = "unknown"
    ) -> str:
        """Get file path from payload, checking 'path', 'file_path', and
        'primary_path' fields.

        Story #1290: per-commit aggregated payloads no longer carry 'path'
        or 'file_path' (only 'paths'/'primary_path' — a chunk can span
        multiple files), so 'primary_path' is checked as a further fallback.
        Legacy 'path'/'file_path' precedence is unchanged for backward
        compatibility with any pre-hard-cut payload still on disk.

        Args:
            payload: Payload dictionary from vector search result
            default: Default value if none of the fields exist

        Returns:
            File path string, preferring 'path' > 'file_path' > 'primary_path'
        """
        return str(
            payload.get("path")
            or payload.get("file_path")
            or payload.get("primary_path", default)
        )

    def has_temporal_index(self) -> bool:
        """Check if temporal index exists.

        Story 2: With diff-based indexing, check for temporal collection
        instead of commits.db (which no longer exists).

        Returns:
            True if temporal collection exists
        """
        # Story 2: Check for temporal collection instead of commits.db
        if self.vector_store_client:
            return bool(
                self.vector_store_client.collection_exists(self.collection_name)
            )
        return False

    def _validate_date_range(self, date_range: str) -> Tuple[str, str]:
        """Validate and parse date range format. Delegates to module-level parse_date_range."""
        return parse_date_range(date_range)

    def _calculate_over_fetch_multiplier(self, limit: int) -> int:
        """Calculate smart over-fetch multiplier based on limit size.

        Strategy:
        - Small limits (1-5): Need high headroom → 20x multiplier
        - Medium limits (6-10): Moderate headroom → 15x multiplier
        - Large limits (11-20): Less headroom → 10x multiplier
        - Very large limits (21+): Minimal headroom → 5x multiplier

        Rationale:
        - Temporal filtering removes results that fall outside date range
        - Removed code filtering further reduces results
        - Smaller limits need proportionally more over-fetch to ensure enough results
        - Larger limits already fetch many results, less multiplicative headroom needed

        Args:
            limit: User-requested result limit

        Returns:
            Over-fetch multiplier (5x to 20x)
        """
        if limit <= 5:
            return 20  # Small limits: high headroom
        elif limit <= 10:
            return 15  # Medium limits: moderate headroom
        elif limit <= 20:
            return 10  # Large limits: lower headroom
        else:
            return 5  # Very large limits: minimal headroom

    def _reconstruct_temporal_content(self, metadata: Dict[str, Any]) -> str:
        """Reconstruct content from git for added/deleted files.

        This method completes the storage optimization by reconstructing file content
        from git at query time for added/deleted files that use pointer-based storage
        (88% storage reduction).

        Args:
            metadata: Payload metadata with reconstruct_from_git marker

        Returns:
            Reconstructed file content or error message
        """
        # Check if reconstruction needed
        if not metadata.get("reconstruct_from_git"):
            return ""

        diff_type = metadata.get("diff_type")
        # Handle both 'path' and 'file_path' keys (different parts of the system use different names)
        path = metadata.get("path") or metadata.get("file_path", "")

        if diff_type == "added":
            # Fetch from commit where file was added
            commit_hash = metadata["commit_hash"]
            cmd = ["git", "show", f"{commit_hash}:{path}"]

        elif diff_type == "deleted":
            # Fetch from parent commit (before deletion)
            parent = metadata.get("parent_commit_hash")
            if not parent:
                return "[Content unavailable - parent commit not tracked]"
            cmd = ["git", "show", f"{parent}:{path}"]

        else:
            # Shouldn't happen but graceful fallback
            return ""

        # Execute git show
        # Story #1170: add timeout=30 to prevent hung git processes from blocking
        # the server thread indefinitely.
        try:
            result_proc = subprocess.run(
                cmd,
                cwd=self.project_root,
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "git reconstruction timed out after 30s for path=%s diff_type=%s",
                metadata.get("path") or metadata.get("file_path", "unknown"),
                metadata.get("diff_type", "unknown"),
            )
            return "[Content unavailable - git reconstruction timed out]"

        if result_proc.returncode == 0:
            return result_proc.stdout
        else:
            # Graceful error handling - truncate stderr to avoid log spam
            error_msg = (
                result_proc.stderr[:100] if result_proc.stderr else "unknown error"
            )
            return f"[Content unavailable - git error: {error_msg}]"

    def query_temporal(
        self,
        query: str,
        time_range: Tuple[str, str],
        diff_types: Optional[List[str]] = None,
        author: Optional[str] = None,
        limit: int = 10,
        min_score: Optional[float] = None,
        language: Optional[List[str]] = None,
        exclude_language: Optional[List[str]] = None,
        path_filter: Optional[List[str]] = None,
        exclude_path: Optional[List[str]] = None,
        chunk_type: Optional[str] = None,
        no_embedding_cache_shortcut: bool = False,
        at_commit_ts: Optional[int] = None,
        precomputed_query_vector: Optional[List[float]] = None,
    ) -> TemporalSearchResults:
        """Execute temporal semantic search with time-range filtering.

        Args:
            query: Search query text
            time_range: Tuple of (start_date, end_date) in YYYY-MM-DD format
            diff_types: Filter by diff type(s) (e.g., ["added", "modified", "deleted"])
            limit: Maximum results to return
            min_score: Minimum similarity score
            language: Filter by language(s) (e.g., ["python", "javascript"])
            exclude_language: Exclude language(s) (e.g., ["markdown"])
            path_filter: Filter by path pattern(s) (e.g., ["src/*"])
            exclude_path: Exclude path pattern(s) (e.g., ["*/tests/*"])
            at_commit_ts: (Bug #1301) Optional pre-resolved UNIX timestamp of
                the `at_commit` ref (resolved via `resolve_commit_timestamp`
                by the caller, which also validates the ref). Tightens the
                commit_timestamp upper bound to
                min(time_range end, at_commit_ts) -- i.e. scopes results to
                commits AT or BEFORE the given commit. Never widens the
                time_range bound.
            precomputed_query_vector: (Story #1293 S1b A5) Optional
                pre-computed embedding vector, supplied by
                execute_temporal_query_with_fusion's compute-once reuse seam
                when querying multiple sequential shards of the SAME
                embedder. When set, embedding is skipped entirely (FSV and
                non-FSV paths alike) -- mirrors the omni fan-out's
                precomputed_query_vector reuse.

        Returns:
            TemporalSearchResults with filtered results
        """
        # Ensure dependencies are available
        if not self.vector_store_client or not self.embedding_provider:
            raise RuntimeError(
                "TemporalSearchService not fully initialized. "
                "Vector store client and embedding provider required for queries."
            )

        # Build filter conditions using same logic as regular semantic search
        from ...services.language_mapper import LanguageMapper
        from ...services.path_filter_builder import PathFilterBuilder

        filter_conditions: Dict[str, Any] = {}

        # Language inclusion filters
        if language:
            language_mapper = LanguageMapper()
            must_conditions = []
            for lang in language:
                language_filter = language_mapper.build_language_filter(lang)
                must_conditions.append(language_filter)
            if must_conditions:
                filter_conditions["must"] = must_conditions

        # Path inclusion filters
        if path_filter:
            for path_pattern in path_filter:
                filter_conditions.setdefault("must", []).append(
                    {"key": "path", "match": {"text": path_pattern}}
                )

        # Language exclusion filters
        if exclude_language:
            language_mapper = LanguageMapper()
            must_not_conditions = []
            for exclude_lang in exclude_language:
                extensions = language_mapper.get_extensions(exclude_lang)
                for ext in extensions:
                    must_not_conditions.append(
                        {"key": "language", "match": {"value": ext}}
                    )
            if must_not_conditions:
                filter_conditions["must_not"] = must_not_conditions

        # Path exclusion filters
        if exclude_path:
            path_filter_builder = PathFilterBuilder()
            path_exclusion_filters = path_filter_builder.build_exclusion_filter(
                list(exclude_path)
            )
            if path_exclusion_filters.get("must_not"):
                if "must_not" in filter_conditions:
                    filter_conditions["must_not"].extend(
                        path_exclusion_filters["must_not"]
                    )
                else:
                    filter_conditions["must_not"] = path_exclusion_filters["must_not"]

        # Add time range filter to filter_conditions (Phase 3: Temporal Filter Migration)
        start_ts = int(datetime.strptime(time_range[0], "%Y-%m-%d").timestamp())
        end_ts = int(
            datetime.strptime(time_range[1], "%Y-%m-%d")
            .replace(hour=23, minute=59, second=59)
            .timestamp()
        )
        # Bug #1301: at_commit_ts tightens (never widens) the upper bound --
        # scopes results to commits AT or BEFORE the resolved at_commit ref.
        if at_commit_ts is not None:
            end_ts = min(end_ts, at_commit_ts)
        filter_conditions.setdefault("must", []).append(
            {"key": "commit_timestamp", "range": {"gte": start_ts, "lte": end_ts}}
        )

        # Story #1290: diff_type is a legacy per-file-diff concept -- see the
        # documented no-op note below (kept close to the chunk_type block).
        # Intentionally NOT added to filter_conditions.

        # Add author filter if specified (Phase 3: Temporal Filter Migration)
        if author:
            filter_conditions.setdefault("must", []).append(
                {"key": "author_name", "match": {"contains": author.lower()}}
            )

        # Story #1290 AC12: canonical chunk_type mapping. Every per-commit
        # chunk carries `type == "commit_chunk"` -- the OLD per-file-diff
        # `type` values ("commit_message"/"commit_diff") no longer exist in
        # the payload, so this is validated and applied as an is_head-based
        # POST-filter in _filter_by_time_range, not a vector-store payload
        # match on `type` (which would now match nothing).
        if chunk_type is not None and chunk_type not in (
            "commit_message",
            "commit_diff",
        ):
            raise ValueError(
                f"chunk_type must be 'commit_message' or 'commit_diff', "
                f"got {chunk_type!r}"
            )

        # Story #1290: diff_type is a legacy per-file-diff concept that no
        # longer exists on per-commit aggregated payloads (a single chunk can
        # span multiple files with different diff kinds). Filtering by it is
        # now a documented no-op -- NOT applied at the vector-store layer
        # (which would silently zero out every result) and NOT applied as a
        # post-filter either (see _filter_by_time_range).

        # Phase 1: Semantic search (over-fetch for filtering headroom)
        start_time = time.time()

        # Smart limit optimization with chunk_type-specific multipliers
        #
        # Post-filters (applied after vector search):
        # - Time range filtering: Narrow ranges filter out results aggressively
        # - Diff type filtering: Filters by modification type (added/modified/deleted)
        # - Author filtering: Filters by commit author
        # - Chunk type filtering: Filters by commit_message vs diff (HIGHLY SELECTIVE)
        #
        # Vector distribution in temporal collections:
        # - commit_message: ~2.7% of vectors
        # - commit_diff: ~97.3% of vectors
        #
        # Chunk type filtering requires distribution-aware multipliers.
        is_all_time = time_range == ALL_TIME_RANGE

        # CHUNK_TYPE-SPECIFIC MULTIPLIER (HIGH PRIORITY)
        if chunk_type == "commit_message":
            # Commit messages are rare (~2.7%), need high over-fetch
            multiplier = 40
            search_limit = limit * multiplier
            logger.debug(
                f"[DEBUG] chunk_type=commit_message, limit={limit}, "
                f"multiplier={multiplier}x, search_limit={search_limit}"
            )
        elif chunk_type == "commit_diff":
            # Diff chunks are majority (~97.3%), minimal over-fetch needed
            search_limit = int(limit * 1.5)
            logger.debug(
                f"[DEBUG] chunk_type=commit_diff, limit={limit}, "
                f"multiplier=1.5x, search_limit={search_limit}"
            )
        elif diff_types or author or not is_all_time:
            # Other post-filters: use existing logic
            multiplier = self._calculate_over_fetch_multiplier(limit)
            search_limit = limit * multiplier
            logger.debug(
                f"[DEBUG] post_filters (no chunk_type), limit={limit}, "
                f"multiplier={multiplier}, search_limit={search_limit}"
            )
        else:
            # No post-filters AND "all" time range: use exact limit
            search_limit = limit
            logger.debug(f"[DEBUG] no post_filters, using exact limit={limit}")

        # Execute vector search using the same pattern as regular query command
        from ...storage.filesystem_vector_store import FilesystemVectorStore

        if isinstance(self.vector_store_client, FilesystemVectorStore):
            # Parallel execution: embedding generation + index loading happen concurrently
            # Always request timing for consistent return type handling.
            # Story #1293 S1b [A5]: when precomputed_query_vector is supplied
            # (compute-once reuse seam across sequential shards), FSV skips
            # generate_embedding() entirely and uses the supplied vector.
            search_result = self.vector_store_client.search(
                query=query,  # Pass query text for parallel embedding
                embedding_provider=self.embedding_provider,  # Provider for parallel execution
                filter_conditions=filter_conditions,  # Apply user-specified filters (language, path, etc.)
                limit=search_limit,  # Smart limit: exact or multiplied based on filters
                collection_name=self.collection_name,
                return_timing=True,
                lazy_load=True,  # Enable lazy loading with early exit optimization
                prefetch_limit=search_limit,  # Use calculated over-fetch limit
                precomputed_query_vector=precomputed_query_vector,
            )
            # Type: Tuple[List[Dict[str, Any]], Dict[str, Any]] when return_timing=True
            raw_results, _timing_info = search_result  # type: ignore
        elif precomputed_query_vector is not None:
            # Story #1293 S1b [A5]: non-FSV backend reuse -- the caller already
            # computed (and emitted an event for) this vector once; skip the
            # embedding call and ctx write entirely for this shard.
            raw_results = self.vector_store_client.search(
                query_vector=precomputed_query_vector,
                filter_conditions=filter_conditions,
                limit=search_limit,
                collection_name=self.collection_name,
            )
        else:
            # FilesystemVectorStore: pre-compute embedding (no parallel support yet).
            # Bug #1078: gate through concurrency governor to cap concurrent provider calls.
            from code_indexer.server.services.governed_call import (
                coalesced_query_embedding,
            )

            query_embedding, _embed_meta = coalesced_query_embedding(
                self.embedding_provider,
                query,
                embedding_purpose="query",
                no_embedding_cache_shortcut=no_embedding_cache_shortcut,
            )
            # Story #1159: write embedding-cache metadata to the active
            # SearchEventContext so cache fields are recorded for temporal queries.
            try:
                from code_indexer.server.services.search_event_context import (
                    _search_event_ctx,
                )

                _temporal_event_ctx = _search_event_ctx.get(None)
                if _temporal_event_ctx is not None:
                    _pname = self.embedding_provider.get_provider_name().lower()
                    if "cohere" in _pname:
                        _temporal_event_ctx.cohere_cache_hit = _embed_meta.key_found
                        _temporal_event_ctx.cohere_cache_mode = _embed_meta.cache_mode
                        _temporal_event_ctx.cohere_latency_ms = (
                            _embed_meta.provider_latency_ms
                        )
                    else:
                        _temporal_event_ctx.voyage_cache_hit = _embed_meta.key_found
                        _temporal_event_ctx.voyage_cache_mode = _embed_meta.cache_mode
                        _temporal_event_ctx.voyage_latency_ms = (
                            _embed_meta.provider_latency_ms
                        )
            except Exception as _tsc_exc:  # noqa: BLE001
                logger.warning(
                    "search_event_log: temporal path failed to write embed_meta to ctx: %s",
                    _tsc_exc,
                )
            raw_results = self.vector_store_client.search(
                query_vector=query_embedding,
                filter_conditions=filter_conditions,  # Apply user-specified filters (language, path, etc.)
                limit=search_limit,  # Smart limit: exact or multiplied based on filters
                collection_name=self.collection_name,
            )

        semantic_time = time.time() - start_time
        logger.debug(f"[DEBUG] Vector search returned {len(raw_results)} raw_results")

        if not raw_results:
            return TemporalSearchResults(
                results=[],
                query=query,
                filter_type="time_range",
                filter_value=time_range,
                performance={
                    "semantic_search_ms": semantic_time * 1000,
                    "temporal_filter_ms": 0,
                    "blob_fetch_ms": 0,
                    "total_ms": semantic_time * 1000,
                },
            )

        # Phase 2: Transform results (content reconstruction, filtering)
        # Note: Time range, diff_type, and author filters applied in vector store,
        # but we apply them again here as post-filters for safety and test compatibility
        filter_start = time.time()
        # Type assertion: raw_results is guaranteed to be List[Dict[str, Any]] at this point
        temporal_results, blob_fetch_time_ms = self._filter_by_time_range(
            semantic_results=cast(List[Dict[str, Any]], raw_results),
            start_date=time_range[0],
            end_date=time_range[1],
            min_score=min_score,
            diff_types=diff_types,
            author=author,
            chunk_type=chunk_type,
            at_commit_ts=at_commit_ts,
        )
        filter_time = time.time() - filter_start

        # Story #1290 AC10: coalesce ALL hits for the same commit BEFORE any
        # result-limit truncation, then dedup-by-commit (max-scoring chunk as
        # top_chunk, paths[] unioned across every retained chunk). Operating
        # on the FULL filtered set (not yet limited) is what guarantees a
        # commit whose only matching chunk ranks low in the raw retrieval
        # order still survives into the final results.
        from .temporal_fusion import dedup_by_commit

        deduped_results = dedup_by_commit(temporal_results)

        # Bug #1380: per-candidate git-show message reconstruction REMOVED.
        # It was 95-98% of wall-clock time on real indexes (65-93s warm
        # queries) for a value dedup_by_commit() already stashes for free.
        # Non-head winners now source their message from the group's
        # short-capped head-chunk message (_head_commit_message, stashed by
        # dedup_by_commit whenever the head chunk was co-retrieved in the
        # same over-fetched batch; empty string when it wasn't -- falls back
        # to the non-head chunk's own always-empty commit_message per AC5).
        # message_truncated is unconditionally True for non-head winners:
        # there is no full-message reconstruction path anymore, so this is
        # never the complete message. Clients needing the full message can
        # resolve it themselves from commit_hash.
        for result in deduped_results:
            is_head = bool(result.metadata.get("is_head"))
            if is_head:
                result.temporal_context["message_truncated"] = False
                continue

            result.temporal_context["commit_message"] = result.metadata.get(
                "_head_commit_message"
            ) or result.metadata.get("commit_message", "")
            result.temporal_context["message_truncated"] = True

        # Phase 3 (Bug #1299): truncate by RELEVANCE (score) first, then
        # re-sort ONLY the selected top-`limit` subset reverse
        # chronologically for display (newest to oldest, like git log --
        # AC13: this is the default order when no rerank is applied).
        #
        # Previously this sorted the ENTIRE deduped candidate set by
        # commit_timestamp and truncated to `limit` afterward, which made
        # truncation recency-based instead of relevance-based: a
        # highly-relevant OLDER commit could be dropped in favor of several
        # weakly-relevant NEWER commits, purely because they were newer.
        total_found = len(deduped_results)
        top_results = sorted(
            deduped_results,
            key=lambda r: r.score,
            reverse=True,  # Highest relevance first
        )[:limit]
        top_results = sorted(
            top_results,
            key=lambda r: r.temporal_context.get("commit_timestamp", 0),
            reverse=True,  # Newest first (display order)
        )

        return TemporalSearchResults(
            results=top_results,
            query=query,
            filter_type="time_range",
            filter_value=time_range,
            total_found=total_found,
            performance={
                "semantic_search_ms": semantic_time * 1000,
                "temporal_filter_ms": filter_time * 1000,
                "blob_fetch_ms": blob_fetch_time_ms,
                "total_ms": (semantic_time + filter_time) * 1000,
            },
        )

    def _fetch_match_content(self, payload: Dict[str, Any]) -> str:
        """Fetch content based on match type.

        Story 2: No blob fetching - content comes from payload directly.

        Args:
            payload: Match payload with content

        Returns:
            Content string for display
        """
        match_type = payload.get("type", "file_chunk")

        if match_type == "file_chunk":
            # Story 2: Content is in payload, not fetched from blobs
            content = payload.get("content", "")
            if content:
                return str(content)

            # Check if binary file
            file_path = self._get_file_path_from_payload(payload, "")
            file_ext = Path(file_path).suffix.lower()
            binary_extensions = {
                ".png",
                ".jpg",
                ".jpeg",
                ".gif",
                ".pdf",
                ".zip",
                ".tar",
                ".gz",
                ".so",
                ".dylib",
                ".dll",
                ".exe",
            }
            if file_ext in binary_extensions:
                return f"[Binary file - {file_ext}]"

            # Fallback if no content in payload
            return "[Content not available]"

        elif match_type == "commit_message":
            # Fetch commit message from SQLite
            commit_hash = payload.get("commit_hash", "")
            char_start = payload.get("char_start", 0)
            char_end = payload.get("char_end", 0)

            try:
                commit_details = self._fetch_commit_details(commit_hash)
                if not commit_details:
                    return "[Commit message not found]"

                # Extract chunk of commit message
                message = str(commit_details["message"])
                if char_end > 0:
                    return message[char_start:char_end]
                else:
                    return message

            except Exception as e:
                logger.warning(f"Failed to fetch commit message {commit_hash[:7]}: {e}")
                return f"[⚠️ Commit message not found - {commit_hash[:7]}]"

        elif match_type == "commit_diff":
            # Story 2: Handle diff-based payloads
            # For now, return a placeholder indicating the diff type
            diff_type = payload.get("diff_type", "unknown")
            file_path = self._get_file_path_from_payload(payload, "unknown")
            return f"[{diff_type.upper()} file: {file_path}]"

        else:
            return "[Unknown match type]"

    def _filter_by_time_range(
        self,
        semantic_results: List[Dict[str, Any]],
        start_date: str,
        end_date: str,
        min_score: Optional[float] = None,
        diff_types: Optional[List[str]] = None,
        author: Optional[str] = None,
        chunk_type: Optional[str] = None,
        at_commit_ts: Optional[int] = None,
    ) -> Tuple[List[TemporalSearchResult], float]:
        """Transform semantic results to TemporalSearchResult objects.

        Phase 3 Migration: Time range filtering moved to vector store filter_conditions.
        This method now handles:
        - Content reconstruction from git (for added/deleted files)
        - min_score filtering (if specified)
        - diff_types post-filtering (safety layer + test compatibility)
        - author post-filtering (safety layer + test compatibility)
        - Result transformation to TemporalSearchResult objects

        Args:
            semantic_results: Results from semantic search (raw vector store format)
            start_date: Start date (YYYY-MM-DD) - kept for backward compatibility
            end_date: End date (YYYY-MM-DD) - kept for backward compatibility
            min_score: Minimum similarity score filter
            diff_types: Filter by diff type(s) (post-filter safety layer)
            author: Filter by author name (post-filter safety layer)
            at_commit_ts: (Bug #1301) Optional pre-resolved at_commit UNIX
                timestamp; tightens the post-filter upper bound the same way
                query_temporal() tightens the vector-store filter_conditions.

        Returns:
            Tuple of (filtered results, blob_fetch_time_ms)
        """
        filtered_results = []

        # Process each semantic result
        for result in semantic_results:
            # Get payload - handles both dict and object formats
            payload = (
                result.get("payload", {})
                if isinstance(result, dict)
                else getattr(result, "payload", {})
            )
            score = (
                result.get("score", 0.0)
                if isinstance(result, dict)
                else getattr(result, "score", 0.0)
            )

            # Storage optimization: Reconstruct content from git for added/deleted files
            if payload.get("reconstruct_from_git"):
                content = self._reconstruct_temporal_content(payload)
            else:
                # Content is in chunk_text at root level (Bug 1 fix in filesystem_vector_store)
                # Handle both dict and object formats
                chunk_text = None
                if isinstance(result, dict):
                    chunk_text = result.get("chunk_text", None)
                elif hasattr(result, "chunk_text") and not callable(
                    getattr(result, "chunk_text")
                ):
                    # Only use chunk_text if it's actually set (not a Mock auto-attribute)
                    try:
                        chunk_text = result.chunk_text
                    except AttributeError:
                        chunk_text = None

                if chunk_text is not None:
                    content = chunk_text
                else:
                    # FAIL FAST - optimization contract broken or index corrupted
                    # No backward compatibility fallbacks (Messi Rule #2)
                    commit_hash = payload.get("commit_hash", "unknown")
                    path = payload.get("path", "unknown")
                    raise RuntimeError(
                        f"Missing chunk_text for {commit_hash}:{path} - "
                        f"optimization contract violated or index corrupted"
                    )

            # Apply min_score filter if specified
            if min_score and score < min_score:
                continue

            # Story #1290: diff_types is a legacy per-file-diff concept that
            # no longer has a corresponding payload field on per-commit
            # chunks -- intentionally NOT filtered (documented no-op; see
            # query_temporal's filter_conditions comment for the same note).

            # Apply author post-filter (safety layer + test compatibility)
            if author:
                result_author = payload.get("author_name", "")
                if author.lower() not in result_author.lower():
                    continue

            # Apply chunk_type post-filter (Story #1290 AC12): the two
            # canonical values map onto the is_head field -- "commit_message"
            # keeps ONLY head chunks; "commit_diff" keeps ALL chunks (no
            # filtering). query_temporal() already validated chunk_type is
            # one of these two values (or None) before calling this method.
            if chunk_type == "commit_message" and not payload.get("is_head"):
                continue

            # Apply time range post-filter (safety layer + test compatibility)
            # Time range filtering is also done in vector store, but we apply it here
            # as a safety layer when _filter_by_time_range is called directly
            commit_timestamp = payload.get("commit_timestamp")
            if commit_timestamp:
                from datetime import datetime

                start_ts = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp())
                end_ts = int(
                    datetime.strptime(end_date, "%Y-%m-%d")
                    .replace(hour=23, minute=59, second=59)
                    .timestamp()
                )
                # Bug #1301: at_commit_ts tightens (never widens) the upper
                # bound -- mirrors the identical min() applied in
                # query_temporal()'s vector-store filter_conditions.
                if at_commit_ts is not None:
                    end_ts = min(end_ts, at_commit_ts)

                if commit_timestamp < start_ts or commit_timestamp > end_ts:
                    continue

            # Create temporal result from payload data
            # Check both "path" and "file_path" - temporal indexer uses "path"
            temporal_result = TemporalSearchResult(
                file_path=self._get_file_path_from_payload(payload, "unknown"),
                chunk_index=payload.get("chunk_index", 0),
                content=content,  # Now uses actual content from payload
                score=score,
                metadata=payload,  # Store full payload as metadata
                temporal_context={
                    "commit_hash": payload.get("commit_hash"),
                    "commit_date": payload.get("commit_date"),
                    "commit_message": payload.get("commit_message"),
                    "author_name": payload.get("author_name"),
                    "commit_timestamp": commit_timestamp,
                    "diff_type": payload.get("diff_type"),
                },
            )
            filtered_results.append(temporal_result)

        # Return results and 0 blob fetch time (no blob fetching in JSON approach)
        return filtered_results, 0.0

    # _get_head_file_blobs method removed - Story 2: SQLite elimination
    # No longer needed with diff-based indexing (blob-based helper)

    def _fetch_commit_details(self, commit_hash: str) -> Optional[Dict[str, Any]]:
        """Fetch commit details - deprecated, returns dummy data.

        Story 2: SQLite removed. This method is only called from CLI display
        functions and should be refactored to use payload data instead.

        Returns:
            Dict with basic commit info for backward compatibility
        """
        # Return minimal data for backward compatibility
        # The CLI should be updated to use payload data directly
        return {
            "hash": commit_hash,
            "date": "Unknown",
            "author_name": "Unknown",
            "author_email": "unknown@example.com",
            "message": "[Commit details not available - use payload data]",
        }

    # _is_new_file method removed - Story 2: SQLite elimination
    # No longer needed with diff-based indexing

    # filter_timeline_changes method removed - Story 2: diff-based indexing
    # Every result is a change by definition, no filtering needed

    # _generate_chunk_diff method removed - Story 2: SQLite elimination
    # No longer needed with diff-based indexing where diffs are pre-computed
