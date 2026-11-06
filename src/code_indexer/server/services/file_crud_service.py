"""
File CRUD Service.

Provides create, read, update, delete operations for files in activated repositories
with security validation and optimistic concurrency control.

Security features:
- .git/ directory blocking
- Path traversal prevention
- Symlink resolution and validation

Concurrency control:
- SHA-256 hash-based optimistic locking
- Atomic file operations (temp file + rename)
"""

import hashlib
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Any, Optional, Protocol, Tuple, TYPE_CHECKING

from code_indexer.global_repos.write_mode_markers import WriteModeMarkers
from code_indexer.utils.blob_hash import git_blob_hash_bytes
from code_indexer.utils.file_locking import nfs_safe_fsync, nfs_safe_link_exclusive
from code_indexer.utils.path_confinement import (
    reject_if_within_git_directory,
    reject_if_within_index_directory,
    resolve_confined_path,
)

if TYPE_CHECKING:
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )
    from code_indexer.server.services.golden_repo_writer import (
        Expected,
        GoldenRepoWriteTarget,
    )
    from code_indexer.server.services.cidx_meta_access.file_doors import (
        CatalogFileAccess,
    )
    from code_indexer.server.services.xray_pattern_authority import PatternWriter

_UTF8_BOM = b"\xef\xbb\xbf"
#: Why a content hash may not match, as each tool has always worded it.
_EDIT_STALE_REASON = "File may have been modified by another process."
_DELETE_STALE_REASON = "File may have been modified since hash was computed."

logger = logging.getLogger(__name__)

# A door's catalog judgement of the directory a write lands in.
AccessFor = Callable[[str], "CatalogFileAccess"]


def _get_activated_repo_manager() -> "ActivatedRepoManager":
    """Get the DI-wired ActivatedRepoManager from app.state (Bug #1692).

    Mirrors stats_service.py's identical `_get_activated_repo_manager()`
    (Bug #1683) and mcp/handlers/_utils.py's `_get_activated_repo_manager()`
    (the same one auto_watch_manager's caller uses).

    A per-instance `FileCRUDService.activated_repo_manager` lazy property
    (the Bug #1689 fix) used to exist and was deliberately NOT used for
    repo-validity resolution: it would have constructed a fresh,
    NODE-LOCAL, UNPOOLED `ActivatedRepoManager` whose
    `user_has_activated_repo()`/`get_activated_repo_path()` fall back to
    scanning node-local `{alias}_metadata.json` files
    (`_list_user_repos_fs`). In cluster/PostgreSQL mode, activation writes
    to the `activated_repos` DB table only (`_save_metadata_pg`) and NEVER
    writes that JSON file -- so a genuinely activated repo looks
    indistinguishable from an orphan to that local instance, and Bug #1692's
    registry gate would refuse 100% of legitimate cluster writes. Bug #1703
    removed that property entirely once it was confirmed to have zero
    remaining production consumers (its only call site was this function,
    since #1692). Both front doors (the MCP module-level `file_crud_service`
    singleton AND `routers/files.py`, which constructs a FRESH
    `FileCRUDService()` per REST request) need this resolved AT CALL TIME
    via app.state rather than cached on any one instance -- do NOT
    reintroduce a per-instance node-local `ActivatedRepoManager` property
    on this class for repo-validity resolution.
    """
    from typing import cast
    from ..app import app as app_module

    manager = getattr(app_module.state, "activated_repo_manager", None)
    if manager is None:
        raise RuntimeError(
            "activated_repo_manager not initialized in app.state. "
            "Server must set app.state.activated_repo_manager during startup."
        )
    return cast("ActivatedRepoManager", manager)


class HashMismatchError(Exception):
    """
    Exception raised when content hash validation fails.

    This indicates the file was modified since the hash was computed,
    representing a concurrent modification conflict.
    """

    pass


class CRUDOperationError(Exception):
    """
    Base exception for CRUD operation failures.

    All file CRUD operations may raise this exception or its subclasses
    to indicate various failure conditions.
    """

    pass


class WriteModeLockUnavailableError(CRUDOperationError):
    """The write-mode lock store could not confirm or deny ownership (a
    transient failure): nothing was written; retry, do not re-enter write
    mode."""


