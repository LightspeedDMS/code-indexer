"""
File Listing Service.

Provides real file listing operations following CLAUDE.md Foundation #1: No mocks.
All operations use real file system operations with proper pagination and filtering.
"""

from code_indexer.server.logging_utils import format_error_log, get_log_extra

import io
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Callable,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
    Dict,
    Any,
    Set,
    TypeVar,
    cast,
    TYPE_CHECKING,
)
from datetime import datetime, timezone
import logging
import math
import hashlib

import pathspec

from ..models.api_models import (
    FileListResponse,
    FileInfo,
    PaginationInfo,
    FileListQueryParams,
)
from ..services.config_service import get_config_service
from code_indexer.utils.path_confinement import (
    confine_relative_path,
    resolve_confined_path,
)
from ..storage.shared.content_backend import (
    ContentBackend,
    ContentEntry,
    ContentPage,
    FetchedFile,
)

if TYPE_CHECKING:
    from code_indexer.global_repos.directory_explorer import (
        DirectoryExplorerService,
    )

    # Bug #1650: type-only import for the lazily-constructed
    # activated_repo_manager property below. Kept under TYPE_CHECKING
    # (never imported at runtime here) so the real import stays local to
    # the property that needs it, for the same circular-import-avoidance
    # reason as the original eager code.
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )

logger = logging.getLogger(__name__)

# Per-request latency: _collect_files walks the FULL repo tree (rglob + a stat
# and an is_indexed check per file). On large repos this dominates list_files /
# browse latency and it ran on every request. Cache the collected listing per
# repo_path for a short TTL so repeated calls (a client paging a repo) reuse the
# walk. Keyed on repo_path: immutable versioned golden-repo snapshots change path
# on refresh and miss naturally; mutable paths are eventually-consistent within
# the TTL.
_COLLECT_CACHE_TTL_SECONDS = 30.0
_COLLECT_CACHE_MAX_REPOS = 64
_collect_cache: "OrderedDict[str, Tuple[float, Any]]" = OrderedDict()
_collect_cache_lock = threading.Lock()

#: Prefix of the cache keys the content-seam reads use. Never an absolute path,
#: so a seam entry cannot collide with a local walk of the same directory.
_SEAM_CACHE_PREFIX = "content-seam:"
#: Shown for a file the manifest does not describe (a pruned directory, or a
#: server that predates mtime): the same placeholder indexed_only listings use.
_EPOCH = datetime.fromtimestamp(0, tz=timezone.utc)
#: Named in the warning when a snapshot has no registered base clone.
_SEAM_CONSUMER = "File tools"

_T = TypeVar("_T")


def _ttl_cached(key: str, build: Callable[[], _T]) -> _T:
    """``build()``'s result, cached under *key* for ``_COLLECT_CACHE_TTL_SECONDS``.

    The one TTL/LRU cache behind every listing, local or through the content
    seam. Expiry is anchored to POST-build time (not a pre-build ``now``): a
    walk that takes longer than the TTL must not be born already-expired --
    that would deliver zero cache benefit for exactly the slow repos this
    cache targets.
    """
    hit, cached = _ttl_get(key)
    if hit:
        return cast(_T, cached)
    value = build()
    _ttl_put(key, value)
    return value


def _ttl_get(key: str) -> Tuple[bool, Any]:
    """``(True, value)`` for a live entry under *key*, else ``(False, None)``."""
    now = time.monotonic()
    with _collect_cache_lock:
        entry = _collect_cache.get(key)
        if entry is not None and now < entry[0]:
            _collect_cache.move_to_end(key)
            return True, entry[1]
    return False, None


def _ttl_put(key: str, value: Any) -> None:
    """Cache *value* under *key*; expiry from NOW, i.e. after the build."""
    expiry = time.monotonic() + _COLLECT_CACHE_TTL_SECONDS
    with _collect_cache_lock:
        _collect_cache[key] = (expiry, value)
        _collect_cache.move_to_end(key)
        while len(_collect_cache) > _COLLECT_CACHE_MAX_REPOS:
            _collect_cache.popitem(last=False)


def modified_at_from_ns(mtime_ns: Optional[int]) -> datetime:
    """A manifest ``mtime_ns`` as the ``modified_at`` a local ``stat()`` gives.

    Computed exactly as CPython derives ``st_mtime`` from the same nanoseconds
    (seconds plus nanoseconds times 1e-9, in floating point), so a file listed
    through the seam carries the very timestamp a local listing would. Unknown
    is the epoch placeholder, never "now".
    """
    if mtime_ns is None:
        return _EPOCH
    seconds, nanos = divmod(mtime_ns, 1_000_000_000)
    return datetime.fromtimestamp(seconds + nanos * 1e-9, tz=timezone.utc)


@dataclass(frozen=True)
class ContentSeamSource:
    """Where a golden repo's files are read when they are not on this pod:
    the configured (non-local) backend, and the root that backend knows."""

    backend: ContentBackend
    root: str


def content_seam_source(
    repo_path: str, *, consumer: str = _SEAM_CONSUMER
) -> Optional[ContentSeamSource]:
    """The seam to read *repo_path* through, or None to read the local disk.

    None -- today's behaviour, unchanged -- unless the running server's content
    backend is NOT local. Then the repository is not on this pod: an alias
    target that is a pod-local versioned snapshot (index state only) maps to
    the golden repo's base clone, which the backend serves. ``consumer`` names
    the caller in the warning logged when that mapping fails.

    Pod-local ``local://`` repositories (Langfuse trace folders) are None even
    then: their files live only on this pod, never with the backend. cidx-meta
    is ``local://`` too but shared through the backend, so it keeps the seam.
    """
    from code_indexer.server.startup.content_backend_wiring import (
        golden_repo_content_root,
        is_pod_local_golden_path,
        server_content_backend,
    )

    backend = server_content_backend()
    if backend is None or backend.is_local:
        return None
    if is_pod_local_golden_path(repo_path):
        return None
    root = golden_repo_content_root(repo_path, consumer=consumer)
    return ContentSeamSource(backend=backend, root=str(root))


class ManifestDeadlineExceeded(TimeoutError):
    """The caller's deadline passed before the manifest's last page arrived."""


def manifest_entries(
    source: ContentSeamSource, prune_dirs: Sequence[str]
) -> Tuple[ContentEntry, ...]:
    """Every manifest entry of the repository, all pages, cached for the TTL.

    ``prune_dirs`` is sent on every page, as the protocol requires; entries
    are cached per (root, prune set), since different prune sets describe
    different entry sets.
    """
    prune = _prune_key(prune_dirs)
    return _ttl_cached(
        _manifest_cache_key(source, prune), lambda: _walk_manifest(source, prune)
    )


def iter_manifest_entries(
    source: ContentSeamSource,
    prune_dirs: Sequence[str],
    *,
    deadline: Optional[float] = None,
) -> Iterator[ContentEntry]:
    """:func:`manifest_entries`, yielded as each page arrives.

    For a caller with a deadline (``time.monotonic()`` based): it is checked
    before every page is requested, and a passed deadline raises
    :class:`ManifestDeadlineExceeded` with the entries already yielded in the
    caller's hands. Shares the TTL cache: a cached manifest is replayed, and a
    walk that reaches the last page is cached; a walk cut short never is.
    """
    prune = _prune_key(prune_dirs)
    key = _manifest_cache_key(source, prune)
    hit, cached = _ttl_get(key)
    if hit:
        yield from cached
        return
    entries: List[ContentEntry] = []
    for page in _manifest_pages(source, prune, deadline=deadline):
        entries.extend(page.entries)
        yield from page.entries
    _ttl_put(key, tuple(entries))


