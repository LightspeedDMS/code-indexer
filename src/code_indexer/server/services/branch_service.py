"""
Branch service for retrieving git branch information.

Provides branch listing functionality using real GitPython operations
without mocking, following CLAUDE.md Foundation #1 (Anti-Mock).
"""

import logging
import subprocess
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, NamedTuple, Optional, Protocol, Tuple

from git import Repo, InvalidGitRepositoryError, GitCommandError
from code_indexer.services.git_topology_service import GitTopologyService
from code_indexer.server.models.branch_models import (
    BranchInfo,
    CommitInfo,
    IndexStatus,
    RemoteTrackingInfo,
)
from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)

# Per-request latency: list_branches reads .commit for every local and remote
# ref (GitPython loads each commit object), which cost tens of seconds on repos
# with many refs and ran on every get_branches call. Cache the result per
# (codebase_dir, include_remote) for a short TTL so repeated calls reuse the
# walk. Branch state is eventually-consistent within the TTL.
_BRANCHES_CACHE_TTL_SECONDS = 30.0
_BRANCHES_CACHE_MAX = 128
_branches_cache: "OrderedDict[Tuple[str, bool], Tuple[float, List[BranchInfo]]]" = (
    OrderedDict()
)
_branches_cache_lock = threading.Lock()


#: ``git for-each-ref --format``: per ref, NUL-terminated fields ending with the
#: tip commit's full message. A commit message cannot hold a NUL, so the stream
#: splits unambiguously. The raw committer date keeps the commit's own UTC
#: offset, as GitPython reports it.
_BACKEND_REF_FORMAT = (
    "%(refname)%00%(objectname)%00%(authorname)%00%(committerdate:raw)"
    "%00%(HEAD)%00%(upstream:short)%00%(contents)%00"
)
_BACKEND_REF_FIELDS = 7
_BACKEND_REF_PATTERNS = ("refs/heads", "refs/remotes")
_LOCAL_PREFIX = "refs/heads/"
_REMOTE_PREFIX = "refs/remotes/"


class _BackendRef(NamedTuple):
    """One branch as the git backend lists it."""

    name: str
    local: bool
    sha: str
    author: str
    raw_date: str
    is_head: bool
    upstream: str
    message: str


class IndexStatusManager(Protocol):
    """Protocol for index status management to avoid tight coupling."""

    def get_branch_index_status(self, branch_name: str, repo_path: Path) -> IndexStatus:
        """Get index status for a specific branch."""
        ...