class FileCRUDService:
    """
    Service for file CRUD operations in activated repositories.

    Provides secure file manipulation with:
    - Security validation (blocks .git/, prevents path traversal)
    - Optimistic concurrency control (hash-based locking)
    - Atomic operations (temp file + rename pattern)

    All operations require:
    - repo_alias: User's repository alias
    - username: Username for ActivatedRepoManager lookup

    Write Exceptions (Story #197):
    - Allows direct editing of golden repos like cidx-meta
    - Bypasses activated repo requirement for registered aliases
    - Uses canonical golden repo path instead of activated copy
    """

    def __init__(self):
        """Initialize file CRUD service."""
        # Bug #1689 (construction is cheap): FileCRUDService.__init__ must
        # never construct a real ActivatedRepoManager (-> GoldenRepoManager
        # -> SQLite golden-repo load -> bgm-worker/bgm-temporal-worker
        # thread spawn) as a side effect of mere instantiation. Bug #1703
        # removed the lazy `activated_repo_manager` property that used to
        # exist here (and the per-instance/class-level state that backed
        # it) once it was confirmed to have zero production consumers:
        # _resolve_repo_path resolves the manager via the module-level,
        # DI/app.state-wired _get_activated_repo_manager() function
        # instead (Bug #1692) -- do NOT reintroduce a per-instance
        # node-local ActivatedRepoManager property on this class.
        # Write exceptions map: alias -> canonical path (Story #197)
        self._global_write_exceptions: Dict[str, Path] = {}
        # Golden repos directory for write-mode marker lookup (Story #231).
        # Set at startup via set_golden_repos_dir() or directly via _golden_repos_dir.
        self._golden_repos_dir: Optional[Path] = None
        # ^ invariant wired at startup (service_init); None refuses every write-mode write
        self._write_mode_lease_renewer: Optional[Callable[[Path, str], bool]] = None

    def register_write_exception(self, alias: str, canonical_path: Path) -> None:
        """
        Register a golden repo alias for direct write access (Story #197).

        Allows power users to edit golden repos directly without activation.
        Used for cidx-meta dependency map corrections.

        Args:
            alias: Repository alias (e.g., 'cidx-meta-global')
            canonical_path: Canonical path to golden repo
        """
        self._global_write_exceptions[alias] = canonical_path

    def is_write_exception(self, repo_alias: str) -> bool:
        """
        Check if a repository alias is registered as a write exception.

        Args:
            repo_alias: Repository alias to check

        Returns:
            True if alias is in write exceptions map, False otherwise
        """
        return repo_alias in self._global_write_exceptions

    def get_write_exception_path(self, repo_alias: str) -> Optional[Path]:
        """
        Get the canonical path for a write exception alias.

        Args:
            repo_alias: Repository alias to look up

        Returns:
            Canonical path if alias is registered, None otherwise
        """
        return self._global_write_exceptions.get(repo_alias)

    def set_golden_repos_dir(self, golden_repos_dir: Path) -> None:
        """
        Set the golden repos directory for write-mode marker lookup (Story #231).

        Called at startup to inject the golden repos path so that write-mode
        enforcement can find the write-mode markers for that directory.

        Args:
            golden_repos_dir: Path to the golden repos root directory
        """
        self._golden_repos_dir = Path(golden_repos_dir)

    def set_write_mode_lease_renewer(
        self, renewer: Callable[[Path, str], bool]
    ) -> None:
        """Install renewer(golden_repos_dir, alias): renews the write-mode
        lock by the marker's token, True only when ownership is confirmed."""
        self._write_mode_lease_renewer = renewer

    def _check_write_mode_active(self, repo_alias: str) -> None:
        """
        Enforce write-mode gate for write-exception repos (Story #231).

        Write-exception repos (e.g. cidx-meta-global) require callers to call
        enter_write_mode() before issuing CRUD operations.  This is signalled by
        a write-mode marker for {alias_without_global} (control-plane metadata,
        see global_repos/write_mode_markers.py).

        If the repo is NOT a write exception, this check is a no-op.

        Args:
            repo_alias: Repository alias (e.g. 'cidx-meta-global')

        Raises:
            PermissionError: If the repo is a write exception and no active
                             write-mode marker exists, or the marker's token
                             no longer owns the write-mode lock.
        """
        if not self.is_write_exception(repo_alias):
            return

        # Determine marker path
        golden_repos_dir = self._golden_repos_dir
        if golden_repos_dir is None:
            raise PermissionError(
                f"Cannot verify write-mode for '{repo_alias}': "
                f"golden_repos_dir not configured. Server may not be fully initialized."
            )

        if not WriteModeMarkers(golden_repos_dir).exists(repo_alias):
            raise PermissionError(
                f"Repo '{repo_alias}' requires write mode. "
                f"Call enter_write_mode('{repo_alias}') before performing file operations."
            )
        alias_without_global = (
            repo_alias[: -len("-global")]
            if repo_alias.endswith("-global")
            else repo_alias
        )
        renewer = self._write_mode_lease_renewer
        if renewer is None or not renewer(golden_repos_dir, alias_without_global):
            raise PermissionError(
                f"Write mode for '{repo_alias}' no longer holds its write lock; "
                f"call enter_write_mode('{repo_alias}') again. Nothing was written."
            )

    def _resolve_repo_path(self, repo_alias: str, username: str) -> Path:
        """
        Resolve repository path, checking write exceptions first.

        Args:
            repo_alias: Repository alias
            username: Username (for activated repo lookup)

        Returns:
            Path to repository (exception path or activated repo path)

        Raises:
            ValueError: If repo not found in either exceptions or activated repos
            FileNotFoundError: If repo_alias is not a registered activated
                workspace for username (Bug #1692), or its activated repo
                path does not exist on disk (Bug #394, #395)
        """
        # Check write exceptions first (Story #197 AC1)
        if repo_alias in self._global_write_exceptions:
            return self._global_write_exceptions[repo_alias]

        not_activated_error = FileNotFoundError(
            f"Repository '{repo_alias}' is not an activated workspace for user "
            f"'{username}'. Use activate_repository to create a writable workspace, "
            f"or check the repository alias is correct."
        )

        # Bug #1692: Gate on the registry -- the SAME authority
        # auto_watch_manager uses via user_has_activated_repo()
        # (mcp/handlers/files.py::_start_auto_watch_if_needed, Bug #1683
        # round 3) -- rather than mere filesystem existence. Prior to this
        # fix, an "orphan" repository_alias directory (exists on disk, no
        # registry entry) was silently ACCEPTED here while the same alias
        # was correctly REFUSED by auto_watch_manager in the same request,
        # letting a write land in an unregistered directory. The bare
        # existence check below stays as defense-in-depth for the
        # different failure mode of a registered repo whose directory was
        # removed out from under it.
        #
        # Resolved via the module-level _get_activated_repo_manager()
        # (DI/app.state-wired, Bug #1692 cluster-mode fix) rather than a
        # per-instance self.activated_repo_manager property (a node-local,
        # unpooled instance whose registry check falls back to scanning
        # local {alias}_metadata.json files -- files PostgreSQL-mode
        # activation never writes, which misclassified every genuinely
        # activated cluster repo as an orphan). That property has since
        # been removed entirely (Bug #1703): once #1692 redirected this
        # call site away from it, it had zero remaining production
        # consumers. Do NOT reintroduce it.
        repo_manager = _get_activated_repo_manager()

        if not repo_manager.user_has_activated_repo(username, repo_alias):
            raise not_activated_error

        # Fall back to activated repo manager
        repo_path_str = repo_manager.get_activated_repo_path(
            username=username, user_alias=repo_alias
        )
        repo_path = Path(repo_path_str)

        # Bug #394/#395: Validate the activated repo path actually exists on disk.
        # get_activated_repo_path() always constructs a path string without checking
        # existence. Even a registered alias could have its directory removed out
        # from under it; deny operations on non-existent paths with the same error.
        # Content seam: a copy that is not on this pod (workspace-mcp) has only
        # the registry gate above; the reader/writer then fail loudly on a copy
        # missing there (never an empty repository).
        from code_indexer.server.services.activated_copy_content import (
            activated_copy_present,
        )

        if not activated_copy_present(repo_manager, username, repo_alias, repo_path):
            raise not_activated_error

        return repo_path

    @staticmethod
    def _refuse_hidden_catalog_path(
        repo_path: Path, file_path: str, access_for: Optional[AccessFor]
    ) -> None:
        """The catalog write gate, before any existence check or read:
        *access_for* judges the directory written (a door passing none is
        refused on a catalog directory)."""
        from code_indexer.server.services.cidx_meta_access.file_doors import (
            refuse_hidden_catalog_writes,
        )

        served = str(repo_path)
        access = access_for(served) if access_for is not None else None
        refuse_hidden_catalog_writes(served, [file_path], access)

    def _refuse_pattern_write(
        self,
        repo_alias: str,
        repo_path: Path,
        file_path: str,
        resolved: Path,
        replacing: bool,
        writer: Optional["PatternWriter"],
    ) -> None:
        """The X-Ray pattern rule (``xray_pattern_authority``) for a write
        on the catalog write exception, after the write-mode gate and before
        anything is written. Judged: the path as requested (nothing
        resolved), its link node and the *resolved* target, so a link
        anywhere on the path never moves the decision."""
        from code_indexer.server.services.constants import is_internal_meta_repo
        from code_indexer.server.services.xray_pattern_authority import (
            refuse_catalog_pattern_write,
        )

        if not (
            self.is_write_exception(repo_alias) and is_internal_meta_repo(repo_alias)
        ):
            return
        requested = Path(os.path.realpath(repo_path)) / os.path.normpath(file_path)
        targets = [requested, self._lexical_node(repo_path, file_path), resolved]
        refuse_catalog_pattern_write(
            repo_path, targets, replacing=replacing, writer=writer
        )

    @staticmethod
    def _lexical_node(repo_path: Path, file_path: str) -> Path:
        """The node *file_path* names, its final component not followed:
        the confined real parent joined with the literal name."""
        name = Path(file_path).name
        if name in ("", ".", ".."):
            raise PermissionError("Access denied")
        return resolve_confined_path(repo_path, str(Path(file_path).parent)) / name

    def _validate_path_or_refuse(
        self,
        repo_alias: str,
        file_path: str,
        username: str,
        operation: str,
        access_for: Optional[AccessFor],
    ) -> None:
        """``_validate_crud_path``; a path it rejects gets the catalog write
        refusal instead only when a restricted catalog view is established
        for the repository, so every spelling answers alike there. In every
        other case, a failing resolution or judgement included, the original
        error is raised unchanged."""
        from code_indexer.server.services.cidx_meta_access.file_doors import (
            CATALOG_WRITE_REFUSAL,
            restricted_catalog_view_applies,
        )

        try:
            self._validate_crud_path(file_path, operation)
        except PermissionError as invalid:
            try:
                served = str(self._resolve_repo_path(repo_alias, username))
                access = access_for(served) if access_for is not None else None
                restricted = restricted_catalog_view_applies(served, access)
            except Exception as judging:
                # ^ inv original validation error survives any judging failure
                logger.debug(
                    "Catalog judgement of a rejected path failed (%s)",
                    type(judging).__name__,
                )
                raise invalid from None
            if restricted:
                raise PermissionError(CATALOG_WRITE_REFUSAL) from None
            raise

    def create_file(
        self,
        repo_alias: str,
        file_path: str,
        content: str,
        username: str,
        access_for: Optional[AccessFor] = None,
        pattern_writer: Optional["PatternWriter"] = None,
    ) -> Dict[str, Any]:
        """
        Create a new file in the activated repository.

        Args:
            repo_alias: User's repository alias
            file_path: Relative path to file within repository
            content: File content as string
            username: Username for repository lookup

        Returns:
            Dictionary with:
                - success: True
                - file_path: Created file path
                - content_hash: SHA-256 hash of content
                - size_bytes: File size in bytes
                - created_at: ISO timestamp

        Raises:
            FileExistsError: If file already exists
            PermissionError: If path validation fails (security)
            CRUDOperationError: If creation fails
        """
        # Enforce write-mode gate for write-exception repos (Story #231)
        self._check_write_mode_active(repo_alias)

        # Validate path security
        self._validate_path_or_refuse(
            repo_alias, file_path, username, "create_file", access_for
        )

        # Resolve repository path (checks write exceptions first - Story #197)
        repo_path = self._resolve_repo_path(repo_alias, username)
        self._refuse_hidden_catalog_path(repo_path, file_path, access_for)
        full_path = resolve_confined_path(repo_path, file_path)
        reject_if_within_git_directory(repo_path, full_path, "create_file")
        reject_if_within_index_directory(repo_path, full_path, "create_file")
        self._refuse_pattern_write(
            repo_alias, repo_path, file_path, full_path, False, pattern_writer
        )

        # Content seam: a golden repo that is not on this pod is written
        # through the content writer; one on this pod's disk as it always was.
        return self._writes_for(repo_alias, repo_path).create(file_path, content)

    def edit_file(
        self,
        repo_alias: str,
        file_path: str,
        old_string: str,
        new_string: str,
        content_hash: str,
        replace_all: bool,
        username: str,
        access_for: Optional[AccessFor] = None,
        pattern_writer: Optional["PatternWriter"] = None,
    ) -> Dict[str, Any]:
        """
        Edit a file by replacing string occurrences.

        Uses optimistic locking: validates content_hash matches current file
        before applying changes.

        Args:
            repo_alias: User's repository alias
            file_path: Relative path to file within repository
            old_string: String to replace
            new_string: Replacement string
            content_hash: Expected SHA-256 hash of current content
            replace_all: If True, replace all occurrences; if False, require unique match
            username: Username for repository lookup

        Returns:
            Dictionary with:
                - success: True
                - file_path: Edited file path
                - content_hash: New SHA-256 hash after edit
                - modified_at: ISO timestamp
                - changes_made: Number of replacements made

        Raises:
            HashMismatchError: If content_hash doesn't match current file
            ValueError: If old_string is not unique and replace_all=False
            FileNotFoundError: If file doesn't exist
            PermissionError: If path validation fails
            CRUDOperationError: If edit fails
        """
        # Enforce write-mode gate for write-exception repos (Story #231)
        self._check_write_mode_active(repo_alias)

        # Validate path security
        self._validate_path_or_refuse(
            repo_alias, file_path, username, "edit_file", access_for
        )

        # Resolve repository path (checks write exceptions first - Story #197)
        repo_path = self._resolve_repo_path(repo_alias, username)
        self._refuse_hidden_catalog_path(repo_path, file_path, access_for)
        full_path = resolve_confined_path(repo_path, file_path)
        reject_if_within_git_directory(repo_path, full_path, "edit_file")
        reject_if_within_index_directory(repo_path, full_path, "edit_file")
        self._refuse_pattern_write(
            repo_alias, repo_path, file_path, full_path, True, pattern_writer
        )

        # Content seam (see create_file).
        return self._writes_for(repo_alias, repo_path).edit(
            file_path, old_string, new_string, content_hash, replace_all
        )

    def delete_file(
        self,
        repo_alias: str,
        file_path: str,
        content_hash: Optional[str],
        username: str,
        access_for: Optional[AccessFor] = None,
        pattern_writer: Optional["PatternWriter"] = None,
    ) -> Dict[str, Any]:
        """
        Delete a file from the activated repository.

        Optionally validates content hash before deletion for additional safety.

        Args:
            repo_alias: User's repository alias
            file_path: Relative path to file within repository
            content_hash: Optional SHA-256 hash to validate before deletion
            username: Username for repository lookup

        Returns:
            Dictionary with:
                - success: True
                - file_path: Deleted file path
                - deleted_at: ISO timestamp

        Raises:
            HashMismatchError: If content_hash provided and doesn't match
            FileNotFoundError: If file doesn't exist
            PermissionError: If path validation fails
            CRUDOperationError: If deletion fails
        """
        # Enforce write-mode gate for write-exception repos (Story #231)
        self._check_write_mode_active(repo_alias)

        # Validate path security
        self._validate_path_or_refuse(
            repo_alias, file_path, username, "delete_file", access_for
        )

        # Resolve repository path (checks write exceptions first - Story #197)
        repo_path = self._resolve_repo_path(repo_alias, username)
        self._refuse_hidden_catalog_path(repo_path, file_path, access_for)

        # Content seam (see create_file).
        return self._writes_for(repo_alias, repo_path).delete(file_path, content_hash)

    # ── repos on this pod's disk ─────────────────────────────────────────

    def _create_on_pod_disk(
        self, repo_path: Path, file_path: str, content: str
    ) -> Dict[str, Any]:
        """create_file for a repository on this pod's disk."""
        # Bug #1891: resolve + confine BEFORE any exists()/open() call.
        # _validate_crud_path() (run by the caller) only rejects literal ".." components
        # and absolute paths -- it does not catch a symlink already
        # present inside the repository (e.g. tracked in the underlying
        # git repo) that points outside the repository root.
        full_path = resolve_confined_path(repo_path, file_path)

        # Bug #1891 (final SECURITY round): a symlink tracked INSIDE the
        # repository (e.g. "gitlink" -> ".git") is itself confined to
        # repo_path, so resolve_confined_path() above does not catch it --
        # the RESOLVED target can still land inside ".git" (e.g.
        # repo_path/.git/hooks/pre-commit), letting a write-mode user
        # plant a hook that executes on the server's next `git commit`.
        reject_if_within_git_directory(repo_path, full_path, "create_file")
        reject_if_within_index_directory(repo_path, full_path, "create_file")

        # Check if file already exists
        if full_path.exists():
            raise FileExistsError(f"File already exists: {file_path}")

        # Create parent directories if needed
        full_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write
        self._atomic_write_file(full_path, content)

        return self._created_result(file_path, content)

    def _edit_on_pod_disk(
        self,
        repo_path: Path,
        file_path: str,
        old_string: str,
        new_string: str,
        content_hash: str,
        replace_all: bool,
    ) -> Dict[str, Any]:
        """edit_file for a repository on this pod's disk."""
        # Bug #1891: resolve + confine BEFORE any exists()/open() call.
        # _validate_crud_path() (run by the caller) only rejects literal ".." components
        # and absolute paths -- it does not catch a symlink already
        # present inside the repository (e.g. tracked in the underlying
        # git repo) that points outside the repository root.
        full_path = resolve_confined_path(repo_path, file_path)

        # Bug #1891 (final SECURITY round): see create_file's identical
        # comment -- a tracked in-repo symlink (e.g. "gitlink" -> ".git")
        # is confined to repo_path yet still resolves into ".git".
        reject_if_within_git_directory(repo_path, full_path, "edit_file")
        reject_if_within_index_directory(repo_path, full_path, "edit_file")

        if not full_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        # Read current content
        try:
            with open(full_path, "rb") as f:
                current_content_bytes = f.read()
        except Exception as e:
            raise CRUDOperationError(
                f"Failed to read file '{file_path}': {str(e)}"
            ) from e

        # Validate hash (optimistic locking)
        self._verify_hash(
            current_content_bytes, content_hash, file_path, _EDIT_STALE_REASON
        )

        new_content, changes_made, has_bom = self._apply_edit(
            current_content_bytes, old_string, new_string, replace_all, file_path
        )

        # Atomic write (Bug #570: preserve BOM if original had one)
        self._atomic_write_file(full_path, new_content, has_bom=has_bom)

        return self._edited_result(file_path, new_content, changes_made)

    def _delete_on_pod_disk(
        self, repo_path: Path, file_path: str, content_hash: Optional[str]
    ) -> Dict[str, Any]:
        """delete_file for a repository on this pod's disk."""
        # Bug #1891: resolve + confine BEFORE any exists()/open() call.
        # _validate_crud_path() (run by the caller) only rejects literal ".." components
        # and absolute paths -- it does not catch a symlink already
        # present inside the repository (e.g. tracked in the underlying
        # git repo) that points outside the repository root.
        full_path = resolve_confined_path(repo_path, file_path)

        # Bug #1891 round 2 (S3): the actual removal below MUST target the
        # LEXICAL (unresolved) path the caller named, never the RESOLVED
        # `full_path`. os.remove()/unlink() never follows a symlink at its
        # FINAL path component -- removing via the lexical path deletes
        # only the symlink node itself, matching what the caller asked to
        # delete. Removing via `full_path` (the confinement-resolved
        # target) deleted the symlink's TARGET file instead and left the
        # symlink dangling -- a round-1 regression. `full_path` remains the
        # correct path for the containment decision and for reading
        # content (hash validation, below).
        #
        # Bug #1891 round 3 (D1): a naive lexical_path = Path(repo_path) /
        # file_path is STILL not safe to pass to os.remove() as-is. The OS
        # follows symlinks in every PARENT path component when resolving a
        # path (only the FINAL component is left un-followed by
        # os.remove()/unlink()). If a PARENT component is itself a symlink
        # escaping the repository (e.g. a tracked "link_to_parent -> .."
        # inside the repo), the lexical path can resolve -- at the kernel
        # level, when os.remove() is called -- to a node OUTSIDE the
        # repository, even though `full_path` above (which follows ALL
        # symlinks, including the final one) happens to land back inside
        # the repo and passes containment (e.g. file_path=
        # "link_to_parent/pointer" where the outside "pointer" symlink
        # happens to point at a file inside the repo). Confining the
        # PARENT directory separately (following ITS symlinks) and then
        # joining the final component's literal NAME onto that
        # confinement-verified parent closes this gap: an escaping parent
        # symlink is rejected here, while a same-repo final-component
        # symlink (no escaping parent) still resolves the parent to its
        # own real, in-repo location and joins the literal name onto it --
        # removing only the symlink node, never its target, exactly as
        # round 2 intended.
        lexical_path = self._lexical_node(repo_path, file_path)

        # Bug #1891 (final SECURITY round): see create_file's identical
        # comment -- a tracked in-repo symlink (e.g. "gitlink" -> ".git")
        # is confined to repo_path yet still resolves into ".git". Check
        # BOTH the resolved containment target (full_path) and the actual
        # node os.remove() will operate on (lexical_path) -- they can
        # differ (e.g. file_path="gitlink" resolves full_path to
        # repo_path/.git itself, while lexical_path is the "gitlink"
        # symlink node; either landing inside/at ".git" is forbidden).
        reject_if_within_git_directory(repo_path, full_path, "delete_file")
        reject_if_within_git_directory(repo_path, lexical_path, "delete_file")
        reject_if_within_index_directory(repo_path, full_path, "delete_file")
        reject_if_within_index_directory(repo_path, lexical_path, "delete_file")

        if not full_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        # Validate hash if provided
        if content_hash is not None:
            try:
                with open(full_path, "rb") as f:
                    current_content_bytes = f.read()
            except Exception as e:
                raise CRUDOperationError(
                    f"Failed to read file '{file_path}' for hash validation: {str(e)}"
                ) from e

            self._verify_hash(
                current_content_bytes, content_hash, file_path, _DELETE_STALE_REASON
            )

        # Delete file (the symlink/file node the caller named -- see the
        # lexical_path comment above)
        try:
            os.remove(str(lexical_path))
        except Exception as e:
            raise CRUDOperationError(
                f"Failed to delete file '{file_path}': {str(e)}"
            ) from e

        return self._deleted_result(file_path)

    # ── repos behind the content seam ────────────────────────────────────

    def _writes_for(self, repo_alias: str, repo_path: Path) -> "_RepositoryWrites":
        """How this service writes *repo_path*: decided once, by the content
        seam's implementation (see :meth:`_golden_write_target`)."""
        target = self._golden_write_target(repo_alias, repo_path)
        if target is None:
            return _PodDiskWrites(self, repo_path)
        return _WriterWrites(self, target)

    def _golden_write_target(
        self, repo_alias: str, repo_path: Path
    ) -> Optional["GoldenRepoWriteTarget"]:
        """Where a repo is written when it is not on this pod, or None to write
        the local disk -- always None under the local content backend.

        Both kinds of repo this service writes go through the content writer
        under a delegated backend: a write-exception (golden) repo, and a
        user's activated copy, which lives on workspace-mcp at its path.
        """
        from code_indexer.server.services.golden_repo_writer import (
            golden_repo_write_target,
        )

        return golden_repo_write_target(repo_path)

    def _create_through_writer(
        self, target: "GoldenRepoWriteTarget", file_path: str, content: str
    ) -> Dict[str, Any]:
        """create_file behind the seam: "must not exist" is the writer's CAS."""
        rel = target.confine(file_path)
        data = self._encode_for_write(target.absolute(rel), content, has_bom=False)
        target.put(rel, data, None, file_path=file_path, operation="create_file")
        return self._created_result(file_path, content)

    def _edit_through_writer(
        self,
        target: "GoldenRepoWriteTarget",
        file_path: str,
        old_string: str,
        new_string: str,
        content_hash: str,
        replace_all: bool,
    ) -> Dict[str, Any]:
        """edit_file behind the seam: the write is conditional on the blob read."""
        rel = target.confine(file_path)
        current = target.read(rel, file_path)
        self._verify_hash(current, content_hash, file_path, _EDIT_STALE_REASON)
        new_content, changes_made, has_bom = self._apply_edit(
            current, old_string, new_string, replace_all, file_path
        )
        data = self._encode_for_write(
            target.absolute(rel), new_content, has_bom=has_bom
        )
        target.put(
            rel,
            data,
            git_blob_hash_bytes(current),
            file_path=file_path,
            operation="edit_file",
        )
        return self._edited_result(file_path, new_content, changes_made)

    def _delete_through_writer(
        self,
        target: "GoldenRepoWriteTarget",
        file_path: str,
        content_hash: Optional[str],
    ) -> Dict[str, Any]:
        """delete_file behind the seam: conditional exactly when a hash is given."""
        from code_indexer.server.services.golden_repo_writer import UNCONDITIONAL

        rel = target.confine(file_path)
        expected: "Expected" = UNCONDITIONAL
        if content_hash is not None:
            current = target.read(rel, file_path)
            self._verify_hash(current, content_hash, file_path, _DELETE_STALE_REASON)
            expected = git_blob_hash_bytes(current)
        if not target.delete(
            rel, expected, file_path=file_path, operation="delete_file"
        ):
            raise FileNotFoundError(f"File not found: {file_path}")
        return self._deleted_result(file_path)

    # ── shared by the local and seam paths ─────────────────────────────────

    def _verify_hash(
        self, current: bytes, content_hash: str, file_path: str, reason: str
    ) -> None:
        """Optimistic locking: HashMismatchError unless *current* is *content_hash*."""
        current_hash = self._compute_hash(current)
        if current_hash != content_hash:
            raise HashMismatchError(
                f"Content hash mismatch for '{file_path}'. "
                f"Expected {content_hash}, got {current_hash}. "
                f"{reason}"
            )

    def _apply_edit(
        self,
        current: bytes,
        old_string: str,
        new_string: str,
        replace_all: bool,
        file_path: str,
    ) -> Tuple[str, int, bool]:
        """(new_content, changes_made, has_bom) for an edit of *current*."""
        # Bug #570: Use utf-8-sig to strip BOM if present; track for write-back
        has_bom = current[:3] == _UTF8_BOM
        current_content_str = current.decode("utf-8-sig")
        new_content, changes_made = self._perform_replacement(
            current_content_str, old_string, new_string, replace_all, file_path
        )
        return new_content, changes_made, has_bom

    def _encode_for_write(self, full_path: str, content: str, has_bom: bool) -> bytes:
        """The bytes ``_atomic_write_file`` would put on disk for *content*."""
        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError as e:
            raise CRUDOperationError(
                f"Failed to write file '{full_path}': {str(e)}"
            ) from e
        return _UTF8_BOM + data if has_bom else data

    def _created_result(self, file_path: str, content: str) -> Dict[str, Any]:
        content_bytes = content.encode("utf-8")
        return {
            "success": True,
            "file_path": file_path,
            "content_hash": self._compute_hash(content_bytes),
            "size_bytes": len(content_bytes),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def _edited_result(
        self, file_path: str, new_content: str, changes_made: int
    ) -> Dict[str, Any]:
        return {
            "success": True,
            "file_path": file_path,
            "content_hash": self._compute_hash(new_content.encode("utf-8")),
            "modified_at": datetime.now(timezone.utc).isoformat(),
            "changes_made": changes_made,
        }

    def _deleted_result(self, file_path: str) -> Dict[str, Any]:
        return {
            "success": True,
            "file_path": file_path,
            "deleted_at": datetime.now(timezone.utc).isoformat(),
        }

    def _validate_crud_path(self, file_path: str, operation: str) -> None:
        """
        Validate file path for CRUD operations.

        Security checks:
        - Block .git/ directory access
        - Prevent path traversal (..)
        - Validate path structure

        Args:
            file_path: Relative file path to validate
            operation: Operation name for error messages

        Raises:
            PermissionError: If path fails security validation
        """
        # Block .git/ directory access (exact directory component match)
        # Use Path.parts to check for .git as a directory component
        # This allows .gitignore, .github/, etc. while blocking .git/
        path_parts = Path(file_path).parts
        if ".git" in path_parts:
            raise PermissionError(
                f"{operation} blocked: Access to .git/ directory is forbidden"
            )

        # Block path traversal (..)
        path_obj = Path(file_path)
        if ".." in path_obj.parts:
            raise PermissionError(
                f"{operation} blocked: Path traversal detected in '{file_path}'"
            )

        # Additional validation: prevent absolute paths
        if path_obj.is_absolute():
            raise PermissionError(
                f"{operation} blocked: Absolute paths not allowed, use relative paths"
            )

    def _compute_hash(self, content: bytes) -> str:
        """
        Compute SHA-256 hash of content.

        Args:
            content: Content as bytes

        Returns:
            Hex digest string of SHA-256 hash
        """
        return hashlib.sha256(content).hexdigest()

    def _atomic_write_file(
        self,
        full_path: Path,
        content: str,
        has_bom: bool = False,
        exclusive: bool = False,
    ) -> None:
        """
        Atomically write content to file using temp file + rename pattern.

        Args:
            full_path: Full path to target file
            content: Content to write as string
            has_bom: If True, write with UTF-8 BOM (Bug #570)
            exclusive: publish only if the target does not exist (the
                absence check and the creation are one operation)

        Raises:
            FileExistsError: *exclusive* and the target exists (untouched)
            CRUDOperationError: If write operation fails
        """
        try:
            # Write to temporary file in same directory (ensures same filesystem)
            temp_fd, temp_path = tempfile.mkstemp(
                dir=str(full_path.parent), prefix=".tmp_", suffix=f"_{full_path.name}"
            )

            try:
                # Write content to temp file
                # Bug #570: Use utf-8-sig to preserve BOM if original had one
                write_encoding = "utf-8-sig" if has_bom else "utf-8"
                with os.fdopen(temp_fd, "w", encoding=write_encoding) as temp_file:
                    temp_file.write(content)
                    temp_file.flush()
                    nfs_safe_fsync(temp_file.fileno())

                if exclusive:
                    nfs_safe_link_exclusive(temp_path, str(full_path))
                    try:
                        os.remove(temp_path)
                    except OSError as exc:
                        logger.warning(
                            "File %s is published but its temp name could "
                            "not be removed (%s)",
                            full_path.name,
                            type(exc).__name__,
                        )
                else:
                    # Atomic rename (POSIX guarantees atomicity)
                    os.replace(temp_path, str(full_path))

            except Exception:
                # Clean up temp file on failure
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise

        except FileExistsError:
            raise
        except Exception as e:
            raise CRUDOperationError(
                f"Failed to write file '{full_path}': {str(e)}"
            ) from e

    def _perform_replacement(
        self,
        content: str,
        old_string: str,
        new_string: str,
        replace_all: bool,
        file_path: str,
    ) -> tuple[str, int]:
        """
        Perform string replacement with validation.

        Args:
            content: Current file content
            old_string: String to replace
            new_string: Replacement string
            replace_all: If True, replace all; if False, require unique match
            file_path: File path for error messages

        Returns:
            Tuple of (new_content, changes_made)

        Raises:
            ValueError: If string not found or not unique when replace_all=False
        """
        # Bug #570: Normalize Unicode to NFC so visually identical strings
        # with different normalization forms (NFC vs NFD, emoji variants) match.
        import unicodedata

        content = unicodedata.normalize("NFC", content)
        old_string = unicodedata.normalize("NFC", old_string)
        new_string = unicodedata.normalize("NFC", new_string)

        if replace_all:
            # Replace all occurrences
            new_content = content.replace(old_string, new_string)
            changes_made = content.count(old_string)
        else:
            # Single replacement - must be unique
            occurrence_count = content.count(old_string)

            if occurrence_count == 0:
                raise ValueError(
                    f"String '{old_string}' not found in file '{file_path}'"
                )
            elif occurrence_count > 1:
                raise ValueError(
                    f"String '{old_string}' appears {occurrence_count} times in '{file_path}'. "
                    "Not unique - use replace_all=True to replace all occurrences."
                )

            # Exactly one occurrence - safe to replace
            new_content = content.replace(old_string, new_string, 1)
            changes_made = 1

        return new_content, changes_made


class _RepositoryWrites(Protocol):
    """One repository's file writes as :class:`FileCRUDService` performs them."""

    def create(self, file_path: str, content: str) -> Dict[str, Any]: ...

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        content_hash: str,
        replace_all: bool,
    ) -> Dict[str, Any]: ...

    def delete(self, file_path: str, content_hash: Optional[str]) -> Dict[str, Any]: ...