def _prune_key(prune_dirs: Sequence[str]) -> Tuple[str, ...]:
    return tuple(sorted(set(prune_dirs)))


def _manifest_cache_key(source: ContentSeamSource, prune: Tuple[str, ...]) -> str:
    return f"{_SEAM_CACHE_PREFIX}manifest:{','.join(prune)}:{source.root}"


def _walk_manifest(
    source: ContentSeamSource, prune: Tuple[str, ...]
) -> Tuple[ContentEntry, ...]:
    """Every entry of every page, in order."""
    return tuple(
        entry for page in _manifest_pages(source, prune) for entry in page.entries
    )


def _manifest_pages(
    source: ContentSeamSource,
    prune: Tuple[str, ...],
    *,
    deadline: Optional[float] = None,
) -> Iterator[ContentPage]:
    """Page through the manifest until the last page.

    A cursor that does not advance raises instead of looping forever on a
    misbehaving backend. ``deadline`` is checked before each page request.
    """
    cursor: Optional[str] = None
    while True:
        if deadline is not None and time.monotonic() > deadline:
            raise ManifestDeadlineExceeded("content manifest walk passed its deadline")
        page = source.backend.manifest(source.root, cursor=cursor, prune_dirs=prune)
        yield page
        if page.next_cursor is None:
            return
        if page.next_cursor == cursor:
            raise RuntimeError(
                "content manifest cursor did not advance; refusing to loop"
            )
        cursor = page.next_cursor


def fetch_one(source: ContentSeamSource, rel: str) -> FetchedFile:
    """The fetch record for ONE repo-relative path.

    A backend that yields nothing for it is reported as an error record, the
    same shape as any other per-file failure.
    """
    fetched = next(iter(source.backend.fetch(source.root, [rel])), None)
    if fetched is None:
        return FetchedFile(path=rel, blob_sha=None, data=b"", error="not returned")
    return fetched


def directory_explorer_for(repo_path: str) -> "DirectoryExplorerService":
    """The directory-tree service for a golden repo's alias target.

    Local backends get today's disk walk of *repo_path*. A non-local backend
    gets the same walk over the base clone's manifest, pruned of the
    directories the tree always excludes.
    """
    from code_indexer.global_repos.directory_explorer import (
        DirectoryExplorerService,
        ManifestDirectoryExplorerService,
    )

    source = content_seam_source(repo_path)
    if source is None:
        return DirectoryExplorerService(Path(repo_path))
    entries = manifest_entries(source, DirectoryExplorerService.prunable_dir_names())
    return ManifestDirectoryExplorerService(
        Path(source.root), ((e.path, e.is_symlink) for e in entries)
    )


# Same language detection as stats service
LANGUAGE_EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".java": "java",
    ".c": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".go": "go",
    ".rs": "rust",
    ".php": "php",
    ".rb": "ruby",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".sass": "sass",
    ".less": "less",
    ".vue": "vue",
    ".jsx": "jsx",
    ".tsx": "tsx",
    ".md": "markdown",
    ".rst": "rst",
    ".txt": "text",
    ".json": "json",
    ".xml": "xml",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "config",
    ".conf": "config",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".fish": "shell",
    ".ps1": "powershell",
    ".bat": "batch",
    ".cmd": "batch",
}


