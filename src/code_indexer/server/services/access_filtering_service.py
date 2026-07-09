"""
Access Filtering Service for CIDX Server.

Story #707: Query-Time Access Enforcement and Repo Visibility Filtering

Provides centralized access filtering for:
- Query results filtering by user group membership
- Repository listing filtering
- cidx-meta summary filtering

Key principles:
- Invisible repo pattern: No 403 errors, repos simply don't appear
- cidx-meta always accessible to everyone
- admins group has full access to all repos
- Group membership checked fresh each query (no caching)
"""

import logging
import re
import time
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Optional,
    Protocol,
    Set,
    runtime_checkable,
)

from .constants import CIDX_META_REPO, DEFAULT_GROUP_ADMINS
from .group_access_manager import GroupAccessManager
from .memory_io import MemoryFileCorruptError, MemoryFileNotFoundError, read_memory_file

if TYPE_CHECKING:
    from .memory_metadata_cache import MemoryMetadataCache

# Memory files are named {uuid4().hex}.md — exactly 32 lowercase hex chars stem.
_MEMORY_FILE_RE = re.compile(r"^[0-9a-f]{32}\.md$")

logger = logging.getLogger(__name__)


@runtime_checkable
class QueryResultProtocol(Protocol):
    """Protocol for query result objects that can be filtered."""

    repository_alias: str