class BranchService:
    """Service for retrieving branch information using real git operations."""

    def __init__(
        self,
        git_topology_service: GitTopologyService,
        index_status_manager: Optional[IndexStatusManager] = None,
    ):
        """Initialize branch service.

        Args:
            git_topology_service: Service for git topology operations
            index_status_manager: Optional manager for index status (can be None for testing)
        """
        self.git_topology_service = git_topology_service
        self.index_status_manager = index_status_manager
        self._closed = False
        self._is_git_repo = self.git_topology_service.is_git_available()
        # The git seam (git-mcp) and the path it sees the repository at, when
        # the repository is not on this pod; GitPython needs a local .git.
        self._git_backend: Optional[Any] = None
        self._backend_repo_path = ""
        if not self._is_git_repo:
            self._is_git_repo = self._attach_delegated_backend()

        # Initialize git repo for branch operations (only if git is available)
        if self._is_git_repo and self._git_backend is None:
            try:
                self.repo = Repo(self.git_topology_service.codebase_dir)
            except InvalidGitRepositoryError as e:
                raise ValueError(f"Invalid git repository: {e}")
        else:
            self.repo = None  # type: ignore[assignment]

    def _attach_delegated_backend(self) -> bool:
        """On a pod that does not hold the repository, use the git seam.

        Local git found no repository at the codebase dir. With
        ``content_backend: delegated`` that is because the repository is with
        git-mcp: its answer is asked instead (only git's own "not a git
        repository" makes the path non-git). With no git seam to ask, branch
        listing REFUSES rather than answer "no branches". A pod-local
        (``local://``) golden repository is on this pod: local git answered.
        Elsewhere (local backend, CLI, tests) nothing changes: the path is
        simply not a repo.
        """
        from code_indexer.server.services.delegated_refusal import (
            refuse_when_delegated,
        )
        from code_indexer.server.startup.content_backend_wiring import (
            golden_repo_content_root,
            is_content_seam_delegated,
            is_pod_local_golden_path,
        )

        if not is_content_seam_delegated():
            return False
        if is_pod_local_golden_path(self.git_topology_service.codebase_dir):
            # A local:// repository's files are on this pod, never with
            # git-mcp: local git's "not a repository" is the answer.
            return False
        from code_indexer.server.startup.git_backend_wiring import (
            server_delegated_git_backend,
        )
        from code_indexer.services.git_head import probe_backend_repository

        backend = server_delegated_git_backend()
        if backend is None:
            refuse_when_delegated(
                "Branch listing",
                "git_backend is 'local', so no git-mcp holds the repository",
            )
        path = str(
            golden_repo_content_root(
                self.git_topology_service.codebase_dir, consumer="BranchService"
            )
        )
        is_repo, _ = probe_backend_repository(path, backend)
        if is_repo:
            self._git_backend = backend
            self._backend_repo_path = path
        return bool(is_repo)

    def list_branches(self, include_remote: bool = False) -> List[BranchInfo]:
        """List branches, CACHED per (codebase_dir, include_remote).

        The underlying walk (:meth:`_list_branches_uncached`) reads ``.commit``
        for every local and remote ref; on repos with many refs it cost tens of
        seconds and ran on every request. Results are cached for
        ``_BRANCHES_CACHE_TTL_SECONDS`` so repeated calls reuse the walk. Branch
        state is eventually-consistent within the TTL.
        """
        if not self._is_git_repo:
            return []

        key = (str(self.git_topology_service.codebase_dir), include_remote)
        now = time.monotonic()
        with _branches_cache_lock:
            entry = _branches_cache.get(key)
            if entry is not None and now < entry[0]:
                _branches_cache.move_to_end(key)
                return entry[1]

        branches = self._list_branches_uncached(include_remote=include_remote)

        # Anchor expiry to POST-walk time (not the pre-walk `now` captured
        # above). A walk that takes longer than the TTL must not be born
        # already-expired -- that would deliver zero cache benefit for
        # exactly the slow repos this cache targets.
        expiry = time.monotonic() + _BRANCHES_CACHE_TTL_SECONDS
        with _branches_cache_lock:
            _branches_cache[key] = (expiry, branches)
            _branches_cache.move_to_end(key)
            while len(_branches_cache) > _BRANCHES_CACHE_MAX:
                _branches_cache.popitem(last=False)
        return branches

    @staticmethod
    def invalidate(codebase_dir: "str | Path") -> None:
        """Invalidate all cached branch listings for ``codebase_dir``.

        Drops BOTH the ``include_remote=True`` and ``include_remote=False``
        cache entries so a branch mutation (git_branch_create/switch/delete)
        is immediately visible on the next :meth:`list_branches` call for
        this codebase_dir, instead of serving a stale listing for up to
        ``_BRANCHES_CACHE_TTL_SECONDS``. Thread-safe under the same lock
        used by :meth:`list_branches`.
        """
        key_str = str(Path(codebase_dir))
        with _branches_cache_lock:
            _branches_cache.pop((key_str, False), None)
            _branches_cache.pop((key_str, True), None)

    def _list_branches_uncached(self, include_remote: bool = False) -> List[BranchInfo]:
        """List all branches in the repository.

        Args:
            include_remote: Whether to include remote tracking information

        Returns:
            List of BranchInfo objects containing branch details (empty list for non-git repos)

        Raises:
            ValueError: If git operations fail
        """
        # Return empty list for non-git repositories
        if not self._is_git_repo:
            return []
        if self._git_backend is not None:
            return self._list_branches_through_backend(include_remote)

        try:
            branches = []
            current_branch_name = self.git_topology_service.get_current_branch()

            # Get all local branches; track names already added to deduplicate remote refs
            seen_branch_names: set = set()
            for branch in self.repo.heads:
                branch_info = self._create_branch_info(
                    branch,
                    is_current=(branch.name == current_branch_name),
                    include_remote=include_remote,
                )
                branches.append(branch_info)
                seen_branch_names.add(branch.name)

            # Add remote-only branches (those with no local counterpart)
            for remote in self.repo.remotes:
                for ref in remote.refs:
                    # Skip symbolic refs like origin/HEAD
                    if ref.name.endswith("/HEAD"):
                        continue
                    # Strip "<remote>/" prefix to get the bare branch name
                    prefix = remote.name + "/"
                    bare_name = (
                        ref.name[len(prefix) :]
                        if ref.name.startswith(prefix)
                        else ref.name
                    )
                    # Skip if this branch name was already added (local takes precedence)
                    if bare_name in seen_branch_names:
                        continue
                    branches.append(self._create_remote_branch_info(bare_name, ref))
                    seen_branch_names.add(bare_name)

            # Sort branches with current branch first, then alphabetically
            branches.sort(key=lambda b: (not b.is_current, b.name))

            return branches

        except (GitCommandError, InvalidGitRepositoryError):
            logger.error(
                format_error_log(
                    "APP-GENERAL-053",
                    "Git operation failed listing branches",
                ),
                exc_info=True,
            )
            raise  # Preserve original exception
        except Exception as e:
            logger.error(
                format_error_log(
                    "APP-GENERAL-054",
                    "Unexpected error listing branches",
                ),
                exc_info=True,
            )
            raise RuntimeError("Failed to retrieve branch information") from e

    def _backend_refs(self) -> List["_BackendRef"]:
        """Every local and remote branch with its tip commit, in ONE git-mcp
        call (``git for-each-ref``), whatever the number of branches."""
        assert self._git_backend is not None
        from code_indexer.global_repos.git_operations import (
            decode_like_run_git_command,
        )

        raw = self._git_backend.for_each_ref(
            self._backend_repo_path, _BACKEND_REF_FORMAT, _BACKEND_REF_PATTERNS
        )
        tokens = decode_like_run_git_command(bytes(raw)).split("\x00")
        # git ends every record with a newline after its last NUL: the stream
        # is records of 7 fields, then one "\n" leading the next record.
        tail = tokens.pop()
        if tail.strip() or len(tokens) % _BACKEND_REF_FIELDS:
            raise RuntimeError(
                f"Unexpected git for-each-ref output from git backend "
                f"({len(tokens)} fields, trailing {tail[:40]!r})"
            )
        refs = []
        for start in range(0, len(tokens), _BACKEND_REF_FIELDS):
            fields = tokens[start : start + _BACKEND_REF_FIELDS]
            fields[0] = fields[0].removeprefix("\n")
            refname, sha, author, raw_date, head, upstream, message = fields
            if refname.startswith(_LOCAL_PREFIX):
                name, local = refname[len(_LOCAL_PREFIX) :], True
            elif refname.startswith(_REMOTE_PREFIX):
                remote_ref = refname[len(_REMOTE_PREFIX) :]
                if remote_ref.endswith("/HEAD") or "/" not in remote_ref:
                    continue  # symbolic origin/HEAD, as the local walk skips it
                name, local = remote_ref.split("/", 1)[1], False
            else:
                continue
            refs.append(
                _BackendRef(
                    name,
                    local,
                    sha,
                    author,
                    raw_date,
                    head.strip() == "*",
                    upstream,
                    message.strip(),
                )
            )
        return refs

    def _backend_branch_info(
        self, ref: "_BackendRef", include_remote: bool
    ) -> BranchInfo:
        """:meth:`_create_branch_info` for a ref the git backend listed."""
        seconds, offset = ref.raw_date.split()
        sign = -1 if offset.startswith("-") else 1
        tz = timezone(
            sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5]))
        )
        commit_info = CommitInfo(
            sha=ref.sha,
            message=ref.message,
            author=ref.author,
            date=datetime.fromtimestamp(int(seconds), tz=tz).isoformat(),
        )
        remote_tracking = None
        if include_remote and ref.local and ref.upstream:
            remote_tracking = self._backend_tracking_info(ref)
        return BranchInfo(
            name=ref.name,
            is_current=ref.local and ref.is_head,
            last_commit=commit_info,
            index_status=self._get_index_status(ref.name),
            remote_tracking=remote_tracking,
        )

    def _backend_tracking_info(self, ref: "_BackendRef") -> RemoteTrackingInfo:
        """Ahead/behind of a local branch against its upstream, via git-mcp."""
        assert self._git_backend is not None
        try:
            ahead = self._git_backend.rev_list_count(
                self._backend_repo_path, f"{ref.upstream}..{ref.name}"
            )
            behind = self._git_backend.rev_list_count(
                self._backend_repo_path, f"{ref.name}..{ref.upstream}"
            )
        except subprocess.CalledProcessError as e:
            logger.warning(
                format_error_log(
                    "APP-GENERAL-058",
                    f"Failed to calculate ahead/behind for branch '{ref.name}': {e}",
                )
            )
            ahead = behind = 0
        return RemoteTrackingInfo(remote=ref.upstream, ahead=ahead, behind=behind)

    def _list_branches_through_backend(self, include_remote: bool) -> List[BranchInfo]:
        """:meth:`_list_branches_uncached` through git-mcp: one
        ``git for-each-ref`` listing every branch with its tip commit's message
        (GitPython reads the same commit objects). A backend failure raises."""
        refs = self._backend_refs()
        branches = [
            self._backend_branch_info(ref, include_remote) for ref in refs if ref.local
        ]
        seen = {b.name for b in branches}
        for ref in refs:
            if not ref.local and ref.name not in seen:
                branches.append(self._backend_branch_info(ref, False))
                seen.add(ref.name)
        branches.sort(key=lambda b: (not b.is_current, b.name))
        return branches

    def get_branch_by_name(self, branch_name: str) -> Optional[BranchInfo]:
        """Get information for a specific branch.

        Args:
            branch_name: Name of the branch to retrieve

        Returns:
            BranchInfo object if branch exists, None otherwise (always None for non-git repos)
        """
        # Return None for non-git repositories
        if not self._is_git_repo:
            return None
        if self._git_backend is not None:
            return next(
                (
                    self._backend_branch_info(ref, include_remote=False)
                    for ref in self._backend_refs()
                    if ref.local and ref.name == branch_name
                ),
                None,
            )

        try:
            # Check if branch exists
            for branch in self.repo.heads:
                if branch.name == branch_name:
                    current_branch_name = self.git_topology_service.get_current_branch()
                    return self._create_branch_info(
                        branch, is_current=(branch.name == current_branch_name)
                    )
            return None

        except (GitCommandError, InvalidGitRepositoryError):
            logger.error(
                format_error_log(
                    "APP-GENERAL-055",
                    f"Git operation failed getting branch '{branch_name}'",
                ),
                exc_info=True,
            )
            return None
        except Exception:
            logger.error(
                format_error_log(
                    "APP-GENERAL-056",
                    f"Unexpected error getting branch '{branch_name}'",
                ),
                exc_info=True,
            )
            return None

    def _create_remote_branch_info(self, name: str, ref) -> BranchInfo:
        """Create BranchInfo for a remote-only branch (no local counterpart).

        Remote refs are never the current branch and do not track another remote.
        This avoids calling .tracking_branch() on RemoteReference objects, which
        have different semantics than local Head objects.

        Args:
            name: Bare branch name (origin/ prefix already stripped)
            ref: Remote git reference object (RemoteReference or similar)

        Returns:
            BranchInfo with is_current=False and remote_tracking=None
        """
        last_commit = ref.commit
        commit_info = CommitInfo(
            sha=last_commit.hexsha,
            message=last_commit.message.strip(),
            author=last_commit.author.name,
            date=last_commit.committed_datetime.isoformat(),
        )
        index_status = self._get_index_status(name)
        return BranchInfo(
            name=name,
            is_current=False,
            last_commit=commit_info,
            index_status=index_status,
            remote_tracking=None,
        )

    def _create_branch_info(
        self, branch, is_current: bool = False, include_remote: bool = False
    ) -> BranchInfo:
        """Create BranchInfo object from git branch.

        Args:
            branch: Git branch object
            is_current: Whether this is the current branch
            include_remote: Whether to include remote tracking info

        Returns:
            BranchInfo object with all branch details
        """
        # Get last commit information
        last_commit = branch.commit
        commit_info = CommitInfo(
            sha=last_commit.hexsha,
            message=last_commit.message.strip(),
            author=last_commit.author.name,
            date=last_commit.committed_datetime.isoformat(),
        )

        # Get index status
        index_status = self._get_index_status(branch.name)

        # Get remote tracking info if requested.
        # RemoteReference objects represent remote branches that do not track another
        # remote — calling .tracking_branch() on them has different semantics than on
        # local Head objects, so skip the lookup entirely for remote refs.
        from git import RemoteReference

        remote_tracking = None
        if include_remote and not isinstance(branch, RemoteReference):
            remote_tracking = self._get_remote_tracking_info(branch)

        return BranchInfo(
            name=branch.name,
            is_current=is_current,
            last_commit=commit_info,
            index_status=index_status,
            remote_tracking=remote_tracking,
        )

    def _get_index_status(self, branch_name: str) -> IndexStatus:
        """Get index status for a branch.

        Args:
            branch_name: Name of the branch

        Returns:
            IndexStatus object with current indexing information
        """
        if self.index_status_manager:
            try:
                return self.index_status_manager.get_branch_index_status(
                    branch_name, self.git_topology_service.codebase_dir
                )
            except Exception as e:
                logger.warning(
                    format_error_log(
                        "APP-GENERAL-057",
                        f"Failed to get index status for branch '{branch_name}': {e}",
                    )
                )

        # Default status when index manager not available or fails
        return IndexStatus(
            status="not_indexed",
            files_indexed=0,
            total_files=None,
            last_indexed=None,
            progress_percentage=0.0,
        )

    def _get_remote_tracking_info(self, branch) -> Optional[RemoteTrackingInfo]:
        """Get remote tracking information for a branch.

        Args:
            branch: Git branch object

        Returns:
            RemoteTrackingInfo object if branch has remote tracking, None otherwise
        """
        try:
            # Check if branch has tracking branch
            tracking_branch = branch.tracking_branch()
            if not tracking_branch:
                return None

            # Calculate ahead/behind counts using GitPython native methods
            try:
                # Use GitPython iter_commits to count ahead/behind - no subprocess needed
                commits_ahead = list(
                    self.repo.iter_commits(f"{tracking_branch}..{branch}")
                )
                commits_behind = list(
                    self.repo.iter_commits(f"{branch}..{tracking_branch}")
                )

                ahead = len(commits_ahead)
                behind = len(commits_behind)

            except (GitCommandError, ValueError) as e:
                logger.warning(
                    format_error_log(
                        "APP-GENERAL-058",
                        f"Failed to calculate ahead/behind for branch '{branch.name}': {e}",
                    )
                )
                behind = ahead = 0

            return RemoteTrackingInfo(
                remote=tracking_branch.name, ahead=ahead, behind=behind
            )

        except Exception as e:
            logger.warning(
                format_error_log(
                    "APP-GENERAL-059",
                    f"Failed to get remote tracking info for branch '{branch.name}': {e}",
                )
            )
            return None

    def close(self):
        """Clean up repository resources.

        Closes the GitPython repository object to release file handles
        and other system resources.
        """
        if not self._closed and hasattr(self, "repo") and self.repo is not None:
            try:
                self.repo.close()
            except Exception as e:
                logger.warning(
                    format_error_log(
                        "APP-GENERAL-060",
                        f"Error closing git repository: {e}",
                    )
                )
            finally:
                self._closed = True

    def __del__(self):
        """Ensure cleanup even if close() not called explicitly.

        This is a safety net for resource cleanup, following CLAUDE.md
        Foundation #8 correct pattern for resource management.
        """
        self.close()

    def __enter__(self):
        """Context manager entry.

        Returns:
            Self for use in with statement
        """
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit with guaranteed cleanup.

        Args:
            exc_type: Exception type if any
            exc_val: Exception value if any
            exc_tb: Exception traceback if any
        """
        self.close()

    def _validate_branch_name(self, branch_name: str) -> bool:
        """Validate branch name for security.

        Prevents command injection by validating branch names contain
        only safe characters. Follows CLAUDE.md security principles.

        Args:
            branch_name: Branch name to validate

        Returns:
            True if branch name is safe, False otherwise
        """
        if not branch_name or not branch_name.strip():
            return False
        if len(branch_name) > 255:  # Git ref name limit
            return False

        # Reject path traversal attempts
        if ".." in branch_name or branch_name.startswith("/"):
            return False

        # Reject command injection characters
        dangerous_chars = [";", "&", "|", "`", "$", "(", ")", "\\n", "\\x00"]
        if any(char in branch_name for char in dangerous_chars):
            return False

        # Only allow safe characters - alphanumeric, slash, dash, dot, underscore
        # Simple character check without regex to avoid import
        allowed_chars = set(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/_.-"
        )
        return all(char in allowed_chars for char in branch_name)