class FileListingService:
    """Service for listing repository files."""

    # Directories that are always excluded from file listings
    ALWAYS_EXCLUDED_DIRS: Set[str] = {".code-indexer", ".git"}

    # Story #686: Line-based default chunking limits
    # Applied IN ADDITION to token limits - the stricter limit wins
    DEFAULT_MAX_LINES: int = 500  # Default limit when no explicit limit provided
    MAX_ALLOWED_LIMIT: int = 5000  # Maximum limit client can request

    # Bug #1650 remediation: CLASS-LEVEL RLock (not per-instance), so
    # instances created via FileListingService.__new__(FileListingService)
    # in several existing test files (bypassing __init__ entirely) still
    # have a lock to synchronize on instead of raising AttributeError.
    # Mirrors the identical pattern applied to GitOperationsService for the
    # same bug: importing this module (transitively, via server/app.py's
    # own module-level `from .services.file_service import file_service`)
    # used to construct a real ActivatedRepoManager -> GoldenRepoManager ->
    # SQLite golden-repo load, spawning bgm-worker/bgm-temporal-worker
    # threads, as a side effect of merely importing any MCP handler module.
    _activated_repo_manager_lock = threading.RLock()

    def __init__(self):
        """Initialize the file listing service."""
        self._activated_repo_manager_lazy: Optional["ActivatedRepoManager"] = None

    @property
    def activated_repo_manager(self) -> "ActivatedRepoManager":
        """Lazily construct ActivatedRepoManager (Bug #1650): deferred to
        first real access instead of FileListingService() construction
        time.

        Uses getattr(..., None) rather than bare self._activated_repo_manager_lazy:
        several existing test files construct this service via
        FileListingService.__new__(FileListingService) (bypassing __init__
        entirely) and may read this property before ever assigning to it.

        Guarded by a per-instance `_arm_initializing` sentinel (Bug #1650
        round-2 remediation, M2): the RLock alone stops CROSS-THREAD
        deadlock but not SAME-THREAD re-entrant recursion -- on re-entry
        the double-checked `is None` test is still True (the assignment
        happens only after the constructor returns), so an unguarded
        re-entrant call arriving from within construction would construct
        AGAIN. Mirrors GitOperationsService.activated_repo_manager's
        identical fix exactly.
        """
        if getattr(self, "_activated_repo_manager_lazy", None) is None:
            with self._activated_repo_manager_lock:
                if getattr(self, "_arm_initializing", False):
                    raise AttributeError(
                        "activated_repo_manager is still under construction "
                        "(re-entrant access)"
                    )
                if getattr(self, "_activated_repo_manager_lazy", None) is None:
                    self._arm_initializing = True
                    try:
                        # Import here to avoid circular imports (same
                        # reasoning as the original eager code this
                        # replaces).
                        import os
                        from ..repositories.activated_repo_manager import (
                            ActivatedRepoManager,
                        )

                        _server_dir = os.environ.get("CIDX_SERVER_DATA_DIR")
                        self._activated_repo_manager_lazy = ActivatedRepoManager(
                            data_dir=os.path.join(_server_dir, "data")
                            if _server_dir
                            else None
                        )
                    finally:
                        self._arm_initializing = False
        return self._activated_repo_manager_lazy

    @activated_repo_manager.setter
    def activated_repo_manager(self, value: "ActivatedRepoManager") -> None:
        with self._activated_repo_manager_lock:
            self._activated_repo_manager_lazy = value

    def _get_file_content_limits_config(self):
        """Get file content limits config from ConfigService (Bug #939: unified ContentLimitsConfig).

        Returns an adapter exposing .max_tokens_per_request and .chars_per_token
        backed by ContentLimitsConfig so all call sites continue to work unchanged.
        """
        config_service = get_config_service()
        content_limits = config_service.get_config().content_limits_config

        class _Adapter:
            """Thin adapter mapping ContentLimitsConfig fields to the legacy attribute names."""

            def __init__(self, cl):
                self._cl = cl

            @property
            def max_tokens_per_request(self):
                return self._cl.file_content_max_tokens

            @property
            def file_content_max_tokens(self):
                return self._cl.file_content_max_tokens

            @property
            def chars_per_token(self):
                return self._cl.chars_per_token

        return _Adapter(content_limits)

    def list_files(
        self, repo_id: str, username: str, query_params: FileListQueryParams
    ) -> FileListResponse:
        """
        List files in repository with pagination and filtering.

        Args:
            repo_id: Repository identifier (user_alias)
            username: Username owning the activated repository
            query_params: Query parameters for filtering and pagination

        Returns:
            File list response with pagination

        Raises:
            FileNotFoundError: If repository doesn't exist
            PermissionError: If repository access denied
            ValueError: If query parameters are invalid
        """
        repo_path = self._get_repository_path(repo_id, username)

        source = content_seam_source(repo_path)
        if source is not None:
            # Content seam: the activated copy is on workspace-mcp at this path.
            all_files = self._collect_listing_via_seam(
                repo_path, source, query_params.indexed_only
            )
        else:
            if not os.path.exists(repo_path):
                raise FileNotFoundError(f"Repository {repo_id} not found")

            # Collect all files
            all_files = self._collect_listing(repo_path, query_params.indexed_only)

        # Apply filters
        filtered_files = self._apply_filters(all_files, query_params)

        # Apply sorting
        sorted_files = self._apply_sorting(filtered_files, query_params.sort_by)

        # Apply pagination
        paginated_files, pagination_info = self._apply_pagination(
            sorted_files, query_params.page, query_params.limit
        )

        return FileListResponse(files=paginated_files, pagination=pagination_info)

    def list_files_by_path(
        self, repo_path: str, query_params: FileListQueryParams
    ) -> FileListResponse:
        """
        List files from a direct filesystem path (for global repos).

        Unlike list_files() which looks up activated repos by alias,
        this method accepts a direct path for global repository browsing.

        Args:
            repo_path: Direct filesystem path to repository
            query_params: Query parameters for filtering and pagination

        Returns:
            File list response with pagination

        Raises:
            FileNotFoundError: If path doesn't exist
        """
        source = content_seam_source(repo_path)
        if source is None:
            if not os.path.exists(repo_path):
                raise FileNotFoundError(f"Path {repo_path} not found")
            all_files = self._collect_listing(repo_path, query_params.indexed_only)
        else:
            all_files = self._collect_listing_via_seam(
                repo_path, source, query_params.indexed_only
            )
        filtered_files = self._apply_filters(all_files, query_params)
        sorted_files = self._apply_sorting(filtered_files, query_params.sort_by)
        paginated_files, pagination_info = self._apply_pagination(
            sorted_files, query_params.page, query_params.limit
        )
        return FileListResponse(files=paginated_files, pagination=pagination_info)

    def list_directory_level(
        self, repo_path: str, subdir: str, query_params: FileListQueryParams
    ) -> FileListResponse:
        """List the files directly under a single directory (one level, non-recursive).

        A single-directory browse only needs the entries of ``<repo>/<subdir>``, but
        :meth:`list_files_by_path` reaches it via :meth:`_collect_files`, which
        ``rglob``s the ENTIRE repo tree (stat per file) and then filters down to the
        one directory. On a large golden repo over NFS that is tens of seconds for
        what is an ``ls`` of one folder. This scans just the target directory instead
        — O(entries-in-dir) rather than O(files-in-repo) — and then runs the identical
        filter/sort/paginate pipeline, so results match the full-walk path exactly.
        """
        source = content_seam_source(repo_path)
        if source is None:
            files = self._collect_single_level(repo_path, subdir)
        else:
            files = self._single_level_via_seam(source, subdir)
        filtered_files = self._apply_filters(files, query_params)
        sorted_files = self._apply_sorting(filtered_files, query_params.sort_by)
        paginated_files, pagination_info = self._apply_pagination(
            sorted_files, query_params.page, query_params.limit
        )
        return FileListResponse(files=paginated_files, pagination=pagination_info)

    def _collect_single_level(self, repo_path: str, subdir: str) -> List[FileInfo]:
        """Collect FileInfo for the files directly inside ``<repo_path>/<subdir>``.

        Parity with :meth:`_collect_files_uncached` for the entries it returns: same
        excluded-dir skip, same root ``.gitignore`` filtering, same FileInfo fields —
        only the traversal is scoped to one directory (``os.scandir``) instead of a
        whole-tree ``rglob``. Returns ``[]`` for a path that escapes the repo, does not
        exist, is not a directory, or is itself excluded.
        """
        repo_root = Path(repo_path)
        root_resolved = repo_root.resolve()
        target = (repo_root / subdir).resolve() if subdir else root_resolved

        # Refuse a subdir that resolves outside the repo (e.g. via ``..`` or a symlink).
        if target != root_resolved and root_resolved not in target.parents:
            return []
        if not target.is_dir():
            return []

        rel_base = target.relative_to(root_resolved)
        # The directory being browsed is itself under .git/.code-indexer -> nothing.
        if str(rel_base) != "." and self._is_in_excluded_dir(rel_base):
            return []

        gitignore_spec = self._load_gitignore_spec(repo_root)
        indexable_extensions = self._get_indexable_extensions()

        files: List[FileInfo] = []
        try:
            with os.scandir(target) as it:
                for entry in it:
                    try:
                        # is_file() follows symlinks, matching the rglob+is_file() path.
                        if not entry.is_file():
                            continue

                        relative_path = (
                            Path(entry.name)
                            if str(rel_base) == "."
                            else rel_base / entry.name
                        )
                        relative_path_str = str(relative_path)

                        if self._is_in_excluded_dir(relative_path):
                            continue
                        if gitignore_spec and gitignore_spec.match_file(
                            relative_path_str
                        ):
                            continue

                        stat_info = entry.stat()
                        files.append(
                            self._build_file_info(
                                relative_path_str,
                                stat_info.st_size,
                                datetime.fromtimestamp(
                                    stat_info.st_mtime, tz=timezone.utc
                                ),
                                indexable_extensions,
                            )
                        )
                    except (OSError, PermissionError) as e:
                        logger.warning(
                            format_error_log(
                                "CACHE-GENERAL-012",
                                "Cannot access file",
                                file_path=str(getattr(entry, "path", "")),
                                error=str(e),
                            ),
                            extra=get_log_extra("CACHE-GENERAL-012"),
                        )
                        continue
        except (FileNotFoundError, NotADirectoryError):
            return []

        return files

    @staticmethod
    def _delegated_activated_path(repo_id: str, username: str) -> Optional[str]:
        """The user's activated copy under a delegated content backend, or None.

        None -- the local default, unchanged below -- unless the server's
        content backend is delegated. Then the copy is on workspace-mcp, not
        this pod, so "is it activated for this user" is the registry's answer
        (the server's DI-wired manager, never a pod-disk probe) and the path is
        returned for the content seam to read; a copy missing on workspace-mcp
        then fails loudly there, never as an empty repository.
        """
        from code_indexer.server.startup.content_backend_wiring import (
            is_content_seam_delegated,
        )

        if not is_content_seam_delegated():
            return None
        from code_indexer.server.services.file_crud_service import (
            _get_activated_repo_manager,
        )

        manager = _get_activated_repo_manager()
        if not manager.user_has_activated_repo(username, repo_id):
            raise FileNotFoundError(
                f"Repository '{repo_id}' not found for user '{username}'"
            )
        return str(
            manager.get_activated_repo_path(username=username, user_alias=repo_id)
        )

    def _get_repository_path(self, repo_id: str, username: str) -> str:
        """
        Get file system path for repository from activated repositories.

        CRITICAL FIX: Searches activated repositories (user workspace) instead of
        golden repositories (shared source).

        Args:
            repo_id: Repository identifier (user_alias)
            username: Username owning the activated repository

        Returns:
            Real file system path to activated repository

        Raises:
            RuntimeError: If database lookup fails
            FileNotFoundError: If repository not found
        """
        delegated_path = self._delegated_activated_path(repo_id, username)
        if delegated_path is not None:
            return delegated_path
        try:
            # Use ActivatedRepositoryManager to search user's workspace
            activated_path = self.activated_repo_manager.get_activated_repo_path(
                username=username, user_alias=repo_id
            )

            # Verify path exists
            if not activated_path or not Path(activated_path).exists():
                raise FileNotFoundError(
                    f"Repository '{repo_id}' not found for user '{username}'"
                )

            return cast(str, activated_path)

        except FileNotFoundError as e:
            # Expected not-found: a repo simply absent on disk (e.g. a stale
            # activation record whose directory is gone). Callers (Story #1039
            # global recovery + clean not-found response) depend on this raise,
            # but it is NOT a server error -- log at DEBUG, never ERROR, to
            # avoid [CACHE-GENERAL-011] spam for an expected condition.
            logger.debug(
                format_error_log(
                    "CACHE-GENERAL-011",
                    "Repository path not found (expected)",
                    repo_id=repo_id,
                    username=username,
                    error=str(e),
                ),
                extra=get_log_extra("CACHE-GENERAL-011"),
            )
            raise
        except Exception as e:
            logger.error(
                format_error_log(
                    "CACHE-GENERAL-011",
                    "Failed to get repository path",
                    repo_id=repo_id,
                    username=username,
                    error=str(e),
                ),
                extra=get_log_extra("CACHE-GENERAL-011"),
            )
            raise RuntimeError(f"Unable to access repository {repo_id}: {e}")

    def _collect_listing(self, repo_path: str, indexed_only: bool) -> List[FileInfo]:
        """Return the file listing, preferring the index fast-path when requested.

        EVO-66189: with ``indexed_only`` set, serve from the index's ``path_index`` (an
        O(ms) msgpack read) instead of stat-walking the working tree over NFS. Falls back
        to the full walk when the repo has no index, so ``indexed_only`` is a safe hint.
        """
        if indexed_only:
            from_index = self._collect_files_from_index(repo_path)
            if from_index is not None:
                return from_index
        return self._collect_files(repo_path)

    def _collect_files_from_index(self, repo_path: str) -> Optional[List[FileInfo]]:
        """Build the listing from the index's ``path_index.bin`` — no NFS stat walk.

        Returns ``None`` when the repo has no index (caller falls back to the walk). The
        path-index keys are exactly the indexed source files (already excluding
        ``.code-indexer``/``.git``/gitignored), so no exclusion filtering is needed.
        ``size_bytes``/``modified_at`` are placeholders (0 / epoch): this mode trades that
        per-file metadata for avoiding the O(files) NFS stat cost (~50s on the evolution
        monolith → an ~ms msgpack read). A corrupt/unreadable index also falls back.
        """
        from code_indexer.storage.filesystem_vector_store import PathIndex

        # Current per-model layout (.code-indexer/index/<model>/) plus the legacy sibling
        # multimodal layout (.code-indexer/multimodal_index/<collection>/) that
        # multi_index_query_service still supports for repos not yet re-indexed — otherwise
        # indexed_only would silently omit files indexed only under the legacy path.
        ci_dir = Path(repo_path) / ".code-indexer"
        path_index_files = sorted(
            list((ci_dir / "index").glob("*/path_index.bin"))
            + list((ci_dir / "multimodal_index").glob("*/path_index.bin"))
        )
        if not path_index_files:
            return None

        epoch = datetime.fromtimestamp(0, tz=timezone.utc)
        seen: Set[str] = set()
        files: List[FileInfo] = []
        try:
            for pi_path in path_index_files:
                for rel in PathIndex.load(pi_path, strict=True).file_paths():
                    if rel in seen:
                        continue
                    seen.add(rel)
                    files.append(
                        FileInfo(
                            path=rel,
                            size_bytes=0,
                            modified_at=epoch,
                            language=self._detect_language(Path(rel)),
                            is_indexed=True,
                        )
                    )
        except Exception as e:
            logger.warning(
                format_error_log(
                    "CACHE-GENERAL-014",
                    "Failed to serve listing from index; falling back to walk",
                    repo_path=repo_path,
                    error=str(e),
                ),
                extra=get_log_extra("CACHE-GENERAL-014"),
            )
            return None
        return files

    def _collect_files(self, repo_path: str) -> List[FileInfo]:
        """Collect all files in the repo, CACHED per repo_path.

        The underlying walk (:meth:`_collect_files_uncached`) is O(files) with a
        stat + an is_indexed check per file; on large repos it cost tens of
        seconds and list_files/browse ran it on every request. Results are cached
        for ``_COLLECT_CACHE_TTL_SECONDS`` so repeated calls (a client paging a
        repo) reuse the walk. Keyed on repo_path; immutable versioned golden-repo
        snapshots change path on refresh and miss naturally.
        """
        return _ttl_cached(repo_path, lambda: self._collect_files_uncached(repo_path))

    # ── reads through the content seam (repository not on this pod) ────────

    def _collect_listing_via_seam(
        self, repo_path: str, source: ContentSeamSource, indexed_only: bool
    ) -> List[FileInfo]:
        """:meth:`_collect_listing` for a repository behind the content seam.

        The index is cidx's own state and stays on this pod, at the alias target
        (*repo_path*), so ``indexed_only`` still reads it there. Everything else
        comes from the backend's manifest of the base clone.
        """
        if indexed_only:
            from_index = self._collect_files_from_index(repo_path)
            if from_index is not None:
                return from_index
        return self._seam_listing(source)

    def _seam_listing(self, source: ContentSeamSource) -> List[FileInfo]:
        """The seam's full listing, cached for the TTL like a local walk."""
        return _ttl_cached(
            f"{_SEAM_CACHE_PREFIX}listing:{source.root}",
            lambda: self._collect_files_via_seam(source),
        )

    def _collect_files_via_seam(self, source: ContentSeamSource) -> List[FileInfo]:
        """:meth:`_collect_files_uncached`, from the manifest instead of a walk.

        Same exclusions (``ALWAYS_EXCLUDED_DIRS`` pruned by the backend and
        re-checked here), same root ``.gitignore`` semantics, same FileInfo
        fields: size from the entry, modified time from its ``mtime_ns``.

        Symlinks: the manifest reports a link and never follows it, so a link is
        listed as itself (its own size and time) and nothing is listed beneath a
        linked directory. The local walk follows both.
        """
        entries = manifest_entries(source, sorted(self.ALWAYS_EXCLUDED_DIRS))
        gitignore_spec = self._load_gitignore_spec_via_seam(source)
        indexable_extensions = self._get_indexable_extensions()
        files: List[FileInfo] = []
        for entry in entries:
            if self._is_in_excluded_dir(Path(entry.path)):
                continue
            if gitignore_spec and gitignore_spec.match_file(entry.path):
                continue
            files.append(
                self._build_file_info(
                    entry.path,
                    entry.size,
                    modified_at_from_ns(entry.mtime_ns),
                    indexable_extensions,
                )
            )
        return files

    def _single_level_via_seam(
        self, source: ContentSeamSource, subdir: str
    ) -> List[FileInfo]:
        """:meth:`_collect_single_level` derived from the seam's listing.

        One level of the cached full listing, so the result holds exactly what
        the local scan returns: files only, same exclusions, same gitignore.
        A subdir that escapes the repository yields nothing, as locally.
        """
        try:
            base = confine_relative_path(subdir)
        except PermissionError:
            return []
        return [
            f for f in self._seam_listing(source) if self._dirname_of(f.path) == base
        ]

    def _load_gitignore_spec_via_seam(
        self, source: ContentSeamSource
    ) -> Optional[pathspec.PathSpec]:
        """:meth:`_load_gitignore_spec`, with the root ``.gitignore`` fetched.

        A missing or unfetchable file means no spec, as locally; undecodable
        bytes are warned about and ignored, as locally.
        """
        fetched = fetch_one(source, ".gitignore")
        if fetched.error is not None:
            return None
        try:
            text = fetched.data.decode("utf-8")
        except UnicodeDecodeError as e:
            logger.warning(
                format_error_log(
                    "CACHE-GENERAL-014",
                    "Failed to read .gitignore",
                    gitignore_path=f"{source.root}/.gitignore",
                    error=str(e),
                ),
                extra=get_log_extra("CACHE-GENERAL-014"),
            )
            return None
        return self._gitignore_spec_from_text(text)

    def _read_bytes_via_seam(
        self, source: ContentSeamSource, file_path: str
    ) -> Tuple[bytes, datetime, Path]:
        """:meth:`_read_bytes_local` for a file behind the content seam.

        Confinement is lexical (absolute, ``..`` and malformed paths are refused
        with PermissionError) because there is no local tree to resolve against.
        The bytes come from ONE fetch, so lines, size and hash match the local
        read byte for byte. The modified time is the manifest entry's, or the
        epoch placeholder for a file the manifest does not describe.
        """
        rel = confine_relative_path(file_path)
        if not rel:
            raise FileNotFoundError(f"Not a file: {file_path}")
        fetched = fetch_one(source, rel)
        entries = manifest_entries(source, sorted(self.ALWAYS_EXCLUDED_DIRS))
        if fetched.error is not None:
            prefix = rel + "/"
            if any(e.path.startswith(prefix) for e in entries):
                raise FileNotFoundError(f"Not a file: {file_path}")
            raise FileNotFoundError(f"File not found: {file_path}")
        entry = next((e for e in entries if e.path == rel), None)
        modified_at = modified_at_from_ns(entry.mtime_ns if entry else None)
        return fetched.data, modified_at, Path(rel)

    @staticmethod
    def _decode_lines(data: bytes) -> List[str]:
        """Lines of *data* exactly as ``open(..., "r", encoding="utf-8",
        errors="replace").readlines()`` yields them (universal newlines)."""
        with io.TextIOWrapper(
            io.BytesIO(data), encoding="utf-8", errors="replace"
        ) as text:
            return text.readlines()

    def _build_file_info(
        self,
        relative_path: str,
        size: int,
        modified_at: datetime,
        indexable_extensions: Set[str],
    ) -> FileInfo:
        """One listing row, built the same way for every source of files."""
        path = Path(relative_path)
        return FileInfo(
            path=relative_path,
            size_bytes=size,
            modified_at=modified_at,
            language=self._detect_language(path),
            is_indexed=path.suffix.lower() in indexable_extensions,
        )

    def _get_indexable_extensions(self) -> Set[str]:
        """Indexable extensions from config, fetched ONCE per collection.

        ``_is_file_indexed`` reads ``get_config_service().get_config()`` and
        ``indexing_config.indexable_extensions`` once PER FILE; hoisting it here
        removes O(files) config lookups from the collection loop.
        """
        config = get_config_service().get_config()
        if config.indexing_config is None:
            return set()
        return set(config.indexing_config.indexable_extensions)

    def _collect_files_uncached(self, repo_path: str) -> List[FileInfo]:
        """
        Collect all files in repository, excluding system directories and gitignored files.

        Excludes:
        - .code-indexer/ directory (CIDX index storage)
        - .git/ directory (Git internals)
        - Files matching .gitignore patterns (if .gitignore exists)

        Args:
            repo_path: Repository file system path

        Returns:
            List of file information objects
        """
        files = []
        repo_root = Path(repo_path)

        # Load .gitignore patterns if present
        gitignore_spec = self._load_gitignore_spec(repo_root)

        # Hoist the indexable-extensions lookup out of the per-file loop below
        # (it was one get_config() call PER FILE via _is_file_indexed()).
        indexable_extensions = self._get_indexable_extensions()

        # EVO-66189: os.walk with in-place pruning of ALWAYS_EXCLUDED_DIRS, replacing
        # rglob("*") + a post-hoc _is_in_excluded_dir filter. The old walk stat'd EVERY
        # entry — including the ~57k vector files under this repo's own .code-indexer/ on
        # a large golden repo — over NFS, then discarded them. Pruning never descends the
        # excluded subtrees, so the output set is identical but those stats disappear.
        # onerror re-raises so an inaccessible repo dir still surfaces exactly as before.
        # followlinks=True matches pre-3.13 Path.rglob("*") semantics (this repo targets
        # <3.13, where rglob unconditionally follows symlinked directories): without it,
        # files under a symlinked subdirectory would be silently dropped.
        def _walk_onerror(err: OSError) -> None:
            raise err

        try:
            for dirpath, dirnames, filenames in os.walk(
                repo_root, onerror=_walk_onerror, followlinks=True
            ):
                dirnames[:] = [
                    d for d in dirnames if d not in self.ALWAYS_EXCLUDED_DIRS
                ]
                for filename in filenames:
                    file_path = Path(dirpath) / filename
                    try:
                        # is_file() follows symlinks — preserve the old rglob+is_file()
                        # filter (regular files + symlinks-to-regular only).
                        if not file_path.is_file():
                            continue

                        relative_path = file_path.relative_to(repo_root)
                        relative_path_str = str(relative_path)

                        # Belt-and-suspenders: pruning already skips excluded subtrees;
                        # this also catches a plain FILE named .git/.code-indexer.
                        if self._is_in_excluded_dir(relative_path):
                            continue

                        # Skip files matching .gitignore patterns
                        if gitignore_spec and gitignore_spec.match_file(
                            relative_path_str
                        ):
                            continue

                        stat_info = file_path.stat()

                        files.append(
                            self._build_file_info(
                                relative_path_str,
                                stat_info.st_size,
                                datetime.fromtimestamp(
                                    stat_info.st_mtime, tz=timezone.utc
                                ),
                                indexable_extensions,
                            )
                        )

                    except (OSError, PermissionError) as e:
                        logger.warning(
                            format_error_log(
                                "CACHE-GENERAL-012",
                                "Cannot access file",
                                file_path=str(file_path),
                                error=str(e),
                            ),
                            extra=get_log_extra("CACHE-GENERAL-012"),
                        )
                        continue

        except PermissionError as e:
            logger.error(
                format_error_log(
                    "CACHE-GENERAL-013",
                    "Cannot access repository directory",
                    repo_path=repo_path,
                    error=str(e),
                ),
                extra=get_log_extra("CACHE-GENERAL-013"),
            )
            raise

        return files

    def _is_in_excluded_dir(self, relative_path: Path) -> bool:
        """
        Check if a file path is within an excluded directory.

        Args:
            relative_path: Path relative to repository root

        Returns:
            True if path is in an excluded directory
        """
        # Check each part of the path against excluded directories
        for part in relative_path.parts:
            if part in self.ALWAYS_EXCLUDED_DIRS:
                return True
        return False

    def _load_gitignore_spec(self, repo_root: Path) -> Optional[pathspec.PathSpec]:
        """
        Load and parse .gitignore file if present.

        Args:
            repo_root: Repository root directory

        Returns:
            PathSpec object for matching gitignore patterns, or None if no .gitignore
        """
        gitignore_path = repo_root / ".gitignore"

        if not gitignore_path.exists():
            return None

        try:
            with open(gitignore_path, "r", encoding="utf-8") as f:
                gitignore_content = f.read()

            return self._gitignore_spec_from_text(gitignore_content)
        except (OSError, UnicodeDecodeError) as e:
            logger.warning(
                format_error_log(
                    "CACHE-GENERAL-014",
                    "Failed to read .gitignore",
                    gitignore_path=str(gitignore_path),
                    error=str(e),
                ),
                extra=get_log_extra("CACHE-GENERAL-014"),
            )
            return None

    @staticmethod
    def _gitignore_spec_from_text(gitignore_content: str) -> pathspec.PathSpec:
        """Parse a root ``.gitignore``'s text -- the one parser for both the
        local file and the one fetched through the content seam."""
        return pathspec.PathSpec.from_lines(
            "gitwildmatch", gitignore_content.splitlines()
        )

    def _detect_language(self, file_path: Path) -> Optional[str]:
        """
        Detect programming language from file extension.

        Args:
            file_path: Path to file

        Returns:
            Programming language name or None if unknown
        """
        extension = file_path.suffix.lower()
        return LANGUAGE_EXTENSIONS.get(extension)

    def _is_file_indexed(self, file_path: Path) -> bool:
        """
        Check if file is likely indexed based on its extension.

        Uses the server's indexable_extensions configuration (Story #223 - AC3)
        to determine if CIDX would index this file type. No fallback to any
        hardcoded set — if the config has no extensions, nothing is indexed.

        Args:
            file_path: Path to file

        Returns:
            Whether file is indexed
        """
        extension = file_path.suffix.lower()
        config_service = get_config_service()
        config = config_service.get_config()
        if config.indexing_config is None:
            return False
        return extension in config.indexing_config.indexable_extensions

    def _apply_filters(
        self, files: List[FileInfo], query_params: FileListQueryParams
    ) -> List[FileInfo]:
        """
        Apply filtering to file list.

        Args:
            files: List of files to filter
            query_params: Query parameters containing filters

        Returns:
            Filtered list of files
        """
        filtered_files = files

        # Filter by path pattern
        if query_params.path_pattern:
            pattern = query_params.path_pattern
            # Use pathspec for proper ** glob support (gitignore-style)
            # pathspec treats ** as "this directory and all subdirectories"
            # while fnmatch treats ** as requiring 1+ subdirectories
            spec = pathspec.PathSpec.from_lines("gitwildmatch", [pattern])
            filtered_files = [f for f in filtered_files if spec.match_file(f.path)]

        # Filter by language
        if query_params.language:
            target_language = query_params.language.lower()
            filtered_files = [
                f
                for f in filtered_files
                if f.language and f.language.lower() == target_language
            ]

        # Bug #1886: single-level depth restriction (browse_directory's
        # recursive=False). Gitignore-style glob matching (used above for
        # path_pattern) generally DOES anchor correctly -- "examples/*.java"
        # matches only direct children ending in .java, never
        # "examples/sub/Nested.java". The one leaky shape is a trailing BARE
        # "*" segment: "examples/*" still matches "examples/sub/Nested.java"
        # because pathspec's directory-containment semantics don't anchor an
        # unsuffixed "*" to stop at "/". This explicit restriction closes
        # that gap (and is a second, independent guarantee for every other
        # pattern shape besides). Applied here (BEFORE
        # _apply_sorting/_apply_pagination) so a limit truncation can never
        # starve real direct children behind alphabetically-earlier nested
        # files.
        if query_params.direct_children_of is not None:
            base = query_params.direct_children_of
            filtered_files = [
                f for f in filtered_files if self._dirname_of(f.path) == base
            ]

        return filtered_files

    @staticmethod
    def _dirname_of(path: str) -> str:
        """Return the parent directory of a repo-relative path (posix "/").

        Mirrors Path(path).parent behavior for the forward-slash-joined
        relative paths FileInfo.path always holds (built via
        ``Path.relative_to()`` in ``_collect_files_uncached``): a top-level
        file's dirname is "" (the repository root).
        """
        idx = path.rfind("/")
        return path[:idx] if idx != -1 else ""

    def _apply_sorting(
        self, files: List[FileInfo], sort_by: Optional[str]
    ) -> List[FileInfo]:
        """
        Apply sorting to file list.

        Args:
            files: List of files to sort
            sort_by: Sort field name

        Returns:
            Sorted list of files
        """
        valid_sort_fields = {"path", "size", "modified_at"}

        if sort_by is None or sort_by not in valid_sort_fields:
            if sort_by is not None:
                logger.warning(
                    format_error_log(
                        "CACHE-GENERAL-015",
                        "Invalid sort field, using default",
                        sort_by=sort_by,
                        default="path",
                    ),
                    extra=get_log_extra("CACHE-GENERAL-015"),
                )
            sort_by = "path"

        if sort_by == "path":
            return sorted(files, key=lambda f: f.path.lower())
        elif sort_by == "size":
            return sorted(files, key=lambda f: f.size_bytes)
        elif sort_by == "modified_at":
            return sorted(files, key=lambda f: f.modified_at)

        return files

    def _apply_pagination(
        self, files: List[FileInfo], page: int, limit: int
    ) -> Tuple[List[FileInfo], PaginationInfo]:
        """
        Apply pagination to file list.

        Args:
            files: List of files to paginate
            page: Page number (1-based)
            limit: Items per page

        Returns:
            Tuple of (paginated files, pagination info)
        """
        total_files = len(files)
        total_pages = math.ceil(total_files / limit) if total_files > 0 else 0

        # Calculate offset
        offset = (page - 1) * limit

        # Get page of files
        paginated_files = files[offset : offset + limit]

        # Check if there are more pages
        has_next = page < total_pages

        pagination_info = PaginationInfo(
            page=page, limit=limit, total=total_files, has_next=has_next
        )

        return paginated_files, pagination_info

    def _enforce_token_limits(
        self, content: str, total_lines: int, effective_offset: int
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Enforce token limits on content and generate metadata.

        Args:
            content: Content to potentially truncate
            total_lines: Total lines in the file
            effective_offset: Starting line number (1-indexed)

        Returns:
            Tuple of (possibly truncated content, metadata dict with token info)
        """
        file_content_limits = self._get_file_content_limits_config()
        max_chars = (
            file_content_limits.file_content_max_tokens
            * file_content_limits.chars_per_token
        )

        # Calculate estimated tokens from content length
        estimated_tokens = len(content) // file_content_limits.chars_per_token

        # Check if truncation needed
        truncated = False
        truncated_at_line = None
        actual_content = content

        if len(content) > max_chars:
            # Truncate content to fit token budget
            actual_content = content[:max_chars]
            truncated = True

            # Calculate which line we truncated at
            lines_before_truncation = actual_content.count("\n")
            truncated_at_line = effective_offset + lines_before_truncation

            # Recalculate estimated tokens for truncated content
            estimated_tokens = (
                len(actual_content) // file_content_limits.chars_per_token
            )

        # Determine if pagination required
        # Formula: last_returned_line = effective_offset + returned_lines - 1
        # Has more if last_returned_line < total_lines, which simplifies to:
        # effective_offset + returned_lines - 1 < total_lines
        # OR: effective_offset + returned_lines <= total_lines
        returned_lines = actual_content.count("\n") + (
            1 if actual_content and not actual_content.endswith("\n") else 0
        )
        last_returned_line = effective_offset + returned_lines - 1
        requires_pagination = truncated or (last_returned_line < total_lines)

        # Generate pagination hint
        pagination_hint = ""
        if requires_pagination:
            if truncated:
                assert truncated_at_line is not None, (
                    "truncated_at_line must be set when truncated=True"
                )
                pagination_hint = f"Content truncated at line {truncated_at_line} due to token limit. Use offset={truncated_at_line + 1} to continue reading."
            else:
                next_offset = effective_offset + returned_lines
                pagination_hint = f"More content available. Use offset={next_offset} to continue reading."

        # Build token enforcement metadata
        token_metadata = {
            "estimated_tokens": estimated_tokens,
            "max_tokens_per_request": file_content_limits.file_content_max_tokens,
            "truncated": truncated,
            "truncated_at_line": truncated_at_line,
            "requires_pagination": requires_pagination,
            "pagination_hint": pagination_hint if requires_pagination else None,
        }

        return actual_content, token_metadata

    def get_file_content(
        self,
        repository_alias: str,
        file_path: str,
        username: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        skip_truncation: bool = False,
    ) -> Dict[str, Any]:
        """Get content of a specific file from repository with optional pagination.

        CRITICAL BEHAVIOR CHANGE: When offset and limit are both None, returns FIRST CHUNK ONLY
        (not entire file) to prevent LLM context window exhaustion. Token limits are enforced
        in all cases, UNLESS skip_truncation=True.

        Args:
            repository_alias: Repository user alias
            file_path: Relative path to file within repository
            username: Username owning the repository
            offset: 1-indexed line number to start reading from (default: 1)
            limit: Maximum number of lines to return (default: None = token-limited chunk)
            skip_truncation: If True, bypasses line limits and token-based truncation.
                Use this when the caller will apply its own truncation (e.g., MCP handlers
                using TruncationHelper for cache_handle support). Default: False.

        Returns:
            Dict with 'content' and 'metadata' keys. Metadata includes pagination info:
            - total_lines: Total lines in file
            - returned_lines: Lines returned in this response
            - offset: Starting line number (1-indexed)
            - limit: Limit used (None if unlimited)
            - has_more: True if more lines exist beyond returned range
            - estimated_tokens: Estimated token count of returned content
            - max_tokens_per_request: Current token limit from config
            - truncated: True if content was truncated due to token limit
            - truncated_at_line: Line number where truncation occurred (if truncated)
            - requires_pagination: True if file has more content to read
            - pagination_hint: Helpful message for navigating large files
        """
        repo_path = self._get_repository_path(repository_alias, username)
        if content_seam_source(repo_path) is not None:
            # Content seam: the activated copy is on workspace-mcp; read it as
            # every delegated repository is read (bytes, byte-sha256 hash --
            # the hash the delegated edit/delete compare against).
            return self.get_file_content_by_path(
                repo_path,
                file_path,
                offset=offset,
                limit=limit,
                skip_truncation=skip_truncation,
            )
        # Bug #1891 round 2 (S1): the previous
        # str(full_file_path).startswith(str(repo_root)) check incorrectly
        # admitted a SIBLING directory whose name starts with repo_root's
        # name (e.g. repo_root=".../foo" admits ".../foo-private/...").
        # resolve_confined_path() confines via Path.relative_to() instead,
        # which is immune to that class of bug.
        full_file_path = resolve_confined_path(Path(repo_path), file_path)
        if not full_file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        if not full_file_path.is_file():
            raise FileNotFoundError(f"Not a file: {file_path}")

        # Read all lines for pagination
        # Bug #1449: errors="replace" so a non-UTF-8 byte (e.g. Windows-1252
        # punctuation) is substituted with U+FFFD instead of raising
        # UnicodeDecodeError -- a legitimately-existing file must still be
        # readable, and byte-stable line/offset pagination is preserved.
        with open(full_file_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()

        total_lines = len(all_lines)

        # CRITICAL: Default behavior changed - return first chunk only (not entire file)
        # Apply offset (convert 1-indexed to 0-indexed for slicing)
        effective_offset = offset if offset is not None else 1
        start_index = max(0, effective_offset - 1)

        # Story #33 Fix: skip_truncation bypasses line limits and token truncation
        # This allows MCP handlers to get full content and apply TruncationHelper
        # for proper cache_handle support
        if skip_truncation:
            # Return all lines from offset to end (or respect user-specified limit only)
            if limit is not None:
                # User specified limit is still respected (pagination boundary)
                end_index = start_index + limit
                selected_lines = all_lines[start_index:end_index]
            else:
                # No limit: return all remaining lines
                selected_lines = all_lines[start_index:]

            # Build content string (NO token truncation)
            content = "".join(selected_lines)

            # Calculate returned_lines from content
            returned_lines = content.count("\n") + (
                1 if content and not content.endswith("\n") else 0
            )

            # Calculate has_more: true if more content exists after returned lines
            last_returned_line = effective_offset + returned_lines - 1
            has_more = last_returned_line < total_lines

            # Calculate next_offset for pagination
            next_offset = effective_offset + returned_lines if has_more else None

            # Build token metadata without truncation
            file_limits = self._get_file_content_limits_config()
            estimated_tokens = len(content) // file_limits.chars_per_token
            token_metadata = {
                "estimated_tokens": estimated_tokens,
                "max_tokens_per_request": file_limits.max_tokens_per_request,
                "truncated": False,
                "truncated_at_line": None,
                "requires_pagination": has_more,
                "pagination_hint": (
                    f"More content available. Use offset={next_offset} to continue reading."
                    if has_more
                    else None
                ),
            }

            enforced_content = content
        else:
            # Original behavior: Apply line-based limits and token truncation

            # Story #686: Apply line-based limits IN ADDITION to token limits
            # The stricter of (line limit, token limit) wins
            if limit is None:
                # No user-specified limit: apply default max lines
                effective_limit = self.DEFAULT_MAX_LINES
            else:
                # User specified limit: cap at max allowed
                effective_limit = min(limit, self.MAX_ALLOWED_LIMIT)

            # Apply the effective limit
            end_index = start_index + effective_limit
            selected_lines = all_lines[start_index:end_index]

            # Build content string
            content = "".join(selected_lines)

            # Apply token enforcement (may truncate content further)
            enforced_content, token_metadata = self._enforce_token_limits(
                content, total_lines, effective_offset
            )

            # Calculate returned_lines from enforced content
            returned_lines = enforced_content.count("\n") + (
                1 if enforced_content and not enforced_content.endswith("\n") else 0
            )

            # Calculate has_more: true if more content exists after returned lines
            last_returned_line = effective_offset + returned_lines - 1
            has_more = last_returned_line < total_lines

            # Story #686: Calculate next_offset for pagination
            next_offset = effective_offset + returned_lines if has_more else None

        # Story #7 AC2: Compute content_hash from FULL file content for optimistic locking
        # Hash must be of entire file (not just paginated portion) so clients can
        # use it for edit operations regardless of which portion they read
        full_content = "".join(all_lines)
        content_hash = hashlib.sha256(full_content.encode("utf-8")).hexdigest()

        # Build metadata with pagination info
        stat_info = full_file_path.stat()
        metadata = {
            "size": stat_info.st_size,
            "modified_at": datetime.fromtimestamp(
                stat_info.st_mtime, tz=timezone.utc
            ).isoformat(),
            "language": self._detect_language(full_file_path),
            "path": file_path,
            # Story #7 AC2: content_hash for optimistic locking
            "content_hash": content_hash,
            # Original pagination metadata
            "total_lines": total_lines,
            "returned_lines": returned_lines,
            "offset": effective_offset,
            "limit": limit,  # Keep original limit for client visibility
            "has_more": has_more,
            # Story #686: Add next_offset for easy pagination
            "next_offset": next_offset,
        }

        # Merge token enforcement metadata
        metadata.update(token_metadata)

        return {"content": enforced_content, "metadata": metadata}

    def get_file_content_by_path(
        self,
        repo_path: str,
        file_path: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        skip_truncation: bool = False,
    ) -> Dict[str, Any]:
        """Get content of a specific file from a direct filesystem path (for global repos).

        Unlike get_file_content() which looks up activated repos by alias,
        this method accepts a direct path for global repository file access.

        CRITICAL BEHAVIOR CHANGE: When offset and limit are both None, returns FIRST CHUNK ONLY
        (not entire file) to prevent LLM context window exhaustion. Token limits are enforced
        in all cases, UNLESS skip_truncation=True.

        Args:
            repo_path: Direct filesystem path to repository root
            file_path: Relative path to file within repository
            offset: 1-indexed line number to start reading from (default: 1)
            limit: Maximum number of lines to return (default: None = token-limited chunk)
            skip_truncation: If True, bypasses line limits and token-based truncation.
                Use this when the caller will apply its own truncation (e.g., MCP handlers
                using TruncationHelper for cache_handle support). Default: False.

        Returns:
            Dict with 'content' and 'metadata' keys. Metadata includes pagination info:
            - total_lines: Total lines in file
            - returned_lines: Lines returned in this response
            - offset: Starting line number (1-indexed)
            - limit: Limit used (None if unlimited)
            - has_more: True if more lines exist beyond returned range
            - estimated_tokens: Estimated token count of returned content
            - max_tokens_per_request: Current token limit from config
            - truncated: True if content was truncated due to token limit
            - truncated_at_line: Line number where truncation occurred (if truncated)
            - requires_pagination: True if file has more content to read
            - pagination_hint: Helpful message for navigating large files

        Raises:
            PermissionError: If path traversal attempted
            FileNotFoundError: If file doesn't exist
        """
        source = content_seam_source(repo_path)
        if source is None:
            data, modified_at, language_path = self._read_bytes_local(
                repo_path, file_path
            )
        else:
            data, modified_at, language_path = self._read_bytes_via_seam(
                source, file_path
            )
        response = self._paginate_file_lines(
            self._decode_lines(data),
            file_path=file_path,
            size=len(data),
            modified_at=modified_at,
            language_path=language_path,
            offset=offset,
            limit=limit,
            skip_truncation=skip_truncation,
        )
        # The optimistic-locking hash edit_file/delete_file check: SHA-256 of
        # the file's bytes, exactly as FileCRUDService hashes them, so a read
        # of a write-mode golden repo yields a hash an edit accepts.
        response["metadata"]["content_hash"] = hashlib.sha256(data).hexdigest()
        return response

    def _read_bytes_local(
        self, repo_path: str, file_path: str
    ) -> Tuple[bytes, datetime, Path]:
        """A file's bytes, modified time and language path, from this disk.

        Raises:
            PermissionError: If path traversal attempted
            FileNotFoundError: If file doesn't exist or is not a file
        """
        # Bug #1891 round 2 (S1): the previous
        # str(full_file_path).startswith(str(repo_root)) check incorrectly
        # admitted a SIBLING directory whose name starts with repo_root's
        # name (e.g. repo_root=".../foo" admits ".../foo-private/...").
        # resolve_confined_path() confines via Path.relative_to() instead,
        # which is immune to that class of bug.
        full_file_path = resolve_confined_path(Path(repo_path), file_path)
        if not full_file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        if not full_file_path.is_file():
            raise FileNotFoundError(f"Not a file: {file_path}")

        # One read: the bytes are both hashed and decoded (_decode_lines, Bug
        # #1449's errors="replace" text-mode decoding), so the hash and the
        # lines describe the same content.
        with open(full_file_path, "rb") as f:
            data = f.read()

        stat_info = full_file_path.stat()
        modified_at = datetime.fromtimestamp(stat_info.st_mtime, tz=timezone.utc)
        return data, modified_at, full_file_path

    def _paginate_file_lines(
        self,
        all_lines: List[str],
        *,
        file_path: str,
        size: int,
        modified_at: datetime,
        language_path: Path,
        offset: Optional[int],
        limit: Optional[int],
        skip_truncation: bool,
    ) -> Dict[str, Any]:
        """The get_file_content_by_path response for a file's lines, wherever
        they were read from: offset/limit, character budget and metadata."""
        total_lines = len(all_lines)

        # CRITICAL: Default behavior changed - return first chunk only (not entire file)
        # Apply offset (convert 1-indexed to 0-indexed for slicing)
        effective_offset = offset if offset is not None else 1
        start_index = max(0, effective_offset - 1)

        # Story #33 Fix: skip_truncation bypasses line limits and token truncation
        # This allows MCP handlers to get full content and apply TruncationHelper
        # for proper cache_handle support
        if skip_truncation:
            # Return all lines from offset to end (or respect user-specified limit only)
            if limit is not None:
                # User specified limit is still respected (pagination boundary)
                end_index = start_index + limit
                selected_lines = all_lines[start_index:end_index]
            else:
                # No limit: return all remaining lines
                selected_lines = all_lines[start_index:]

            # Build content string (NO token truncation)
            content = "".join(selected_lines)

            # Calculate returned_lines from content
            returned_lines = content.count("\n") + (
                1 if content and not content.endswith("\n") else 0
            )

            # Calculate has_more: true if more content exists after returned lines
            last_returned_line = effective_offset + returned_lines - 1
            has_more = last_returned_line < total_lines

            # Calculate next_offset for pagination
            next_offset = effective_offset + returned_lines if has_more else None

            # Build token metadata without truncation
            file_limits = self._get_file_content_limits_config()
            estimated_tokens = len(content) // file_limits.chars_per_token
            token_metadata = {
                "estimated_tokens": estimated_tokens,
                "max_tokens_per_request": file_limits.max_tokens_per_request,
                "truncated": False,
                "truncated_at_line": None,
                "requires_pagination": has_more,
                "pagination_hint": (
                    f"More content available. Use offset={next_offset} to continue reading."
                    if has_more
                    else None
                ),
            }

            enforced_content = content
        else:
            # Original behavior: Apply line-based limits and token truncation

            # Bug #939: When limit=None, use MAX_ALLOWED_LIMIT so that token
            # truncation is the authoritative constraint (not line truncation).
            # Story #686 DEFAULT_MAX_LINES applies only to get_file_content (alias-based).
            if limit is None:
                effective_limit = self.MAX_ALLOWED_LIMIT
            else:
                # User specified limit: cap at max allowed
                effective_limit = min(limit, self.MAX_ALLOWED_LIMIT)

            # Apply the effective limit
            end_index = start_index + effective_limit
            selected_lines = all_lines[start_index:end_index]

            # Build content string
            content = "".join(selected_lines)

            # Apply token enforcement (may truncate content further)
            enforced_content, token_metadata = self._enforce_token_limits(
                content, total_lines, effective_offset
            )

            # Calculate returned_lines from enforced content
            returned_lines = enforced_content.count("\n") + (
                1 if enforced_content and not enforced_content.endswith("\n") else 0
            )

            # Calculate has_more: true if more content exists after returned lines
            last_returned_line = effective_offset + returned_lines - 1
            has_more = last_returned_line < total_lines

            # Story #686: Calculate next_offset for pagination
            next_offset = effective_offset + returned_lines if has_more else None

        # Build metadata with pagination info
        metadata = {
            "size": size,
            "modified_at": modified_at.isoformat(),
            "language": self._detect_language(language_path),
            "path": file_path,
            # Original pagination metadata
            "total_lines": total_lines,
            "returned_lines": returned_lines,
            "offset": effective_offset,
            "limit": limit,  # Keep original limit for client visibility
            "has_more": has_more,
            # Story #686: Add next_offset for easy pagination
            "next_offset": next_offset,
        }

        # Merge token enforcement metadata
        metadata.update(token_metadata)

        return {"content": enforced_content, "metadata": metadata}


# Global service instance
file_service = FileListingService()