class AccessFilteringService:
    """
    Centralized service for access filtering at query time.

    Filters query results and repository listings based on user's group
    membership. Implements the invisible repo pattern - inaccessible
    repositories are simply not returned, with no indication they exist.
    """

    # Default over-fetch multiplier for compensating filtered results
    DEFAULT_OVER_FETCH_FACTOR = 2

    # Special group name that has full access to all repos
    ADMIN_GROUP_NAME = DEFAULT_GROUP_ADMINS

    # Bug #338: TTL for _get_all_repo_aliases() cache (seconds)
    REPO_ALIASES_CACHE_TTL = 60

    def __init__(
        self,
        group_access_manager: GroupAccessManager,
        *,
        memory_metadata_cache: "Optional[MemoryMetadataCache]" = None,
        memories_dir: Optional[Path] = None,
    ):
        """
        Initialize the AccessFilteringService.

        Args:
            group_access_manager: Manager for group and access data.
            memory_metadata_cache: Optional cache for memory file frontmatter.
                When provided, used by filter_cidx_meta_files() to look up
                scope/referenced_repo for UUID-stemmed memory files.
                When None, the service falls back to direct disk reads using
                memories_dir (slower but still correct).
            memories_dir: Directory containing memory files ({uuid}.md).
                Only used when memory_metadata_cache is None.
                When both are None, memory files are excluded (fail-closed).
        """
        self.group_manager = group_access_manager
        self._memory_metadata_cache: "Optional[MemoryMetadataCache]" = (
            memory_metadata_cache
        )
        self._memories_dir: Optional[Path] = memories_dir
        # Bug #338: TTL cache for _get_all_repo_aliases()
        self._repo_aliases_cache: Optional[Set[str]] = None
        self._repo_aliases_cache_time: float = 0.0
        # Bug #338: Register automatic cache invalidation on any repo access change
        group_access_manager.register_on_repo_change(self.invalidate_repo_aliases_cache)

    def get_accessible_repos(self, user_id: str) -> Set[str]:
        """
        Get set of repos accessible by user's group.

        cidx-meta is always included. For admin users, all repos are
        accessible (returns special marker for full access).

        Args:
            user_id: The user's unique identifier

        Returns:
            Set of repository names the user can access
        """
        group = self.group_manager.get_user_group(user_id)

        if not group:
            # User not assigned to any group - cidx-meta only
            return {CIDX_META_REPO}

        # Admin group has full access to ALL repos from ALL groups
        if group.name == self.ADMIN_GROUP_NAME:
            # Collect repos from ALL groups in the system
            all_repos: Set[str] = set()
            for grp in self.group_manager.get_all_groups():
                group_repos = self.group_manager.get_group_repos(grp.id)
                all_repos.update(group_repos)
            all_repos.add(CIDX_META_REPO)
            return all_repos

        # Regular group - get explicitly assigned repos
        repos = set(self.group_manager.get_group_repos(group.id))
        repos.add(CIDX_META_REPO)  # Always include cidx-meta
        return repos

    def is_admin_user(self, user_id: str) -> bool:
        """
        Check if user belongs to the admin group.

        Args:
            user_id: The user's unique identifier

        Returns:
            True if user is in admins group
        """
        group = self.group_manager.get_user_group(user_id)
        return group is not None and group.name == self.ADMIN_GROUP_NAME

    def _get_repo_alias(self, result: Any) -> str:
        """
        Get repository alias from a result, normalized for access checks.

        Handles both dict and object results. Falls back to source_repo when
        repository_alias is absent (omni-search results). Strips the -global
        suffix so aliases match stored repo names in get_accessible_repos().

        Args:
            result: A QueryResult object or dictionary

        Returns:
            The normalized repository alias, or empty string if not found
        """
        if isinstance(result, dict):
            alias = str(
                result.get("repository_alias", "") or result.get("source_repo", "")
            )
        else:
            alias = str(
                getattr(result, "repository_alias", "")
                or getattr(result, "source_repo", "")
            )
        # Strip -global suffix to match stored repo names in access control
        if alias.endswith("-global"):
            alias = alias[: -len("-global")]
        return alias

    def filter_query_results(self, results: List[Any], user_id: str) -> List[Any]:
        """
        Filter query results by user's accessible repos.

        Implements AC1 and AC2: Users only see results from repos their
        group can access. Admins see all results.

        Args:
            results: List of QueryResult objects or dictionaries with
                    repository_alias attribute/key
            user_id: The user's unique identifier

        Returns:
            Filtered list containing only results from accessible repos
        """
        if not results:
            return []

        # Admin users see everything
        if self.is_admin_user(user_id):
            return results

        accessible = self.get_accessible_repos(user_id)

        return [r for r in results if self._get_repo_alias(r) in accessible]

    def filter_repo_listing(self, repos: List[str], user_id: str) -> List[str]:
        """
        Filter repository listing by user's accessible repos.

        Implements AC4: Repository listing only returns repos the user's
        group can access.

        Args:
            repos: List of repository names
            user_id: The user's unique identifier

        Returns:
            Filtered list containing only accessible repos
        """
        if not repos:
            return []

        # Admin users see everything
        if self.is_admin_user(user_id):
            return repos

        accessible = self.get_accessible_repos(user_id)
        return [r for r in repos if r in accessible]

    def filter_cidx_meta_results(self, results: List[Any], user_id: str) -> List[Any]:
        """
        Filter cidx-meta summaries that reference inaccessible repos.

        Implements AC3: When querying cidx-meta, summaries referencing
        inaccessible repos are filtered out.

        Args:
            results: List of QueryResult objects from cidx-meta
            user_id: The user's unique identifier

        Returns:
            Filtered list with inaccessible repo references removed
        """
        if not results:
            return []

        # Admin users see everything
        if self.is_admin_user(user_id):
            return results

        accessible = self.get_accessible_repos(user_id)
        filtered = []

        for result in results:
            # Check if result has metadata with referenced_repo
            metadata = getattr(result, "metadata", None)
            if metadata is None:
                # No metadata - pass through
                filtered.append(result)
                continue

            referenced_repo = metadata.get("referenced_repo")
            if referenced_repo is None:
                # No referenced_repo in metadata - pass through
                filtered.append(result)
                continue

            # Only include if referenced repo is accessible
            if referenced_repo in accessible:
                filtered.append(result)

        return filtered

    def _get_all_repo_aliases(self) -> Set[str]:
        """
        Return the universe of all repo aliases known to the system.

        Collects repo aliases granted to every group. Used to distinguish
        repo-description .md files from general .md files (e.g. README.md).

        Bug #338: Result is cached with REPO_ALIASES_CACHE_TTL (default 60s)
        to avoid N+1 DB queries on every filter_cidx_meta_files() call.
        Use invalidate_repo_aliases_cache() to force a fresh query when
        group-repo assignments change.

        Returns:
            Set of all repo alias strings across all groups.
        """
        now = time.monotonic()
        if (
            self._repo_aliases_cache is not None
            and (now - self._repo_aliases_cache_time) < self.REPO_ALIASES_CACHE_TTL
        ):
            return self._repo_aliases_cache

        all_aliases: Set[str] = set()
        for grp in self.group_manager.get_all_groups():
            all_aliases.update(self.group_manager.get_group_repos(grp.id))

        self._repo_aliases_cache = all_aliases
        self._repo_aliases_cache_time = now
        return all_aliases

    def invalidate_repo_aliases_cache(self) -> None:
        """
        Invalidate the _get_all_repo_aliases() TTL cache.

        Bug #338: Call this method after any group-repo assignment change
        (grant or revoke) so the next call to filter_cidx_meta_files()
        reflects the updated state immediately rather than waiting for TTL.
        """
        self._repo_aliases_cache = None
        self._repo_aliases_cache_time = 0.0

    def filter_cidx_meta_files(self, files: List[str], user_id: str) -> List[str]:
        """
        Filter a list of cidx-meta filenames to only those the user may access.

        Bug #336: File-level access control for cidx-meta.

        Each repo-description file in cidx-meta is named ``{repo-alias}.md``.
        General files (e.g. ``README.md``, ``.gitignore``) are always visible.

        A ``.md`` file is treated as a repo-description file only when its stem
        (name without the ``.md`` extension) matches a known repo alias.
        Unknown stems pass through unconditionally.

        Admin users receive the full list unchanged.

        Args:
            files: List of filenames (basenames) from cidx-meta.
            user_id: The user's unique identifier.

        Returns:
            Filtered list of filenames the user is allowed to see.
        """
        if not files:
            return []

        # Admin users see everything
        if self.is_admin_user(user_id):
            return files

        accessible = self.get_accessible_repos(user_id)
        all_repo_aliases = self._get_all_repo_aliases()

        result = []
        for filename in files:
            if not filename.endswith(".md"):
                # Non-.md files (e.g. .gitignore, README.txt) always pass through
                result.append(filename)
            elif _MEMORY_FILE_RE.match(filename):
                # Memory file: UUID-stemmed .md — apply memory-aware access rules
                if self._is_memory_file_accessible(filename, accessible):
                    result.append(filename)
            else:
                stem = filename[: -len(".md")]
                if stem not in all_repo_aliases:
                    # Not a repo-description file (e.g. README.md) – always pass
                    result.append(filename)
                elif stem in accessible:
                    # Repo-description file the user is authorised to see
                    result.append(filename)
        return result

    def _is_memory_file_accessible(self, filename: str, accessible: Set[str]) -> bool:
        """Determine whether a UUID-stemmed memory file is accessible to the user.

        Looks up the file's frontmatter via the metadata cache (or disk when
        cache is absent). Applies scope rules:
          - scope=global: always accessible.
          - scope=repo or scope=file: accessible only if referenced_repo
            (with -global suffix stripped) is in the accessible set.
          - Any other/missing scope: fail closed (exclude).

        Fails closed: if metadata is unavailable or scope is unrecognised,
        returns False.

        Args:
            filename: Basename of the memory file (e.g. ``{uuid}.md``).
            accessible: Set of repo aliases the user can access.

        Returns:
            True if the file should be visible to the user.
        """
        memory_id = filename[: -len(".md")]
        metadata = self._lookup_memory_metadata(memory_id)
        if metadata is None:
            logger.debug(
                "filter_cidx_meta_files: metadata unavailable for memory %r — excluding",
                memory_id,
            )
            return False

        scope = metadata.get("scope")
        if scope == "global":
            return True

        # Fail closed for any unrecognised or missing scope value
        if scope not in {"repo", "file"}:
            logger.debug(
                "filter_cidx_meta_files: memory %r has unrecognised scope %r — excluding",
                memory_id,
                scope,
            )
            return False

        # scope in ("repo", "file"): check referenced_repo
        referenced_repo = metadata.get("referenced_repo")
        if not referenced_repo:
            logger.debug(
                "filter_cidx_meta_files: memory %r scope=%r missing referenced_repo — excluding",
                memory_id,
                scope,
            )
            return False

        # Strip -global suffix to match stored repo names in access control
        if referenced_repo.endswith("-global"):
            referenced_repo = referenced_repo[: -len("-global")]

        return referenced_repo in accessible

    def _lookup_memory_metadata(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """Return frontmatter dict for a memory, using cache when available.

        Falls back to direct disk read when no cache is injected.
        Returns None if the file is missing, unreadable, or both cache and
        memories_dir are absent (fail-closed in all error cases).

        Args:
            memory_id: 32-character hex memory identifier.

        Returns:
            Frontmatter dict, or None on any failure.
        """
        if self._memory_metadata_cache is not None:
            return self._memory_metadata_cache.get(memory_id)

        # No cache: fall back to direct disk read if memories_dir is known
        if self._memories_dir is None:
            logger.debug(
                "_lookup_memory_metadata: no cache and no memories_dir for %r — excluding",
                memory_id,
            )
            return None

        path = self._memories_dir / f"{memory_id}.md"
        try:
            fm, _body, _hash = read_memory_file(path)
            return fm
        except MemoryFileNotFoundError:
            logger.debug(
                "_lookup_memory_metadata: file not found for %r — excluding", memory_id
            )
            return None
        except MemoryFileCorruptError as exc:
            logger.debug(
                "_lookup_memory_metadata: corrupt file for %r — excluding: %s",
                memory_id,
                exc,
            )
            return None
        except Exception as exc:
            logger.warning(
                "_lookup_memory_metadata: unexpected error reading %r — excluding: %s",
                memory_id,
                exc,
            )
            return None

    def calculate_over_fetch_limit(self, requested_limit: int) -> int:
        """
        Calculate over-fetch limit for HNSW queries.

        To compensate for post-query filtering reducing results,
        we over-fetch from HNSW by a factor.

        Args:
            requested_limit: Original requested result limit

        Returns:
            Adjusted limit for HNSW query
        """
        return requested_limit * self.DEFAULT_OVER_FETCH_FACTOR