class _PodDiskWrites:
    """A repository whose content implementation is this pod's disk: today's
    writes (confinement that resolves links, BOM-preserving atomic writes)."""

    def __init__(self, service: FileCRUDService, repo_path: Path) -> None:
        self._service = service
        self._repo_path = repo_path

    def create(self, file_path: str, content: str) -> Dict[str, Any]:
        return self._service._create_on_pod_disk(self._repo_path, file_path, content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        content_hash: str,
        replace_all: bool,
    ) -> Dict[str, Any]:
        return self._service._edit_on_pod_disk(
            self._repo_path,
            file_path,
            old_string,
            new_string,
            content_hash,
            replace_all,
        )

    def delete(self, file_path: str, content_hash: Optional[str]) -> Dict[str, Any]:
        return self._service._delete_on_pod_disk(
            self._repo_path, file_path, content_hash
        )


class _WriterWrites:
    """A repository read through a content source and written by the writer
    that writes where it reads (``content_writer_for``)."""

    def __init__(
        self, service: FileCRUDService, target: "GoldenRepoWriteTarget"
    ) -> None:
        self._service = service
        self._target = target

    def create(self, file_path: str, content: str) -> Dict[str, Any]:
        return self._service._create_through_writer(self._target, file_path, content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        content_hash: str,
        replace_all: bool,
    ) -> Dict[str, Any]:
        return self._service._edit_through_writer(
            self._target, file_path, old_string, new_string, content_hash, replace_all
        )

    def delete(self, file_path: str, content_hash: Optional[str]) -> Dict[str, Any]:
        return self._service._delete_through_writer(
            self._target, file_path, content_hash
        )


# Global service instance
file_crud_service = FileCRUDService()
