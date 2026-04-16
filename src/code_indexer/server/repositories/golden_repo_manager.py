"""
Golden Repository Manager for CIDX Server.

Manages golden repositories that can be activated by users for semantic search.
Golden repositories are stored in ~/.cidx-server/data/golden-repos/ with metadata tracking.
"""

from code_indexer.server.middleware.correlation import get_correlation_id

import errno
import json
import os
import re
import shutil
import subprocess
import logging
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, TYPE_CHECKING, cast

if TYPE_CHECKING:
    from code_indexer.server.models.golden_repo_branch_models import (
        GoldenRepoBranchInfo,
    )
    from code_indexer.server.utils.config_manager import ServerResourceConfig
    from code_indexer.server.services.background_job_manager import BackgroundJobManager
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )
    from code_indexer.server.services.group_access_manager import GroupAccessManager

from pydantic import BaseModel
from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)


class GoldenRepoError(Exception):
    """Base exception for golden repository operations."""

    pass


class GitOperationError(GoldenRepoError):
    """Exception raised when git operations fail."""

    pass


def _gather_repo_metrics(repo_path) -> tuple:
    """
    Gather file count and commit count for a repository.

    Delegates to the shared gather_repo_metrics utility in
    code_indexer.services.progress_subprocess_runner.

    Args:
        repo_path: Path to the git repository (str or Path)

    Returns:
        (file_count, commit_count) as integers.  Returns (0, 0) if repo is
        not a git repository or if git commands fail (graceful degradation).
    """
    from code_indexer.services.progress_subprocess_runner import gather_repo_metrics

    return gather_repo_metrics(repo_path)  # type: ignore[no-any-return]


class GoldenRepoNotFoundError(GoldenRepoError):
    """Exception raised when golden repository cannot be found on filesystem."""

    pass


class GoldenRepo(BaseModel):
    """Model representing a golden repository."""

    alias: str
    repo_url: str
    default_branch: str
    clone_path: str
    created_at: str
    enable_temporal: bool = False
    temporal_options: Optional[Dict] = None
    category_id: Optional[int] = None
    category_auto_assigned: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert golden repository to dictionary."""
        return {
            "alias": self.alias,
            "repo_url": self.repo_url,
            "default_branch": self.default_branch,
            "clone_path": self.clone_path,
            "created_at": self.created_at,
            "enable_temporal": self.enable_temporal,
            "temporal_options": self.temporal_options,
            "category_id": self.category_id,
            "category_auto_assigned": self.category_auto_assigned,
        }


# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)


class GoldenRepoManager:
    """
    Manages golden repositories for the CIDX server.

    Golden repositories are admin-managed, globally unique namespaced repositories
    that support git operations with Copy-on-Write (CoW) cloning.
    """

    # Dynamically injected by app.py at runtime
    background_job_manager: "BackgroundJobManager"
    activated_repo_manager: "ActivatedRepoManager"
    group_access_manager: Optional["GroupAccessManager"] = None
    _repo_category_service: Optional[Any] = None  # RepoCategoryService (Story #181)

    def __init__(
        self,
        data_dir: str,
        resource_config: Optional["ServerResourceConfig"] = None,
        db_path: Optional[str] = None,
        storage_backend: Optional[Any] = None,
    ):
        """
        Initialize golden repository manager.

        Args:
            data_dir: Data directory path (REQUIRED - no default)
            resource_config: Resource configuration (timeouts, limits)
            db_path: Path to SQLite database file (optional, auto-computed from data_dir if not provided)

        Raises:
            ValueError: If data_dir is None or empty
        """
        if not data_dir or not data_dir.strip():
            raise ValueError("data_dir is required and cannot be None or empty")

        self.data_dir = data_dir
        self.golden_repos_dir = os.path.join(self.data_dir, "golden-repos")

        # Resource configuration (import here to avoid circular dependency)
        if resource_config is None:
            from code_indexer.server.utils.config_manager import ServerResourceConfig

            resource_config = ServerResourceConfig()
        self.resource_config = resource_config

        # Ensure directory structure exists
        os.makedirs(self.golden_repos_dir, exist_ok=True)

        # Thread safety for concurrent operations (Story #620 Priority 2A)
        self._operation_lock = threading.Lock()

        # Storage for golden repositories
        self.golden_repos: Dict[str, GoldenRepo] = {}

        # SQLite backend - always enabled (Bug #176: single source of truth)
        # Auto-compute db_path if not provided
        if db_path is None:
            db_path = os.path.join(self.data_dir, "cidx_server.db")

        # Expose db_path for wiki cache and other consumers
        self.db_path = db_path

        if storage_backend is not None:
            self._sqlite_backend: Any = storage_backend
            logger.info("GoldenRepoManager using injected storage backend")
        else:
            from code_indexer.server.storage.sqlite_backends import (
                GoldenRepoMetadataSqliteBackend,
            )

            self._sqlite_backend = GoldenRepoMetadataSqliteBackend(db_path)
        self._sqlite_backend.ensure_table_exists()
        self._load_metadata_from_sqlite()

        # One-time migration from metadata.json to SQLite (Bug #176)
        metadata_file = os.path.join(self.golden_repos_dir, "metadata.json")
        if os.path.exists(metadata_file):
            try:
                with open(metadata_file, "r") as f:
                    json_data = json.load(f)
                migrated = 0
                failed = 0
                for alias, repo_data in json_data.items():
                    try:
                        if alias not in self.golden_repos:
                            self._sqlite_backend.add_repo(
                                alias=repo_data["alias"],
                                repo_url=repo_data["repo_url"],
                                default_branch=repo_data["default_branch"],
                                clone_path=repo_data["clone_path"],
                                created_at=repo_data["created_at"],
                                enable_temporal=repo_data.get("enable_temporal", False),
                                temporal_options=repo_data.get("temporal_options"),
                            )
                            self.golden_repos[alias] = GoldenRepo(**repo_data)
                            migrated += 1
                    except (TypeError, KeyError, ValueError) as repo_error:
                        failed += 1
                        logging.warning(
                            f"Failed to migrate repo '{alias}' from metadata.json: {repo_error}. "
                            f"Continuing with remaining repos."
                        )

                if migrated > 0:
                    logging.warning(
                        f"Migrated {migrated} repos from metadata.json to SQLite. "
                        f"metadata.json is now deprecated and can be removed."
                    )
                if failed > 0:
                    logging.warning(
                        f"Failed to migrate {failed} repos from metadata.json. "
                        f"Check logs for details."
                    )

                # Rename metadata.json to metadata.json.migrated to prevent re-processing
                migrated_file = metadata_file + ".migrated"
                try:
                    os.rename(metadata_file, migrated_file)
                    logging.info(
                        f"Renamed metadata.json to {os.path.basename(migrated_file)}"
                    )
                except OSError as rename_error:
                    logging.warning(
                        f"Failed to rename metadata.json: {rename_error}. "
                        f"Migration succeeded but file will be re-processed on next startup."
                    )
            except (json.JSONDecodeError, TypeError, KeyError) as e:
                logging.warning(f"Could not migrate metadata.json: {e}")

    def _load_metadata_from_sqlite(self) -> None:
        """Load golden repository metadata from SQLite backend.

        Thread-safe: Uses _operation_lock to prevent concurrent access.
        """
        with self._operation_lock:
            repos = self._sqlite_backend.list_repos()
            for repo_data in repos:
                self.golden_repos[repo_data["alias"]] = GoldenRepo(**repo_data)
            logging.info(f"Loaded {len(self.golden_repos)} golden repos from SQLite")

    def add_golden_repo(
        self,
        repo_url: str,
        alias: str,
        default_branch: Optional[str] = None,
        description: Optional[str] = None,
        enable_temporal: bool = False,
        temporal_options: Optional[Dict] = None,
        submitter_username: str = "admin",
    ) -> str:
        """
        Add a golden repository.

        This method submits a background job and returns immediately with a job_id.
        Use BackgroundJobManager to track progress and results.

        Args:
            repo_url: Git repository URL
            alias: Unique alias for the repository
            default_branch: Branch to clone. When None (the default), git uses the
                            remote's HEAD ref so any default branch name works.
            description: Optional description for the repository
            enable_temporal: Enable temporal git history indexing
            temporal_options: Temporal indexing configuration options
            submitter_username: Username of the user submitting the job (default: "admin")

        Returns:
            Job ID for tracking add operation progress

        Raises:
            ValueError: If alias contains path traversal characters
            GoldenRepoError: If alias already exists
            GitOperationError: If git repository is invalid or inaccessible
            MaintenanceModeError: If server is in maintenance mode (Story #734)
        """
        # Check maintenance mode first (Story #734)
        from code_indexer.server.services.maintenance_service import (
            get_maintenance_state,
        )
        from code_indexer.server.jobs.exceptions import MaintenanceModeError

        if get_maintenance_state().is_maintenance_mode():
            raise MaintenanceModeError()

        # SECURITY: Validate alias BEFORE any operations (defense-in-depth)
        # Reject path traversal characters to prevent escaping golden repos directory
        if ".." in alias:
            raise ValueError(
                f"Invalid alias '{alias}': cannot contain path traversal characters (..)"
            )
        if "/" in alias:
            raise ValueError(
                f"Invalid alias '{alias}': cannot contain path traversal characters (/)"
            )
        if "\\" in alias:
            raise ValueError(
                f"Invalid alias '{alias}': cannot contain path traversal characters (\\)"
            )

        # Validate BEFORE submitting job
        if alias in self.golden_repos:
            raise GoldenRepoError(f"Golden repository alias '{alias}' already exists")

        # Skip git validation for local:// URLs (Story #538)
        if not repo_url.startswith("local://"):
            if not self._validate_git_repository(repo_url):
                raise GitOperationError(
                    f"Invalid or inaccessible git repository: {repo_url}"
                )

        # Create wrapper for background execution that accepts progress_callback
        # so BackgroundJobManager can inject it (Story #482 PATH A).
        def background_worker(progress_callback=None) -> Dict[str, Any]:
            """Execute add operation in background thread."""
            nonlocal default_branch
            try:
                # Clone repository
                clone_path = self._clone_repository(repo_url, alias, default_branch)

                # Bug #699: When no branch was specified, resolve the actual
                # checked-out branch so metadata always stores a concrete value
                # for future refreshes (GoldenRepo model requires a string).
                if default_branch is None:
                    default_branch = self._resolve_cloned_branch(clone_path)

                # Execute post-clone workflow
                self._execute_post_clone_workflow(
                    clone_path,
                    force_init=False,
                    enable_temporal=enable_temporal,
                    temporal_options=temporal_options,
                    progress_callback=progress_callback,
                )

                # Create golden repository record
                created_at = datetime.now(timezone.utc).isoformat()
                golden_repo = GoldenRepo(
                    alias=alias,
                    repo_url=repo_url,
                    default_branch=default_branch,
                    clone_path=clone_path,
                    created_at=created_at,
                    enable_temporal=enable_temporal,
                    temporal_options=temporal_options,
                )

                # Store and persist
                self.golden_repos[alias] = golden_repo
                self._sqlite_backend.add_repo(
                    alias=alias,
                    repo_url=repo_url,
                    default_branch=default_branch,
                    clone_path=clone_path,
                    created_at=created_at,
                    enable_temporal=enable_temporal,
                    temporal_options=temporal_options,
                )

                # Auto-assign category (Story #181 AC1, Story #622 URL matching)
                # Non-blocking: log error but don't fail registration
                if self._repo_category_service is not None:
                    try:
                        category_id = self._repo_category_service.auto_assign(
                            alias, repo_url=repo_url
                        )
                        if category_id is not None:
                            self._sqlite_backend.update_category(
                                alias, category_id, auto_assigned=True
                            )
                            logging.info(
                                f"Auto-assigned category {category_id} to '{alias}'"
                            )
                    except Exception as e:
                        logging.warning(
                            f"Category auto-assignment failed for '{alias}': {e}"
                        )

                # Automatic global activation (AC1 from Story #521)
                # This is a non-blocking post-registration step (AC4)
                try:
                    from code_indexer.global_repos.global_activation import (
                        GlobalActivator,
                    )

                    global_activator = GlobalActivator(self.golden_repos_dir)
                    global_activator.activate_golden_repo(
                        repo_name=alias,
                        repo_url=repo_url,
                        clone_path=clone_path,
                        enable_temporal=enable_temporal,
                        temporal_options=temporal_options,
                    )
                    logging.info(
                        f"Golden repository '{alias}' automatically activated globally as '{alias}-global'"
                    )
                except Exception as activation_error:
                    # Log error but don't fail the golden repo registration (AC4)
                    logging.error(
                        f"Global activation failed for '{alias}': {activation_error}. "
                        f"Golden repository is registered but not globally accessible. "
                        f"Manual global activation can be retried later."
                    )
                    # Continue with successful registration response

                # Lifecycle hook: Create .md file in cidx-meta (Story #538)
                try:
                    from code_indexer.global_repos.meta_description_hook import (
                        on_repo_added,
                    )

                    on_repo_added(
                        repo_name=alias,
                        repo_url=repo_url,
                        clone_path=clone_path,
                        golden_repos_dir=self.golden_repos_dir,
                    )
                except Exception as hook_error:
                    # Log error but don't fail the golden repo registration
                    logging.error(
                        f"Meta description hook failed for '{alias}': {hook_error}. "
                        f"Golden repository added but meta description not created."
                    )

                # Lifecycle hook: Auto-assign to admins/powerusers groups (Story #706)
                try:
                    if self.group_access_manager is not None:
                        from code_indexer.server.services.group_access_hooks import (
                            on_repo_added as group_access_on_repo_added,
                        )

                        group_access_on_repo_added(alias, self.group_access_manager)
                except Exception as hook_error:
                    # Log error but don't fail the golden repo registration
                    logging.error(
                        f"Group access hook failed for '{alias}': {hook_error}. "
                        f"Golden repository added but may not be accessible to expected groups."
                    )

                return {
                    "success": True,
                    "alias": alias,
                    "message": f"Golden repository '{alias}' added successfully",
                }

            except subprocess.CalledProcessError as e:
                raise GitOperationError(
                    f"Failed to clone repository: Git process failed with exit code {e.returncode}: {e.stderr}"
                )
            except subprocess.TimeoutExpired as e:
                raise GitOperationError(
                    f"Failed to clone repository: Git operation timed out after {e.timeout} seconds"
                )
            except (OSError, IOError) as e:
                raise GitOperationError(
                    f"Failed to clone repository: File system error: {str(e)}"
                )
            except GitOperationError:
                # Re-raise GitOperationError from sub-methods without modification
                raise

        # Submit to BackgroundJobManager
        job_id = self.background_job_manager.submit_job(
            operation_type="add_golden_repo",
            func=background_worker,
            submitter_username=submitter_username,
            is_admin=True,
            repo_alias=alias,  # AC5: Fix unknown repo bug
        )
        return cast(str, job_id)

    def list_golden_repos(self) -> List[Dict[str, str]]:
        """
        List all golden repositories from SQLite (source of truth).

        Returns:
            List of golden repository dictionaries
        """
        return self._sqlite_backend.list_repos()  # type: ignore[no-any-return]

    def register_local_repo(
        self,
        alias: str,
        folder_path: "Path",
        fire_lifecycle_hooks: bool = True,
    ) -> bool:
        """
        Register a local (non-git) directory as a golden repo. Synchronous, idempotent.

        This is the standard registration path for local folders (Langfuse traces,
        cidx-meta, etc.) that don't require git cloning. Unlike add_golden_repo(),
        this method is synchronous and doesn't use BackgroundJobManager.

        Args:
            alias: Unique alias for the repository
            folder_path: Path to the local directory
            fire_lifecycle_hooks: Whether to fire on_repo_added and group_access hooks

        Returns:
            True if newly registered, False if already existed (idempotent)

        Raises:
            ValueError: If alias contains path traversal characters
        """
        # SECURITY: Validate alias BEFORE any operations (defense-in-depth)
        if ".." in alias:
            raise ValueError(
                f"Invalid alias '{alias}': cannot contain path traversal characters (..)"
            )
        if "/" in alias:
            raise ValueError(
                f"Invalid alias '{alias}': cannot contain path traversal characters (/)"
            )
        if "\\" in alias:
            raise ValueError(
                f"Invalid alias '{alias}': cannot contain path traversal characters (\\)"
            )

        with self._operation_lock:
            # Idempotency: return False if already registered
            if alias in self.golden_repos:
                return False

            # Create golden repository record
            created_at = datetime.now(timezone.utc).isoformat()
            golden_repo = GoldenRepo(
                alias=alias,
                repo_url=f"local://{alias}",
                default_branch="main",
                clone_path=str(folder_path),
                created_at=created_at,
                enable_temporal=False,
                temporal_options=None,
            )

            # Store in in-memory dict
            self.golden_repos[alias] = golden_repo

            # Persist to storage backend (SQLite - doesn't acquire lock)
            self._sqlite_backend.add_repo(
                alias=alias,
                repo_url=f"local://{alias}",
                default_branch="main",
                clone_path=str(folder_path),
                created_at=created_at,
                enable_temporal=False,
                temporal_options=None,
            )

            # Auto-assign category (Story #181 AC1, Story #622 URL matching)
            # Non-blocking: log error but don't fail registration
            if self._repo_category_service is not None:
                try:
                    local_repo_url = f"local://{alias}"
                    category_id = self._repo_category_service.auto_assign(
                        alias, repo_url=local_repo_url
                    )
                    if category_id is not None:
                        self._sqlite_backend.update_category(
                            alias, category_id, auto_assigned=True
                        )
                        logging.info(
                            f"Auto-assigned category {category_id} to '{alias}'"
                        )
                except Exception as e:
                    logging.warning(
                        f"Category auto-assignment failed for '{alias}': {e}"
                    )

        # Global activation (non-blocking - logs error but doesn't fail)
        try:
            from code_indexer.global_repos.global_activation import GlobalActivator

            global_activator = GlobalActivator(self.golden_repos_dir)
            global_activator.activate_golden_repo(
                repo_name=alias,
                repo_url=f"local://{alias}",
                clone_path=str(folder_path),
                enable_temporal=False,
                temporal_options=None,
            )
            logging.info(f"Local repo '{alias}' activated globally as '{alias}-global'")
        except Exception as activation_error:
            logging.error(
                f"Global activation failed for local repo '{alias}': {activation_error}. "
                f"Repository registered but not globally accessible."
            )

        # Initialize CIDX index structure for local repos (idempotent)
        if not (folder_path / ".code-indexer").exists():
            try:
                import subprocess

                subprocess.run(
                    ["cidx", "init"],
                    cwd=str(folder_path),
                    check=True,
                    capture_output=True,
                    text=True,
                )
                logging.info(
                    f"Initialized CIDX index structure for local repo '{alias}'"
                )
            except subprocess.CalledProcessError as e:
                logging.error(
                    f"Failed to initialize CIDX for '{alias}': {e.stderr if e.stderr else str(e)}"
                )
                # Continue with registration even if init fails
            except Exception as e:
                logging.error(f"Unexpected error during CIDX init for '{alias}': {e}")
                # Continue with registration even if init fails

        # Lifecycle hooks (controlled by fire_lifecycle_hooks parameter)
        if fire_lifecycle_hooks:
            try:
                from code_indexer.global_repos.meta_description_hook import (
                    on_repo_added,
                )

                on_repo_added(
                    repo_name=alias,
                    repo_url=f"local://{alias}",
                    clone_path=str(folder_path),
                    golden_repos_dir=self.golden_repos_dir,
                )
            except Exception as hook_error:
                logging.error(
                    f"Meta description hook failed for '{alias}': {hook_error}"
                )

            try:
                if self.group_access_manager is not None:
                    from code_indexer.server.services.group_access_hooks import (
                        on_repo_added as group_access_on_repo_added,
                    )

                    group_access_on_repo_added(alias, self.group_access_manager)
            except Exception as hook_error:
                logging.error(f"Group access hook failed for '{alias}': {hook_error}")

        return True

    def remove_golden_repo(self, alias: str, submitter_username: str = "admin") -> str:
        """
        Remove a golden repository.

        This method submits a background job and returns immediately with a job_id.
        Use BackgroundJobManager to track progress and results.

        Args:
            alias: Alias of the repository to remove
            submitter_username: Username of the user submitting the job (default: "admin")

        Returns:
            Job ID for tracking removal progress

        Raises:
            GoldenRepoError: If repository not found
            MaintenanceModeError: If server is in maintenance mode (Story #734)
        """
        # Check maintenance mode first (Story #734)
        from code_indexer.server.services.maintenance_service import (
            get_maintenance_state,
        )
        from code_indexer.server.jobs.exceptions import MaintenanceModeError

        if get_maintenance_state().is_maintenance_mode():
            raise MaintenanceModeError()

        # Validate repository exists BEFORE submitting job
        if alias not in self.golden_repos:
            raise GoldenRepoError(f"Golden repository '{alias}' not found")

        # Create no-args wrapper for background execution
        def background_worker() -> Dict[str, Any]:
            """Execute removal in background thread."""
            # Initialize cascade results tracking
            cascade_results: Dict[str, Any] = {
                "activated_repos_deleted": [],
                "activated_repos_failed": [],
                "global_alias_deleted": False,
                "golden_repo_deleted": False,
            }

            # Step 1: Cascade delete all activated repos
            if hasattr(self, "activated_repo_manager") and self.activated_repo_manager:
                try:
                    activated_repos = (
                        self.activated_repo_manager.find_repos_by_golden_alias(alias)
                    )

                    for repo_info in activated_repos:
                        username = repo_info["username"]
                        user_alias = repo_info["user_alias"]

                        try:
                            # Direct deletion (not background job) for cascade
                            self.activated_repo_manager._do_deactivate_repository(
                                username=username,
                                user_alias=user_alias,
                            )
                            cascade_results["activated_repos_deleted"].append(
                                f"{username}/{user_alias}"
                            )
                            logging.info(
                                f"Cascade deleted activated repo: {username}/{user_alias}"
                            )
                        except Exception as e:
                            cascade_results["activated_repos_failed"].append(
                                {
                                    "repo": f"{username}/{user_alias}",
                                    "error": str(e),
                                }
                            )
                            logging.error(
                                f"Failed to cascade delete {username}/{user_alias}: {e}"
                            )

                except Exception as e:
                    logging.error(
                        f"Failed to find activated repos for cascade deletion: {e}"
                    )

            # Get repository info before removal
            golden_repo = self.golden_repos[alias]

            # Perform cleanup BEFORE removing from memory
            # Use canonical path resolution to handle versioned structure repos
            try:
                actual_path = self.get_actual_repo_path(alias)
                cleanup_successful = self._cleanup_repository_files(actual_path)
            except GoldenRepoNotFoundError:
                # If repository doesn't exist on filesystem, nothing to clean up
                # This can happen in test scenarios or if repo was manually deleted
                logging.info(
                    f"Repository '{alias}' not found on filesystem during removal - skipping cleanup"
                )
                cleanup_successful = True
            except GitOperationError as cleanup_error:
                # Critical cleanup failures should prevent deletion
                logging.error(
                    f"Critical cleanup failure prevents repository deletion: {cleanup_error}"
                )
                raise  # Re-raise to prevent deletion

            # Only remove from storage after cleanup is complete
            del self.golden_repos[alias]

            try:
                self._sqlite_backend.remove_repo(alias)
            except Exception as save_error:
                # If SQLite delete fails, rollback the in-memory deletion
                logging.error(
                    f"Failed to remove from SQLite after deletion, rolling back: {save_error}"
                )
                self.golden_repos[alias] = golden_repo  # Restore repository
                raise GitOperationError(
                    f"Repository deletion rollback due to SQLite removal failure: {save_error}"
                )

            # ANTI-FALLBACK RULE: Fail operation when cleanup is incomplete
            # Per MESSI Rule 2: "Graceful failure over forced success"
            # Don't report "success with warnings" - either succeed or fail clearly
            if cleanup_successful:
                # Deactivate global activation (Story #532)
                # Remove GlobalRegistry entry, alias pointer file, meta-directory .md file
                try:
                    from code_indexer.global_repos.global_activation import (
                        GlobalActivator,
                    )

                    global_activator = GlobalActivator(self.golden_repos_dir)
                    global_activator.deactivate_golden_repo(alias)
                    logging.info(f"Golden repository '{alias}' deactivated globally")
                    cascade_results["global_alias_deleted"] = True
                except Exception as deactivation_error:
                    # Log error but don't fail removal - the repo files are already deleted
                    # This is consistent with add_golden_repo() behavior (AC4)
                    logging.error(
                        f"Global deactivation failed for '{alias}': {deactivation_error}. "
                        f"Golden repository removed but some global resources may remain."
                    )

                # Lifecycle hook: Delete .md file from cidx-meta (Story #538)
                try:
                    from code_indexer.global_repos.meta_description_hook import (
                        on_repo_removed,
                    )

                    on_repo_removed(
                        repo_name=alias, golden_repos_dir=self.golden_repos_dir
                    )
                except Exception as hook_error:
                    # Log error but don't fail removal - the repo is already removed
                    logging.error(
                        f"Meta description hook failed for '{alias}': {hook_error}. "
                        f"Golden repository removed but meta description not deleted."
                    )

                # Lifecycle hook: Revoke group access (Story #706)
                try:
                    if self.group_access_manager is not None:
                        from code_indexer.server.services.group_access_hooks import (
                            on_repo_removed as group_access_on_repo_removed,
                        )

                        group_access_on_repo_removed(alias, self.group_access_manager)
                except Exception as hook_error:
                    # Log error but don't fail removal - the repo is already removed
                    logging.error(
                        f"Group access hook failed for '{alias}': {hook_error}. "
                        f"Golden repository removed but access records may remain."
                    )

                # Lifecycle hook: Delete wiki article view records (Story #287, AC4)
                try:
                    from code_indexer.server.wiki.wiki_cache import WikiCache

                    wiki_cache = WikiCache(self.db_path)
                    wiki_cache.delete_views_for_repo(alias)
                except Exception as hook_error:
                    logging.error(
                        f"Wiki view cleanup hook failed for '{alias}': {hook_error}. "
                        f"Golden repository removed but view records may remain."
                    )

                # Mark golden repo as deleted
                cascade_results["golden_repo_deleted"] = True

                # Build enhanced message with cascade deletion counts
                activated_count = len(cascade_results["activated_repos_deleted"])
                failed_count = len(cascade_results["activated_repos_failed"])

                message = f"Golden repository '{alias}' removed successfully"
                if activated_count > 0:
                    message += f" (cascade deleted {activated_count} activated repos)"
                if failed_count > 0:
                    message += (
                        f" (WARNING: {failed_count} activated repos failed to delete)"
                    )

                return {
                    "success": True,
                    "alias": alias,
                    "message": message,
                    "cascade_results": cascade_results,
                }
            else:
                # FAIL the operation - don't mask cleanup failures
                raise GitOperationError(
                    "Repository metadata removed but cleanup incomplete. "
                    "Resource leak detected: some cleanup operations did not complete fully."
                )

        # Submit to BackgroundJobManager
        job_id = self.background_job_manager.submit_job(
            operation_type="remove_golden_repo",
            func=background_worker,
            submitter_username=submitter_username,
            is_admin=True,
            repo_alias=alias,  # AC5: Fix unknown repo bug
        )
        return cast(str, job_id)

    def _validate_git_repository(self, repo_url: str) -> bool:
        """
        Validate that a git repository URL is accessible.

        Args:
            repo_url: Git repository URL to validate

        Returns:
            True if repository is valid and accessible, False otherwise
        """
        try:
            # Use git ls-remote to check if repository is accessible
            result = subprocess.run(
                ["git", "ls-remote", repo_url],
                capture_output=True,
                text=True,
                timeout=self.resource_config.git_clone_timeout,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, subprocess.SubprocessError):
            return False

    def _clone_repository(self, repo_url: str, alias: str, branch: str) -> str:
        """
        Clone a git repository to the golden repos directory.

        Golden repository registration should always use regular copying/cloning,
        NOT Copy-on-Write (CoW) cloning, as it may involve cross-device operations.

        Args:
            repo_url: Git repository URL
            alias: Repository alias for directory name
            branch: Branch to clone

        Returns:
            Path to cloned repository

        Raises:
            GitOperationError: If cloning fails
        """
        clone_path = os.path.join(self.golden_repos_dir, alias)

        # For local repositories, use regular copying (NO CoW for golden repo registration)
        if self._is_local_path(repo_url):
            return self._clone_local_repository_with_regular_copy(repo_url, clone_path)

        # For remote repositories, use regular git clone
        return self._clone_remote_repository(repo_url, clone_path, branch)

    def _is_local_path(self, repo_url: str) -> bool:
        """
        Check if the repository URL is a local filesystem path.

        Args:
            repo_url: Repository URL to check

        Returns:
            True if it's a local path, False if remote
        """
        return (
            repo_url.startswith("/")
            or repo_url.startswith("file://")
            or repo_url.startswith("local://")
        )

    def _clone_local_repository_with_regular_copy(
        self, repo_url: str, clone_path: str
    ) -> str:
        """
        Clone a local repository using regular copying (NO CoW).

        This method is used for golden repository registration to avoid
        cross-device link issues when copying from arbitrary local paths
        (like /tmp) to the golden repository storage directory.

        Args:
            repo_url: Local repository path
            clone_path: Destination path

        Returns:
            Path to cloned repository

        Raises:
            GitOperationError: If cloning fails
        """
        # Normalize file:// and local:// URLs
        if repo_url.startswith("file://"):
            source_path = repo_url.replace("file://", "")
        elif repo_url.startswith("local://"):
            # local://cidx-meta -> Use the target directory directly (no source to copy)
            # The directory should already exist (created by bootstrap_cidx_meta)
            source_path = None
        else:
            source_path = repo_url

        try:
            # For local:// URLs, use or create the target directory (no copy needed)
            if source_path is None:
                if not os.path.exists(clone_path):
                    # AC4 Story #163: Auto-create folder for local:// URLs
                    os.makedirs(clone_path, exist_ok=True)
                    logging.info(
                        f"Created local repository directory for {repo_url}: {clone_path}"
                    )
                else:
                    logging.info(
                        f"Using existing local directory for {repo_url}: {clone_path}"
                    )
                return clone_path

            # Always use regular copy for golden repository registration
            # This avoids cross-device link issues that occur with CoW cloning
            shutil.copytree(source_path, clone_path, symlinks=True)
            logging.info(
                f"Golden repository registered using regular copy: {source_path} -> {clone_path}"
            )
            return clone_path
        except FileNotFoundError as e:
            raise GitOperationError(
                f"Failed to copy local repository: Source directory not found: {str(e)}"
            )
        except PermissionError as e:
            raise GitOperationError(
                f"Failed to copy local repository: Permission denied: {str(e)}"
            )
        except OSError as e:
            raise GitOperationError(
                f"Failed to copy local repository: File system error: {str(e)}"
            )
        except shutil.Error as e:
            raise GitOperationError(
                f"Failed to copy local repository: Copy operation failed: {str(e)}"
            )

    def _clone_remote_repository(
        self, repo_url: str, clone_path: str, branch: Optional[str] = None
    ) -> str:
        """
        Clone a remote git repository using git clone.

        Args:
            repo_url: Remote git repository URL
            clone_path: Destination path
            branch: Branch to clone. When None, git uses the remote's HEAD ref
                    (its natural default), so no --branch flag is passed.

        Returns:
            Path to cloned repository

        Raises:
            GitOperationError: If cloning fails
        """
        try:
            # Clone full repository with complete history for semantic search.
            # Only pass --branch when explicitly requested; omitting it lets git
            # resolve the remote HEAD, which works for any default branch name.
            cmd = ["git", "clone"]
            if branch is not None:
                cmd.extend(["--branch", branch])
            cmd.extend([repo_url, clone_path])

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.resource_config.git_pull_timeout,
            )

            if result.returncode != 0:
                raise GitOperationError(
                    f"Git clone failed with code {result.returncode}: {result.stderr}"
                )

            return clone_path

        except subprocess.TimeoutExpired:
            raise GitOperationError("Git clone operation timed out")
        except subprocess.SubprocessError as e:
            raise GitOperationError(f"Git clone subprocess error: {str(e)}")

    def _resolve_cloned_branch(self, clone_path: str) -> str:
        """Detect the checked-out branch in a freshly cloned repository.

        Used after a clone with no explicit --branch to discover which branch
        the remote's HEAD pointed to (e.g., 'master', 'main', 'develop').

        Returns:
            Branch name string (never None). Falls back to 'main' with a
            logged warning if detection fails, which is strictly better than
            the old behavior that always hardcoded 'main' regardless.
        """
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=clone_path,
                capture_output=True,
                text=True,
                check=True,
                timeout=self.resource_config.git_local_timeout,
            )
            branch = result.stdout.strip()
            if not branch:
                logger.warning(
                    "git rev-parse returned empty output for cloned repo at %s. "
                    "Falling back to 'main'.",
                    clone_path,
                )
                return "main"
            return branch
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.warning(
                "Failed to detect default branch in cloned repo at %s: %s. "
                "Falling back to 'main'.",
                clone_path,
                e,
            )
            return "main"

    def _cleanup_repository_files(self, clone_path: str) -> bool:
        """
        Clean up repository files and directories using orchestrated cleanup.

        Uses the same approach as 'cidx uninstall' to properly handle root-owned files
        created by previous infrastructure (removed in v8.0).

        Args:
            clone_path: Path to repository directory to clean up

        Returns:
            bool: True if cleanup was successful, False if there were issues
                  (but the repository deletion can still be considered successful)

        Raises:
            GitOperationError: Only for critical failures that should prevent deletion
        """
        if not os.path.exists(clone_path):
            return True  # Already cleaned up

        try:
            from pathlib import Path

            clone_path_obj = Path(clone_path)
            project_config_dir = clone_path_obj / ".code-indexer"
            overall_cleanup_successful = True

            # Phase 1: Docker cleanup if project has cidx services
            if project_config_dir.exists():
                docker_success = self._perform_docker_cleanup(
                    clone_path, project_config_dir
                )
                if not docker_success:
                    overall_cleanup_successful = False

            # Phase 2: Final filesystem cleanup
            filesystem_success = self._cleanup_filesystem(clone_path_obj)
            if filesystem_success is None:
                return overall_cleanup_successful  # Directory already removed
            return filesystem_success and overall_cleanup_successful

        except (
            ImportError,
            PermissionError,
            OSError,
            subprocess.CalledProcessError,
            RuntimeError,
        ) as e:
            return self._handle_cleanup_errors(e, clone_path)

    def _perform_docker_cleanup(
        self, clone_path: str, project_config_dir: Path
    ) -> bool:
        """
        Docker cleanup is no longer performed (Story #506: container management deprecated).

        Container-based backends are deprecated. Repositories should use filesystem backend.
        This method now returns True to allow cleanup to proceed.

        Args:
            clone_path: Path to repository directory
            project_config_dir: Path to .code-indexer config directory

        Returns:
            bool: Always returns True (no-op)

        Raises:
            GitOperationError: Not raised (legacy compatibility)
        """
        logging.info(
            f"Docker cleanup skipped for {clone_path} (container management deprecated)"
        )
        return True

    def _cleanup_filesystem(self, clone_path_obj: Path) -> Optional[bool]:
        """
        Clean up remaining filesystem structure after Docker cleanup.

        Args:
            clone_path_obj: Path object for repository directory

        Returns:
            bool: True if successful, False if issues occurred
            None: If directory already removed
        """
        if not clone_path_obj.exists():
            return None  # Directory already removed

        try:
            shutil.rmtree(str(clone_path_obj))
            logging.info(f"Successfully cleaned up repository files: {clone_path_obj}")
            return True
        except (PermissionError, OSError) as fs_error:
            # File system cleanup failed - log but don't prevent deletion
            logging.warning(
                f"File system cleanup incomplete for {clone_path_obj}: "
                f"{type(fs_error).__name__}: {fs_error}. "
                "Some files may remain but repository deletion was successful."
            )
            return False

    def _handle_cleanup_errors(self, error: Exception, clone_path: str) -> bool:
        """
        Handle specific cleanup errors with appropriate logging and error translation.

        Args:
            error: The exception that occurred during cleanup
            clone_path: Path to repository being cleaned

        Returns:
            bool: For non-critical errors that allow deletion to proceed

        Raises:
            GitOperationError: For critical failures that should prevent deletion
        """
        if isinstance(error, ImportError):
            # Import errors during cleanup are no longer critical (container management deprecated)
            logging.warning(
                f"Import error during cleanup of {clone_path} (non-critical): {error}"
            )
            return True  # Non-critical, allow cleanup to proceed
        elif isinstance(error, PermissionError):
            logging.error(
                f"Permission denied during cleanup of {clone_path}: "
                f"Insufficient access to: {error.filename or 'unknown file'}"
            )
            raise GitOperationError(
                f"Insufficient permissions for cleanup: {str(error)}"
            )
        elif isinstance(error, OSError):
            if error.errno == errno.ENOENT:  # File not found
                return True  # Already cleaned
            elif error.errno == errno.EACCES:  # Permission denied
                logging.error(
                    f"Access denied during cleanup of {clone_path}: "
                    f"Cannot access: {error.filename or 'unknown file'}"
                )
                raise GitOperationError(f"Access denied for cleanup: {str(error)}")
            else:
                logging.error(
                    f"OS error during cleanup of {clone_path}: "
                    f"Error {error.errno}: {error}"
                )
                raise GitOperationError(
                    f"File system error during cleanup: {str(error)}"
                )
        elif isinstance(error, subprocess.CalledProcessError):
            if error.returncode == 126:  # Permission denied
                logging.error(
                    f"Command permission error during cleanup of {clone_path}: "
                    f"Command: {' '.join(error.cmd) if error.cmd else 'unknown'}"
                )
                raise GitOperationError(f"Command permission denied: {str(error)}")
            else:
                logging.error(
                    f"Process error during cleanup of {clone_path}: "
                    f"Command failed with exit code {error.returncode}"
                )
                raise GitOperationError(
                    f"Process failed with exit code {error.returncode}: {str(error)}"
                )
        elif isinstance(error, RuntimeError):
            # Critical system errors that prevent cleanup
            logging.error(
                f"Critical system error during cleanup of {clone_path}: {error}"
            )
            raise GitOperationError(f"Critical cleanup failure: {str(error)}")
        else:
            # Shouldn't reach here, but handle unexpected errors
            logging.error(
                f"Unexpected error during cleanup of {clone_path}: "
                f"{type(error).__name__}: {error}"
            )
            raise GitOperationError(f"Unexpected cleanup failure: {str(error)}")

    def _execute_post_clone_workflow(
        self,
        clone_path: str,
        force_init: bool = False,
        enable_temporal: bool = False,
        temporal_options: Optional[Dict] = None,
        progress_callback=None,
    ) -> None:
        """
        Execute the required workflow after successful repository cloning.

        The workflow includes:
        1. cidx init with voyage-ai embedding provider (with optional --force for refresh)
        2. cidx index (with optional temporal indexing parameters)

        Note: FilesystemVectorStore is container-free, so no start/stop/status commands needed.

        Args:
            clone_path: Path to the cloned repository
            force_init: Whether to use --force flag with cidx init (for refresh operations)
            enable_temporal: Whether to enable temporal indexing (git history)
            temporal_options: Optional temporal indexing parameters (time_range, include/exclude paths, diff_context)

        Raises:
            GitOperationError: If any workflow step fails
        """
        logging.info(
            f"Executing post-clone workflow for {clone_path} (force_init={force_init})"
        )

        # Build init command with optional --force flag
        init_command = ["cidx", "init", "--embedding-provider", "voyage-ai"]
        if force_init:
            init_command.append("--force")

        # Skip cidx init if config already exists and force_init=False (incremental refresh)
        config_path = Path(clone_path) / ".code-indexer" / "config.json"
        skip_init = not force_init and config_path.exists()
        if skip_init:
            logging.info(
                f"Skipping cidx init for {clone_path}: config exists and force_init=False (incremental refresh)"
            )

        # If temporal indexing is enabled, build temporal command
        temporal_command: Optional[List[str]] = None
        if enable_temporal:
            temporal_command = ["cidx", "index", "--index-commits", "--progress-json"]

            if temporal_options:
                if temporal_options.get("max_commits"):
                    temporal_command.extend(
                        ["--max-commits", str(temporal_options["max_commits"])]
                    )

                if temporal_options.get("since_date"):
                    temporal_command.extend(
                        ["--since-date", temporal_options["since_date"]]
                    )

                # Add diff-context parameter (default: 5 from model)
                diff_context = temporal_options.get("diff_context", 5)
                temporal_command.extend(["--diff-context", str(diff_context)])

                # Log warning for large context values
                if diff_context > 20:
                    logging.warning(
                        f"Large diff context ({diff_context} lines) will significantly "
                        f"increase storage. Recommended range: 3-10 lines."
                    )

        # Story #482 PATH A: Build ProgressPhaseAllocator for phase-aware progress reporting.
        # Phases: "semantic" (covers cidx index --fts), "temporal" (if enabled).
        # cidx init is fast (coarse markers only); no separate phase needed.
        from code_indexer.services.progress_phase_allocator import (
            ProgressPhaseAllocator,
        )
        from code_indexer.services.progress_subprocess_runner import (
            gather_repo_metrics,
            run_with_popen_progress,
            IndexingSubprocessError,
        )

        _phase_index_types = ["semantic"]
        if enable_temporal:
            _phase_index_types.append("temporal")

        file_count, commit_count = gather_repo_metrics(clone_path)
        _opts = temporal_options or {}
        max_commits_opt = _opts.get("max_commits") if temporal_options else None

        allocator = ProgressPhaseAllocator()
        allocator.calculate_weights(
            index_types=_phase_index_types,
            file_count=file_count,
            commit_count=commit_count,
            max_commits=max_commits_opt,
        )

        # Helper for Popen-based indexing with progress forwarding
        _popen_stdout: List[str] = []
        _popen_stderr: List[str] = []

        def _run_popen(command: List[str], phase_name: str, error_label: str) -> None:
            """Run command with Popen progress, re-raising as GitOperationError on failure."""
            _popen_stdout.clear()
            _popen_stderr.clear()
            try:
                run_with_popen_progress(
                    command=command,
                    phase_name=phase_name,
                    allocator=allocator,
                    progress_callback=progress_callback,
                    all_stdout=_popen_stdout,
                    all_stderr=_popen_stderr,
                    cwd=clone_path,
                    error_label=error_label,
                )
            except IndexingSubprocessError as e:
                # Check for "No files found" — acceptable for golden repo registration
                combined = "".join(_popen_stdout) + "".join(_popen_stderr)
                if "No files found to index" in combined:
                    logging.warning(
                        "PATH A: Repository has no indexable files — acceptable for golden repo registration"
                    )
                    return
                raise GitOperationError(str(e)) from e

        try:
            # Step 1: cidx init (fast — coarse markers only)
            if not skip_init:
                logging.info(f"Executing cidx init for {clone_path}")
                if progress_callback is not None:
                    progress_callback(
                        0,
                        phase="init",
                        detail="init: initializing repository...",
                    )

                result = subprocess.run(
                    init_command,
                    cwd=clone_path,
                    capture_output=True,
                    text=True,
                )

                if result.returncode != 0:
                    combined_output = result.stdout + result.stderr
                    if self._is_recoverable_init_error(combined_output):
                        logging.warning(
                            f"Recoverable configuration conflict during init. "
                            f"Attempting to resolve: {combined_output}"
                        )
                        if not self._attempt_init_conflict_resolution(
                            clone_path, force_init
                        ):
                            raise GitOperationError(f"Init failed: {combined_output}")
                    else:
                        raise GitOperationError(f"cidx init failed: {combined_output}")

                logging.info(f"cidx init completed for {clone_path}")
                if progress_callback is not None:
                    progress_callback(
                        5,
                        phase="init",
                        detail="init: complete",
                    )

                # Story #223 AC6: Seed repo with server-configured file extensions after cidx init
                try:
                    from ..services.config_service import get_config_service

                    config_svc = get_config_service()
                    config_svc.seed_repo_extensions_from_server_config(clone_path)
                    logging.info(
                        "Seeded file_extensions from server config for %s",
                        clone_path,
                    )
                except Exception as e:
                    logging.warning(
                        "Could not seed extensions for %s: %s", clone_path, e
                    )

                # Story #620: Write embedding_providers list so cidx index loops all providers.
                self._write_embedding_providers_to_config(clone_path)

            # Bug #678: Wrapper that seeds config before and drains health events after
            # each cidx index subprocess. Fire-and-forget: telemetry failures are logged
            # at DEBUG and never interrupt indexing.
            def _run_popen_with_telemetry(
                command: List[str], phase_name: str, error_label: str
            ) -> None:
                try:
                    from code_indexer.server.services.config_seeding import (
                        seed_provider_config,
                    )

                    seed_provider_config(clone_path)
                except Exception as _seed_exc:  # noqa: BLE001
                    logging.debug(
                        "Bug #678: seed_provider_config failed (non-fatal): %s",
                        _seed_exc,
                    )
                try:
                    _run_popen(command, phase_name=phase_name, error_label=error_label)
                finally:
                    try:
                        from code_indexer.services.provider_health_bridge import (
                            drain_and_feed_monitor,
                        )

                        drain_and_feed_monitor(clone_path)
                    except Exception as _drain_exc:  # noqa: BLE001
                        logging.debug(
                            "Bug #678: drain_and_feed_monitor failed (non-fatal): %s",
                            _drain_exc,
                        )

            # Step 2: cidx index --fts --progress-json (semantic + FTS, Popen for real progress)
            logging.info(f"Executing cidx index --fts for {clone_path}")
            _run_popen_with_telemetry(
                ["cidx", "index", "--fts", "--progress-json"],
                phase_name="semantic",
                error_label="semantic+FTS indexing",
            )
            logging.info(f"cidx index --fts completed for {clone_path}")

            # Step 3: cidx index --index-commits --progress-json (temporal, if enabled)
            if temporal_command is not None:
                logging.info(f"Executing cidx index --index-commits for {clone_path}")
                _run_popen_with_telemetry(
                    temporal_command,
                    phase_name="temporal",
                    error_label="temporal indexing",
                )
                logging.info(f"cidx index --index-commits completed for {clone_path}")

            logging.info(f"Post-clone workflow completed successfully for {clone_path}")

        except GitOperationError:
            raise
        except subprocess.TimeoutExpired:
            raise GitOperationError("Post-clone workflow timed out")
        except subprocess.CalledProcessError as e:
            raise GitOperationError(
                f"Post-clone workflow failed: Command '{' '.join(e.cmd)}' failed with exit code {e.returncode}"
            )
        except FileNotFoundError as e:
            raise GitOperationError(
                f"Post-clone workflow failed: Required command not found: {str(e)}"
            )
        except PermissionError as e:
            raise GitOperationError(
                f"Post-clone workflow failed: Permission denied: {str(e)}"
            )
        except OSError as e:
            raise GitOperationError(
                f"Post-clone workflow failed: System error: {str(e)}"
            )

    def _write_embedding_providers_to_config(self, clone_path: str) -> None:
        """Write the embedding_providers list to .code-indexer/config.json (Story #620).

        Uses EmbeddingProviderFactory.get_configured_providers() to determine which
        providers have valid API keys (checks env vars, CLI config, and server DB config).
        Preserves all existing keys in config.json.
        """
        config_path = Path(clone_path) / ".code-indexer" / "config.json"
        if not config_path.exists():
            logging.warning(
                "_write_embedding_providers_to_config: config.json not found at %s",
                config_path,
            )
            return

        try:
            from code_indexer.services.embedding_factory import EmbeddingProviderFactory
            from code_indexer.config import Config

            providers = EmbeddingProviderFactory.get_configured_providers(Config())
            if "voyage-ai" not in providers:
                providers.insert(0, "voyage-ai")

            with open(config_path) as f:
                config_data = json.load(f)
            config_data["embedding_providers"] = providers
            with open(config_path, "w") as f:
                json.dump(config_data, f)

            logging.info("Wrote embedding_providers=%s to %s", providers, config_path)
        except Exception as exc:
            logging.warning(
                "Could not write embedding_providers to %s: %s", config_path, exc
            )

    def _is_recoverable_init_error(self, error_output: str) -> bool:
        """
        Check if an init command error is recoverable.

        Args:
            error_output: Combined stdout/stderr from failed init command

        Returns:
            bool: True if error appears recoverable
        """
        error_lower = error_output.lower()
        recoverable_patterns = [
            "configuration conflict",
            "already initialized",
            "config file exists",
            "already in use",
            "service already running",
        ]
        return any(pattern in error_lower for pattern in recoverable_patterns)

    def _is_recoverable_service_error(self, error_output: str) -> bool:
        """
        Check if a service start command error is recoverable.

        Args:
            error_output: Combined stdout/stderr from failed start command

        Returns:
            bool: True if error appears recoverable
        """
        error_lower = error_output.lower()
        recoverable_patterns = [
            "already in use",
            "service already running",
            "container already exists",
            "already exists",
        ]
        return any(pattern in error_lower for pattern in recoverable_patterns)

    def _attempt_init_conflict_resolution(
        self, clone_path: str, force_init: bool
    ) -> bool:
        """
        Attempt to resolve initialization conflicts.

        Args:
            clone_path: Path to repository
            force_init: Whether force flag was already used

        Returns:
            bool: True if conflict was resolved
        """
        try:
            if not force_init:
                # Try init with force flag if it wasn't already used
                logging.info("Attempting init conflict resolution with --force flag")
                result = subprocess.run(
                    ["cidx", "init", "--embedding-provider", "voyage-ai", "--force"],
                    cwd=clone_path,
                    capture_output=True,
                    text=True,
                    timeout=self.resource_config.git_init_conflict_timeout,
                )
                return result.returncode == 0
            else:
                # Force was already used, try cleanup and retry
                logging.info("Attempting init conflict resolution with cleanup")
                config_dir = os.path.join(clone_path, ".code-indexer")
                if os.path.exists(config_dir):
                    import shutil

                    shutil.rmtree(config_dir)

                result = subprocess.run(
                    ["cidx", "init", "--embedding-provider", "voyage-ai"],
                    cwd=clone_path,
                    capture_output=True,
                    text=True,
                    timeout=self.resource_config.git_init_conflict_timeout,
                )
                return result.returncode == 0

        except subprocess.CalledProcessError as e:
            logging.warning(
                f"Init conflict resolution failed: Command failed with exit code {e.returncode}: {e.stderr}"
            )
            return False
        except subprocess.TimeoutExpired as e:
            logging.warning(
                f"Init conflict resolution failed: Command timed out after {e.timeout} seconds"
            )
            return False
        except FileNotFoundError as e:
            logging.warning(
                f"Init conflict resolution failed: Required command not found: {str(e)}"
            )
            return False
        except PermissionError as e:
            logging.warning(
                f"Init conflict resolution failed: Permission denied: {str(e)}"
            )
            return False
        except OSError as e:
            logging.warning(f"Init conflict resolution failed: System error: {str(e)}")
            return False

    def _attempt_service_conflict_resolution(self, clone_path: str) -> bool:
        """
        Attempt to resolve service conflicts.

        Args:
            clone_path: Path to repository

        Returns:
            bool: True if conflict was resolved
        """
        try:
            # Try stopping any existing services first
            logging.info(
                "Attempting service conflict resolution by stopping existing services"
            )
            subprocess.run(
                ["cidx", "stop"],
                cwd=clone_path,
                capture_output=True,
                text=True,
                timeout=self.resource_config.git_service_cleanup_timeout,
            )

            # Wait for service cleanup using proper event-based waiting
            if not self._wait_for_service_cleanup(
                clone_path, timeout=self.resource_config.git_service_wait_timeout
            ):
                logging.warning(
                    "Service cleanup wait timed out, proceeding with start attempt"
                )

            # Try starting again
            result = subprocess.run(
                ["cidx", "start"],
                cwd=clone_path,
                capture_output=True,
                text=True,
                timeout=self.resource_config.git_service_conflict_timeout,
            )
            return result.returncode == 0

        except subprocess.CalledProcessError as e:
            logging.warning(
                f"Service conflict resolution failed: Command failed with exit code {e.returncode}: {e.stderr}"
            )
            return False
        except subprocess.TimeoutExpired as e:
            logging.warning(
                f"Service conflict resolution failed: Command timed out after {e.timeout} seconds"
            )
            return False
        except FileNotFoundError as e:
            logging.warning(
                f"Service conflict resolution failed: Required command not found: {str(e)}"
            )
            return False
        except PermissionError as e:
            logging.warning(
                f"Service conflict resolution failed: Permission denied: {str(e)}"
            )
            return False
        except OSError as e:
            logging.warning(
                f"Service conflict resolution failed: System error: {str(e)}"
            )
            return False

    def _wait_for_service_cleanup(self, clone_path: str, timeout: int = 30) -> bool:
        """
        Wait for service cleanup using polling without artificial sleep delays.

        Args:
            clone_path: Path to repository
            timeout: Maximum time to wait in seconds

        Returns:
            bool: True if services are cleaned up, False if timeout
        """
        start_time = time.time()
        while time.time() - start_time < timeout:
            # Check if services are stopped by attempting status check
            try:
                result = subprocess.run(
                    ["cidx", "status"],
                    cwd=clone_path,
                    capture_output=True,
                    text=True,
                    timeout=self.resource_config.git_process_check_timeout,
                )
                # If status shows no services or fails with "not running", cleanup is complete
                if result.returncode != 0 or "not running" in result.stdout.lower():
                    return True
            except subprocess.CalledProcessError:
                # Status check failed, assume services are down
                return True
            except subprocess.TimeoutExpired:
                # Status check timed out, assume services are down
                return True
            except FileNotFoundError:
                # Status command not found, assume services are down
                return True
            except PermissionError:
                # Permission denied for status check, assume services are down
                return True
            except OSError:
                # System error during status check, assume services are down
                return True

            # Yield control without sleep - just let the loop continue
            pass

        return False

    def find_by_canonical_url(self, canonical_url: str) -> List[Dict[str, Any]]:
        """
        Find golden repositories by canonical git URL.

        Args:
            canonical_url: Canonical form of git URL (e.g., "github.com/user/repo")

        Returns:
            List of matching golden repository dictionaries
        """
        from ..services.git_url_normalizer import GitUrlNormalizer

        normalizer = GitUrlNormalizer()
        matching_repos = []

        for repo in self.golden_repos.values():
            try:
                # Normalize the repository's URL
                normalized = normalizer.normalize(repo.repo_url)

                # Check if it matches the target canonical URL
                if normalized.canonical_form == canonical_url:
                    repo_dict = repo.to_dict()

                    # Add canonical URL and branch information - need to cast to Dict[str, Any]
                    repo_dict_any: Dict[str, Any] = dict(
                        repo_dict
                    )  # Convert to Dict[str, Any]
                    repo_dict_any["canonical_url"] = canonical_url
                    # Use canonical path resolution to handle versioned structure repos
                    actual_path = self.get_actual_repo_path(repo.alias)
                    repo_dict_any["branches"] = self._get_repository_branches(
                        actual_path
                    )

                    matching_repos.append(repo_dict_any)

            except Exception as e:
                logging.warning(
                    f"Failed to normalize URL for repo {repo.alias}: {str(e)}"
                )
                continue

        return matching_repos

    def _get_repository_branches(self, repo_path: str) -> List[str]:
        """
        Get list of branches for a repository.

        Args:
            repo_path: Path to the repository

        Returns:
            List of branch names
        """
        try:
            if not os.path.exists(repo_path):
                return ["main"]  # Default fallback

            # Get branches using git command
            result = subprocess.run(
                ["git", "branch", "-r"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=self.resource_config.git_untracked_file_timeout,
            )

            if result.returncode == 0:
                branches = []
                for line in result.stdout.strip().split("\n"):
                    if line.strip():
                        # Clean up branch name (remove origin/ prefix)
                        branch = line.strip().replace("origin/", "")
                        if branch and branch != "HEAD":
                            branches.append(branch)

                return branches if branches else ["main"]
            else:
                return ["main"]

        except Exception as e:
            logging.warning(f"Failed to get branches for {repo_path}: {str(e)}")
            return ["main"]

    def get_golden_repo(self, alias: str) -> Optional[GoldenRepo]:
        """
        Get golden repository by alias from SQLite (source of truth).

        Args:
            alias: Repository alias

        Returns:
            GoldenRepo object if found, None otherwise
        """
        repo_data = self._sqlite_backend.get_repo(alias)
        if repo_data is None:
            return None
        return GoldenRepo(**repo_data)

    def golden_repo_exists(self, alias: str) -> bool:
        """
        Check if a golden repository exists.

        Args:
            alias: Repository alias

        Returns:
            True if repository exists, False otherwise
        """
        return alias in self.golden_repos

    def get_wiki_enabled(self, alias: str) -> bool:
        """Check if wiki is enabled for a golden repo (Story #280)."""
        try:
            repo = self._sqlite_backend.get_repo(alias)
            if repo is None:
                return False
            return bool(repo.get("wiki_enabled", False))
        except Exception as e:
            logger.warning("Failed to get wiki_enabled for alias '%s': %s", alias, e)
            return False

    def set_wiki_enabled(self, alias: str, enabled: bool) -> None:
        """Set wiki_enabled flag for a golden repo (Story #280)."""
        self._sqlite_backend.update_wiki_enabled(alias, enabled)

    def save_temporal_options(self, alias: str, options: Optional[Dict]) -> bool:
        """
        Persist temporal indexing options for a golden repo (Story #478).

        Args:
            alias: Repository alias.
            options: Dict with temporal options (max_commits, diff_context,
                     since_date, all_branches), or None to clear.

        Returns:
            True if updated, False if alias not found.
        """
        updated = self._sqlite_backend.update_temporal_options(alias, options)
        if updated and alias in self.golden_repos:
            self.golden_repos[alias].temporal_options = options
        return updated  # type: ignore[no-any-return]

    def get_actual_repo_path(self, alias: str) -> str:
        """
        Resolve actual filesystem path for golden/global repo.

        Resolves the canonical filesystem path for a golden repository,
        handling mixed topology (flat structure + versioned structure).

        This method is critical for fixing Bug #3, Bug #4, Topology Bug 2.1,
        and Topology Bug 3.1 where metadata paths become stale when repos
        are stored in .versioned/ structure but metadata still points to
        flat structure paths.

        Checks in priority order:
        1. Metadata clone_path (legacy flat structure)
        2. .versioned/{alias}/v_*/ (versioned structure, latest version)
        3. Raise GoldenRepoNotFoundError if not found

        Security: Validates alias and paths to prevent path traversal attacks.

        Args:
            alias: Repository alias

        Returns:
            str: Actual filesystem path to repository

        Raises:
            ValueError: If alias contains path traversal characters or resolved path escapes sandbox
            GoldenRepoNotFoundError: If repo doesn't exist in metadata or filesystem
        """
        # SECURITY: Reject dangerous characters in alias (path traversal protection)
        if ".." in alias:
            raise ValueError(
                f"Invalid alias '{alias}': cannot contain path traversal characters (..)"
            )
        if "/" in alias:
            raise ValueError(
                f"Invalid alias '{alias}': cannot contain path traversal characters (/)"
            )
        if "\\" in alias:
            raise ValueError(
                f"Invalid alias '{alias}': cannot contain path traversal characters (\\)"
            )

        # Check if alias exists in metadata
        if alias not in self.golden_repos:
            raise GoldenRepoNotFoundError(
                f"Golden repository '{alias}' not found in metadata"
            )

        golden_repo = self.golden_repos[alias]
        metadata_path = golden_repo.clone_path

        # Get logger for this module
        logger = logging.getLogger(__name__)

        # Priority 1: Check if metadata path exists on filesystem
        if os.path.exists(metadata_path):
            # SECURITY: Verify resolved path stays within golden_repos_dir (symlink protection)
            # Only validate paths that actually exist to avoid blocking test fixtures
            resolved_metadata = os.path.realpath(metadata_path)
            golden_repos_realpath = os.path.realpath(self.golden_repos_dir)
            if not resolved_metadata.startswith(golden_repos_realpath):
                raise ValueError(
                    f"Security violation: resolved path '{resolved_metadata}' "
                    f"outside golden repos directory '{self.golden_repos_dir}'"
                )
            return metadata_path

        # Priority 2: Check .versioned/{alias}/ structure
        versioned_base = os.path.join(self.golden_repos_dir, ".versioned", alias)

        if os.path.exists(versioned_base):
            # SECURITY: Verify versioned path also stays within bounds
            # Only validate paths that actually exist to avoid blocking test fixtures
            resolved_versioned_base = os.path.realpath(versioned_base)
            golden_repos_realpath = os.path.realpath(self.golden_repos_dir)
            if not resolved_versioned_base.startswith(golden_repos_realpath):
                raise ValueError(
                    f"Security violation: versioned path '{resolved_versioned_base}' "
                    f"outside golden repos directory"
                )

            # Find all v_* subdirectories
            version_dirs = []
            for entry in os.listdir(versioned_base):
                if entry.startswith("v_") and os.path.isdir(
                    os.path.join(versioned_base, entry)
                ):
                    version_dirs.append(entry)

            # SECURITY: Filter valid version directories and skip malformed ones
            if version_dirs:
                valid_versions = []
                for v_dir in version_dirs:
                    try:
                        # Extract timestamp from v_TIMESTAMP format
                        timestamp = int(v_dir.split("_")[1])
                        valid_versions.append((v_dir, timestamp))
                    except (ValueError, IndexError) as e:
                        # Skip malformed version directories gracefully
                        logger.warning(
                            format_error_log(
                                "REPO-GENERAL-040",
                                f"Skipping malformed version directory: {v_dir} "
                                f"(expected format: v_TIMESTAMP, error: {e})",
                                extra={"correlation_id": get_correlation_id()},
                            )
                        )
                        continue

                if valid_versions:
                    # Sort by timestamp (highest = latest)
                    valid_versions.sort(key=lambda x: x[1], reverse=True)
                    latest_version = valid_versions[0][0]
                    versioned_path = os.path.join(versioned_base, latest_version)
                    return versioned_path

        # Not found in either location
        raise GoldenRepoNotFoundError(
            f"Golden repository '{alias}' not found on filesystem.\n"
            f"Attempted paths:\n"
            f"  1. Metadata path: {metadata_path}\n"
            f"  2. Versioned path: {versioned_base}/v_*/"
        )

    def user_can_access_golden_repo(self, alias: str, user: Any) -> bool:
        """
        Check if a user can access a golden repository.

        For now, all authenticated users can access all golden repositories.
        This method exists for future permission system expansion.

        Args:
            alias: Repository alias
            user: User object (can be None for unauthenticated)

        Returns:
            True if user can access repository, False otherwise
        """
        # Golden repositories are accessible to all authenticated users
        return user is not None

    def get_golden_repo_branches(self, alias: str) -> List["GoldenRepoBranchInfo"]:
        """
        Get branches for a golden repository.

        This method delegates to the branch service for actual branch retrieval.
        Kept here for compatibility and future enhancement.

        Args:
            alias: Repository alias

        Returns:
            List of GoldenRepoBranchInfo objects

        Raises:
            GoldenRepoError: If repository not found or operation fails
        """
        # Import here to avoid circular imports
        from code_indexer.server.services.golden_repo_branch_service import (
            GoldenRepoBranchService,
        )

        if not self.golden_repo_exists(alias):
            raise GoldenRepoError(f"Golden repository '{alias}' not found")

        branch_service = GoldenRepoBranchService(self)
        branches: List["GoldenRepoBranchInfo"] = (
            branch_service.get_golden_repo_branches(alias)
        )
        return branches

    # ------------------------------------------------------------------
    # Story #303: Change active branch of golden repository
    # Default timeouts (seconds) used when resource_config is absent.
    # ------------------------------------------------------------------
    _CB_GIT_TIMEOUT: int = 600  # 10 min for fetch / checkout / pull
    _CB_COW_TIMEOUT: int = 600  # 10 min for CoW clone
    _CB_FIX_TIMEOUT: int = 60  # 1 min for cidx fix-config

    def _cb_git_fetch_and_validate(
        self, base_clone_path: str, target_branch: str, git_timeout: int
    ) -> None:
        """Fetch remote and validate that target_branch exists (AC3)."""
        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=base_clone_path,
            capture_output=True,
            text=True,
            timeout=git_timeout,
            check=True,
        )
        result = subprocess.run(
            ["git", "branch", "-r"],
            cwd=base_clone_path,
            capture_output=True,
            text=True,
            timeout=git_timeout,
            check=True,
        )
        remote_branches = {b.strip() for b in result.stdout.splitlines() if b.strip()}
        if f"origin/{target_branch}" not in remote_branches:
            available = sorted(remote_branches)
            raise ValueError(
                f"Branch '{target_branch}' does not exist on remote. "
                f"Available remote branches: {available}"
            )

    def _cb_checkout_and_pull(
        self, base_clone_path: str, target_branch: str, git_timeout: int
    ) -> None:
        """Checkout target branch and pull latest commits."""
        subprocess.run(
            ["git", "checkout", target_branch],
            cwd=base_clone_path,
            capture_output=True,
            text=True,
            timeout=git_timeout,
            check=True,
        )
        subprocess.run(
            ["git", "pull", "origin", target_branch],
            cwd=base_clone_path,
            capture_output=True,
            text=True,
            timeout=git_timeout,
            check=True,
        )

    def _cb_cidx_index(self, base_clone_path: str) -> None:
        """Run cidx index --fts on the base clone.

        Bug #467: No timeout — let indexing run to completion.
        The HNSW filtered rebuild handles branch isolation by rebuilding
        the HNSW index with only visible-branch files.
        """
        try:
            result = subprocess.run(
                ["cidx", "index", "--fts"],
                cwd=base_clone_path,
                capture_output=True,
                text=True,
                check=True,
            )
            if result.stdout:
                logger.debug(
                    "[change_branch] cidx index stdout:\n%s", result.stdout[-2000:]
                )
            if result.stderr:
                logger.warning(
                    "[change_branch] cidx index stderr:\n%s", result.stderr[-2000:]
                )
        except subprocess.CalledProcessError as e:
            logger.error(
                "[change_branch] cidx index failed (exit %d):\nstdout: %s\nstderr: %s",
                e.returncode,
                (e.stdout or "")[-2000:],
                (e.stderr or "")[-2000:],
            )
            raise

    def _cb_cow_snapshot(
        self,
        alias: str,
        base_clone_path: str,
        cow_timeout: int,
        cidx_fix_timeout: int,
    ) -> str:
        """Create CoW snapshot and return its path."""
        versioned_base = os.path.join(self.golden_repos_dir, ".versioned", alias)
        os.makedirs(versioned_base, exist_ok=True)
        snapshot_path = os.path.join(versioned_base, f"v_{int(time.time())}")

        subprocess.run(
            ["cp", "--reflink=auto", "-a", base_clone_path, snapshot_path],
            capture_output=True,
            text=True,
            timeout=cow_timeout,
            check=True,
        )
        # fix-config on clone only (non-fatal)
        try:
            subprocess.run(
                ["cidx", "fix-config", "--force"],
                cwd=snapshot_path,
                capture_output=True,
                text=True,
                timeout=cidx_fix_timeout,
                check=False,
            )
        except Exception as exc:
            logger.warning(
                f"[change_branch] cidx fix-config on clone failed (non-fatal): {exc}"
            )
        return snapshot_path

    def _cb_fts_branch_cleanup(self, snapshot_path: str, target_branch: str) -> None:
        """Run FTS branch isolation cleanup on the versioned CoW snapshot (Bug #307).

        The base-clone FTS cleanup may not persist through CoW snapshot due to
        Tantivy segment file timing. This method runs post-CoW on the snapshot to
        ensure FTS documents for files not in the target branch are deleted.

        Args:
            snapshot_path: Path to the versioned CoW snapshot directory
            target_branch: The branch that is now active (used for log messages)
        """
        fts_index_dir = Path(snapshot_path) / ".code-indexer" / "tantivy_index"
        if not fts_index_dir.exists():
            logger.debug(
                f"[change_branch] No FTS index in snapshot '{snapshot_path}' - skipping FTS cleanup"
            )
            return

        try:
            # Lazy import to keep cidx --help fast (per CLAUDE.md)
            from code_indexer.services.tantivy_index_manager import TantivyIndexManager

            # Get list of files in target branch via git ls-files on the snapshot
            result = subprocess.run(
                ["git", "ls-files"],
                cwd=snapshot_path,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                logger.warning(
                    f"[change_branch] git ls-files failed on snapshot '{snapshot_path}': "
                    f"{result.stderr} - skipping FTS cleanup"
                )
                return

            branch_files = set(result.stdout.splitlines())

            # Open existing FTS index in snapshot (create_new=False to not wipe it)
            fts_manager = TantivyIndexManager(fts_index_dir)
            fts_manager.initialize_index(create_new=False)

            # Get all paths currently indexed
            indexed_paths = fts_manager.get_all_indexed_paths()

            # Delete documents for files not in target branch
            deleted_count = 0
            for indexed_path in indexed_paths:
                if indexed_path not in branch_files:
                    try:
                        fts_manager.delete_document(indexed_path)
                        deleted_count += 1
                    except Exception as e:
                        logger.warning(
                            f"[change_branch] FTS post-CoW delete failed for "
                            f"'{indexed_path}': {e}"
                        )

            if deleted_count > 0:
                fts_manager.commit()
                logger.info(
                    f"[change_branch] FTS post-CoW cleanup: deleted {deleted_count} "
                    f"documents not in branch '{target_branch}'"
                )
            else:
                logger.debug(
                    f"[change_branch] FTS post-CoW cleanup: no documents to delete "
                    f"for branch '{target_branch}'"
                )

        except Exception as e:
            logger.warning(
                f"[change_branch] FTS post-CoW cleanup failed (non-fatal): {e}"
            )

    def _cb_hnsw_branch_cleanup(self, snapshot_path: str, target_branch: str) -> None:
        """Rebuild HNSW index on the versioned CoW snapshot for branch isolation.

        Belt-and-suspenders fix: ensures HNSW index only contains vectors for
        files visible in the target branch, eliminating ghost vectors even if
        the cidx index subprocess failed to perform the filtered rebuild.

        Args:
            snapshot_path: Path to the versioned CoW snapshot directory
            target_branch: The branch that is now active
        """
        index_dir = Path(snapshot_path) / ".code-indexer" / "index"
        if not index_dir.exists():
            logger.debug(
                f"[change_branch] No HNSW index dir in snapshot '{snapshot_path}' "
                "- skipping HNSW cleanup"
            )
            return

        try:
            # Lazy import to keep cidx --help fast (per CLAUDE.md)
            from code_indexer.storage.filesystem_vector_store import (
                FilesystemVectorStore,
            )

            # Get list of files in target branch via git ls-files on the snapshot
            result = subprocess.run(
                ["git", "ls-files"],
                cwd=snapshot_path,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                logger.warning(
                    f"[change_branch] git ls-files failed on snapshot '{snapshot_path}': "
                    f"{result.stderr} - skipping HNSW cleanup"
                )
                return

            branch_files = set(result.stdout.splitlines())

            store = FilesystemVectorStore(index_dir)
            collections = store.list_collections()

            for collection_name in collections:
                try:
                    store.rebuild_hnsw_filtered(
                        collection_name,
                        visible_files=branch_files,
                        current_branch=target_branch,
                    )
                    logger.info(
                        f"[change_branch] HNSW filtered rebuild complete for collection "
                        f"'{collection_name}' (branch '{target_branch}')"
                    )
                except Exception as e:
                    logger.warning(
                        f"[change_branch] HNSW rebuild failed for collection "
                        f"'{collection_name}' (non-fatal): {e}"
                    )

        except Exception as e:
            logger.warning(
                f"[change_branch] HNSW post-CoW cleanup failed (non-fatal): {e}"
            )

    def _cb_swap_alias(self, alias: str, new_snapshot_path: str) -> None:
        """Atomically swap alias JSON to new snapshot; schedule old snapshot cleanup."""
        # Import here to avoid circular dependency (alias_manager imports from server)
        from code_indexer.global_repos.alias_manager import AliasManager

        global_alias = f"{alias}-global"
        aliases_dir = os.path.join(self.golden_repos_dir, "aliases")
        os.makedirs(aliases_dir, exist_ok=True)
        alias_manager = AliasManager(aliases_dir)

        current_target = alias_manager.read_alias(global_alias)
        alias_manager.create_alias(global_alias, new_snapshot_path, repo_name=alias)

        if current_target and ".versioned" in current_target:
            try:
                # Import here to avoid circular dependency (app imports from repos)
                from code_indexer.server import app as app_module

                lifecycle = getattr(
                    app_module.app.state, "global_lifecycle_manager", None
                )
                if lifecycle and getattr(lifecycle, "refresh_scheduler", None):
                    cm = getattr(lifecycle.refresh_scheduler, "cleanup_manager", None)
                    if cm:
                        cm.schedule_cleanup(current_target)
            except Exception as exc:
                logger.warning(
                    f"[change_branch] Cleanup scheduling failed (non-fatal): {exc}"
                )

    def change_branch(
        self, alias: str, target_branch: str, progress_callback=None
    ) -> Dict[str, Any]:
        """
        Change the active branch of a golden repository (Story #303).

        See class docstring and story algorithm for full lifecycle description.

        Args:
            alias: Golden repository alias (without -global suffix).
            target_branch: Branch name to switch to.

        Returns:
            Dict with keys: success (bool), message (str).

        Raises:
            GoldenRepoNotFoundError: If alias not registered.
            RuntimeError: If write lock cannot be acquired (repo busy).
            ValueError: If target branch does not exist on remote.
            RuntimeError: If any git or indexing step fails.
        """
        if not target_branch or not re.match(
            r"^[a-zA-Z0-9_][a-zA-Z0-9_./-]*$", target_branch
        ):
            raise ValueError(f"Invalid branch name: '{target_branch}'")

        if alias not in self.golden_repos:
            raise GoldenRepoNotFoundError(f"Golden repository '{alias}' not found")

        golden_repo = self.golden_repos[alias]
        if target_branch == golden_repo.default_branch:
            return {"success": True, "message": f"Already on branch '{target_branch}'"}

        scheduler = getattr(self, "_refresh_scheduler", None)
        if scheduler is not None:
            if scheduler.is_write_locked(alias):
                raise RuntimeError(
                    f"Repository '{alias}' is currently being indexed or refreshed. "
                    "Try again later."
                )
            if not scheduler.acquire_write_lock(alias, owner_name="branch_change"):
                raise RuntimeError(
                    f"Could not acquire write lock for repository '{alias}'."
                )

        try:
            rc = self.resource_config
            git_t = (
                getattr(rc, "git_pull_timeout", self._CB_GIT_TIMEOUT)
                if rc
                else self._CB_GIT_TIMEOUT
            )
            cow_t = (
                getattr(rc, "cow_clone_timeout", self._CB_COW_TIMEOUT)
                if rc
                else self._CB_COW_TIMEOUT
            )
            fix_t = (
                getattr(rc, "cidx_fix_config_timeout", self._CB_FIX_TIMEOUT)
                if rc
                else self._CB_FIX_TIMEOUT
            )

            base = golden_repo.clone_path
            previous_branch = golden_repo.default_branch
            self._cb_git_fetch_and_validate(base, target_branch, git_t)
            self._cb_checkout_and_pull(base, target_branch, git_t)
            try:
                # Story #482 PATH D: coarse progress markers around each major step.
                # _cb_cidx_index/_cb_cow_snapshot are shared callbacks used elsewhere;
                # we emit markers before/after each call in change_branch() itself.
                if progress_callback is not None:
                    progress_callback(
                        0,
                        phase="index",
                        detail="branch change: rebuilding index on base clone...",
                    )
                self._cb_cidx_index(base)
                if progress_callback is not None:
                    progress_callback(
                        60,
                        phase="cow",
                        detail="branch change: creating versioned snapshot...",
                    )
                snapshot = self._cb_cow_snapshot(alias, base, cow_t, fix_t)
                if progress_callback is not None:
                    progress_callback(
                        80,
                        phase="cleanup",
                        detail="branch change: cleaning up FTS/HNSW branch data...",
                    )
                self._cb_fts_branch_cleanup(snapshot, target_branch)
                self._cb_hnsw_branch_cleanup(snapshot, target_branch)
                if progress_callback is not None:
                    progress_callback(
                        95,
                        phase="swap",
                        detail="branch change: activating new snapshot...",
                    )
                self._cb_swap_alias(alias, snapshot)
                if progress_callback is not None:
                    progress_callback(
                        100,
                        phase="complete",
                        detail=f"branch change: switched to '{target_branch}'",
                    )
            except Exception as exc:
                # Bug #469: Rollback git HEAD to previous branch on partial failure.
                try:
                    subprocess.run(
                        ["git", "checkout", previous_branch],
                        cwd=base,
                        check=True,
                        capture_output=True,
                    )
                except Exception as rollback_exc:
                    logger.error(
                        "[change_branch] Rollback to '%s' failed for '%s': %s",
                        previous_branch,
                        alias,
                        rollback_exc,
                    )
                raise exc

            self._sqlite_backend.update_default_branch(alias, target_branch)
            # Invalidate branch-dependent tracking metadata (AC1, Story #303)
            self._sqlite_backend.invalidate_description_refresh_tracking(alias)
            self._sqlite_backend.invalidate_dependency_map_tracking(alias)
            with self._operation_lock:
                old = self.golden_repos[alias]
                self.golden_repos[alias] = GoldenRepo(
                    alias=old.alias,
                    repo_url=old.repo_url,
                    default_branch=target_branch,
                    clone_path=old.clone_path,
                    created_at=old.created_at,
                    enable_temporal=old.enable_temporal,
                    temporal_options=old.temporal_options,
                    category_id=old.category_id,
                    category_auto_assigned=old.category_auto_assigned,
                )

            logger.info(
                "[change_branch] Branch changed for '%s' to '%s'", alias, target_branch
            )
            return {"success": True, "message": f"Branch changed to '{target_branch}'"}

        finally:
            if scheduler is not None:
                scheduler.release_write_lock(alias, owner_name="branch_change")

    def change_branch_async(
        self,
        alias: str,
        target_branch: str,
        submitter_username: str,
    ) -> Dict[str, Any]:
        """
        Submit a branch-change operation as a background job (Story #308).

        Validates inputs eagerly and returns immediately with a job_id.
        The actual branch change runs in a background thread via BackgroundJobManager.

        Args:
            alias: Golden repository alias (without -global suffix).
            target_branch: Branch name to switch to.
            submitter_username: Username submitting the request (for audit logging).

        Returns:
            Dict with keys:
                success (bool): True always on successful submission.
                job_id (str | None): Background job ID, or None if already on branch.

        Raises:
            ValueError: If branch name is syntactically invalid.
            GoldenRepoNotFoundError: If alias is not registered.
            DuplicateJobError: If a change_branch job is already running for this repo.
        """
        if not target_branch or not re.match(
            r"^[a-zA-Z0-9_][a-zA-Z0-9_./-]*$", target_branch
        ):
            raise ValueError(f"Invalid branch name: '{target_branch}'")

        if alias not in self.golden_repos:
            raise GoldenRepoNotFoundError(f"Golden repository '{alias}' not found")

        golden_repo = self.golden_repos[alias]
        if target_branch == golden_repo.default_branch:
            return {"success": True, "job_id": None}

        def background_worker(progress_callback=None) -> Dict[str, Any]:
            """Execute change_branch in a background thread (Story #482 PATH D)."""
            return self.change_branch(
                alias, target_branch, progress_callback=progress_callback
            )

        job_id = self.background_job_manager.submit_job(
            operation_type="change_branch",
            func=background_worker,
            submitter_username=submitter_username,
            is_admin=True,
            repo_alias=alias,
        )
        return {"success": True, "job_id": job_id}

    def add_index_to_golden_repo(
        self,
        alias: str,
        index_type: str,
        submitter_username: str = "admin",
    ) -> str:
        """
        Add a single index type to an existing golden repository.

        Delegates to add_indexes_to_golden_repo (plural) for backward compatibility.
        Kept because MCP handlers and external callers use the singular form.

        Args:
            alias: The golden repo alias
            index_type: One of "semantic", "fts", "temporal", "scip"
            submitter_username: Username for audit logging

        Returns:
            job_id: The background job ID for tracking

        Raises:
            ValueError: If alias not found or index_type invalid
        """
        return self.add_indexes_to_golden_repo(
            alias=alias,
            index_types=[index_type],
            submitter_username=submitter_username,
        )

    def add_indexes_to_golden_repo(
        self,
        alias: str,
        index_types: List[str],
        submitter_username: str = "admin",
    ) -> str:
        """
        Add one or more index types to an existing golden repository atomically.

        Bug #473 Fix: This method acquires the write lock before indexing starts,
        runs all index types sequentially in a single background job, then creates
        a CoW snapshot + alias swap so the rebuilt indexes become visible to queries.

        The old add_index_to_golden_repo (singular) delegates to this method.

        Args:
            alias: The golden repo alias
            index_types: List of index types, each one of "semantic", "fts", "temporal", "scip"
            submitter_username: Username for audit logging

        Returns:
            job_id: The background job ID for tracking

        Raises:
            ValueError: If alias not found or any index_type is invalid
        """
        # Validate alias exists
        if alias not in self.golden_repos:
            raise ValueError(f"Golden repository '{alias}' not found")

        # Validate all index types up front
        valid_index_types = ["semantic", "fts", "temporal", "scip"]
        for index_type in index_types:
            if index_type not in valid_index_types:
                raise ValueError(
                    f"Invalid index_type: {index_type}. Must be one of: {', '.join(valid_index_types)}"
                )

        def background_worker(progress_callback=None) -> Dict[str, Any]:
            """Execute add index operations in a single background thread with write lock + CoW."""
            # Acquire write lock (Bug #473 Fix 1)
            scheduler = getattr(self, "_refresh_scheduler", None)
            lock_acquired = False

            if scheduler is not None:
                lock_acquired = scheduler.acquire_write_lock(
                    alias, owner_name="add_index"
                )
                if not lock_acquired:
                    raise GoldenRepoError(
                        f"Repository '{alias}' is currently being refreshed or indexed. "
                        "Try again later."
                    )

            try:
                # Get repository details
                repo = self.golden_repos[alias]
                # Use canonical path resolution (base clone, not versioned snapshot)
                repo_path = self.get_actual_repo_path(alias)

                # Accumulated stdout/stderr across all index types
                all_stdout = ""
                all_stderr = ""

                # Story #480: Gather repo metrics and build phase allocator
                from code_indexer.services.progress_phase_allocator import (
                    ProgressPhaseAllocator,
                )
                from code_indexer.services.progress_subprocess_runner import (
                    run_with_popen_progress as _run_with_popen_progress_shared,
                    IndexingSubprocessError,
                )

                file_count, commit_count = _gather_repo_metrics(repo_path)
                temporal_options = repo.temporal_options or {}
                max_commits_opt = temporal_options.get("max_commits")

                allocator = ProgressPhaseAllocator()
                allocator.calculate_weights(
                    index_types=index_types,
                    file_count=file_count,
                    commit_count=commit_count,
                    max_commits=max_commits_opt,
                )

                # Adapt all_stdout/all_stderr strings to list form for shared utility
                _stdout_lines: List[str] = []
                _stderr_lines: List[str] = []

                def _run_with_popen_progress(
                    command: List[str],
                    phase_name: str,
                    error_label: str,
                ) -> None:
                    """Delegate to shared run_with_popen_progress utility.

                    Catches IndexingSubprocessError (raised by the shared utility
                    to avoid circular imports) and re-raises as GoldenRepoError
                    which is the expected error type for callers of this path.
                    """
                    nonlocal all_stdout, all_stderr
                    try:
                        _run_with_popen_progress_shared(
                            command=command,
                            phase_name=phase_name,
                            allocator=allocator,
                            progress_callback=progress_callback,
                            all_stdout=_stdout_lines,
                            all_stderr=_stderr_lines,
                            cwd=str(repo_path),
                            error_label=error_label,
                        )
                    except IndexingSubprocessError as e:
                        raise GoldenRepoError(str(e)) from e
                    finally:
                        all_stdout += "".join(_stdout_lines)
                        all_stderr += "".join(_stderr_lines)
                        _stdout_lines.clear()
                        _stderr_lines.clear()

                # Ensure repo is initialized before running cidx index commands (idempotent)
                init_result = subprocess.run(
                    ["cidx", "init"],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                )
                init_output = (init_result.stdout or "") + (init_result.stderr or "")
                if (
                    init_result.returncode != 0
                    and "already exists" not in init_output.lower()
                ):
                    error_details = (
                        init_result.stderr
                        or init_result.stdout
                        or f"Exit code {init_result.returncode}"
                    )
                    raise GoldenRepoError(
                        f"Failed to initialize repo before indexing: {error_details}"
                    )
                if init_result.stdout:
                    all_stdout += f"[init] {init_result.stdout}\n"
                if init_result.stderr:
                    all_stderr += f"[init] {init_result.stderr}\n"

                # Run each requested index type sequentially
                for index_type in index_types:
                    if index_type == "semantic":
                        # Bug #468: --clear forces full rebuild for already-indexed repos
                        # Story #480: Add --progress-json for real-time progress streaming
                        command = ["cidx", "index", "--clear", "--progress-json"]
                        _run_with_popen_progress(
                            command,
                            phase_name="semantic",
                            error_label="create semantic index",
                        )

                    elif index_type == "fts":
                        # FTS is fast: coarse start/end markers only
                        if progress_callback is not None:
                            progress_callback(
                                int(allocator.phase_start("fts")),
                                phase="fts",
                                detail="FTS: building index...",
                            )
                        command = ["cidx", "index", "--rebuild-fts-index"]
                        result = subprocess.run(
                            command,
                            cwd=repo_path,
                            capture_output=True,
                            text=True,
                        )
                        all_stdout += result.stdout or ""
                        all_stderr += result.stderr or ""
                        if result.returncode != 0:
                            error_details = (
                                result.stderr
                                or result.stdout
                                or f"Exit code {result.returncode}"
                            )
                            raise GoldenRepoError(
                                f"Failed to create FTS index: {error_details}"
                            )
                        if progress_callback is not None:
                            progress_callback(
                                int(allocator.phase_end("fts")),
                                phase="fts",
                                detail="FTS: complete",
                            )

                    elif index_type == "temporal":
                        # Story #480: Use Popen + line reader for real-time progress
                        command = [
                            "cidx",
                            "index",
                            "--index-commits",
                            "--clear",
                            "--progress-json",
                        ]

                        max_commits = temporal_options.get("max_commits")
                        if max_commits is not None:
                            command.extend(["--max-commits", str(max_commits)])

                        since_date = temporal_options.get("since_date")
                        if since_date:
                            command.extend(["--since-date", since_date])

                        diff_context = temporal_options.get("diff_context")
                        if diff_context is not None:
                            command.extend(["--diff-context", str(diff_context)])

                        if temporal_options.get("all_branches"):
                            command.append("--all-branches")

                        _run_with_popen_progress(
                            command,
                            phase_name="temporal",
                            error_label="create temporal index",
                        )

                        # Bug #131: Update enable_temporal flag in BOTH tables
                        if self._sqlite_backend.update_enable_temporal(alias, True):
                            self.golden_repos[alias].enable_temporal = True
                            logging.info(
                                f"Updated enable_temporal=True for repo {alias} in golden_repos_metadata"
                            )
                        else:
                            logging.warning(
                                f"Failed to update enable_temporal for {alias} in golden_repos_metadata"
                            )

                        global_alias_temporal = f"{alias}-global"
                        try:
                            from code_indexer.global_repos.global_registry import (
                                GlobalRegistry,
                            )
                            from pathlib import Path as PathLib

                            data_dir = PathLib(self.data_dir)
                            golden_repos_dir = data_dir / "golden-repos"
                            sqlite_db_path = str(data_dir / "cidx_server.db")

                            registry = GlobalRegistry(
                                str(golden_repos_dir),
                                use_sqlite=True,
                                db_path=sqlite_db_path,
                            )
                            if (
                                registry._sqlite_backend is not None
                                and registry._sqlite_backend.update_enable_temporal(
                                    global_alias_temporal, True
                                )
                            ):
                                logging.info(
                                    f"Updated enable_temporal=True for repo {global_alias_temporal} in global_repos"
                                )
                            else:
                                logging.warning(
                                    f"Failed to update enable_temporal for {global_alias_temporal} in global_repos"
                                )
                        except Exception as e:
                            logging.error(
                                f"Error updating global_repos table for {global_alias_temporal}: {e}"
                            )

                    elif index_type == "scip":
                        # SCIP is fast: coarse start/end markers only
                        if progress_callback is not None:
                            progress_callback(
                                int(allocator.phase_start("scip")),
                                phase="scip",
                                detail="SCIP: generating...",
                            )
                        command = ["cidx", "scip", "generate"]
                        result = subprocess.run(
                            command,
                            cwd=repo_path,
                            capture_output=True,
                            text=True,
                        )
                        all_stdout += result.stdout or ""
                        all_stderr += result.stderr or ""
                        if result.returncode != 0:
                            error_details = (
                                result.stderr
                                or result.stdout
                                or f"Exit code {result.returncode}"
                            )
                            raise GoldenRepoError(
                                f"Failed to create SCIP index: {error_details}"
                            )
                        if progress_callback is not None:
                            progress_callback(
                                int(allocator.phase_end("scip")),
                                phase="scip",
                                detail="SCIP: complete",
                            )

                # CoW snapshot phase: coarse start marker before operation
                if progress_callback is not None:
                    progress_callback(
                        int(allocator.phase_start("cow")),
                        phase="cow",
                        detail="Creating snapshot...",
                    )

                # Bug #473 Fix 2: Create CoW snapshot + alias swap after all indexing succeeds
                if scheduler is not None:
                    global_alias = f"{alias}-global"
                    # base clone path (NOT the versioned snapshot — indexing ran there)
                    source_path = str(
                        __import__("pathlib").Path(self.data_dir)
                        / "golden-repos"
                        / alias
                    )
                    current_target = scheduler.alias_manager.read_alias(global_alias)

                    new_snapshot = scheduler._create_snapshot(
                        alias_name=global_alias,
                        source_path=source_path,
                    )
                    scheduler.alias_manager.swap_alias(
                        alias_name=global_alias,
                        new_target=new_snapshot,
                        old_target=current_target,
                    )
                    # Only schedule cleanup for versioned snapshots (never for master clone)
                    if ".versioned" in current_target:
                        scheduler.cleanup_manager.schedule_cleanup(current_target)
                    else:
                        logging.info(
                            f"[add_index] Preserving master golden repo (not scheduling cleanup): "
                            f"{current_target}"
                        )

                # CoW snapshot phase: end marker at 100% after swap completes
                if progress_callback is not None:
                    progress_callback(
                        int(allocator.phase_end("cow")),
                        phase="cow",
                        detail="Snapshot complete",
                    )

                return {
                    "success": True,
                    "alias": alias,
                    "message": (
                        f"Index type(s) {index_types} added successfully to '{alias}'"
                    ),
                    "stdout": all_stdout,
                    "stderr": all_stderr,
                }
            except subprocess.CalledProcessError as e:
                error_details = e.stderr or e.stdout or f"Exit code {e.returncode}"
                raise GoldenRepoError(
                    f"Failed to add index: Command failed: {error_details}"
                )
            except GoldenRepoError:
                raise
            except Exception as e:
                raise GoldenRepoError(f"Failed to add index: {str(e)}")
            finally:
                # Release write lock if we acquired it (Bug #473 Fix 1)
                if scheduler is not None and lock_acquired:
                    scheduler.release_write_lock(alias, owner_name="add_index")

        # Bug #473: Use combined operation_type for all index types to avoid
        # multiple independent jobs racing each other
        combined_type = "_".join(["add_index"] + sorted(index_types))
        job_id = self.background_job_manager.submit_job(
            operation_type=combined_type,
            func=background_worker,
            submitter_username=submitter_username,
            is_admin=True,
            repo_alias=alias,
        )
        return cast(str, job_id)

    def _index_exists(self, golden_repo: GoldenRepo, index_type: str) -> bool:
        """
        Check if an index type already exists for a golden repository.

        Args:
            golden_repo: The golden repository object
            index_type: The index type to check ("semantic", "fts", "temporal", "scip")

        Returns:
            True if the index exists, False otherwise
        """
        # Use canonical path resolution to handle versioned structure repos
        actual_path = self.get_actual_repo_path(golden_repo.alias)
        repo_dir = Path(actual_path)

        if index_type == "semantic":
            # semantic index: check for vector index files
            vector_index = repo_dir / ".code-indexer" / "index"
            if vector_index.exists():
                # Check for any .json files in index subdirectories (collection folders)
                return any(vector_index.rglob("*.json"))
            return False

        elif index_type == "fts":
            # fts index: check for tantivy FTS index files
            fts_index = repo_dir / ".code-indexer" / "tantivy_index"
            if fts_index.exists():
                # Check for meta.json or any index files
                return (fts_index / "meta.json").exists() or any(
                    fts_index.rglob("*.json")
                )
            return False

        elif index_type == "temporal":
            from code_indexer.services.temporal.temporal_collection_naming import (
                get_temporal_collections,
            )

            index_dir = repo_dir / ".code-indexer" / "index"
            collections = get_temporal_collections(None, index_dir)
            if not collections:
                return False
            return any(any(coll_path.rglob("*.json")) for _, coll_path in collections)

        elif index_type == "scip":
            # AC3: scip requires .code-indexer/scip/ directory with valid .scip.db files containing data
            scip_dir = repo_dir / ".code-indexer" / "scip"
            if not scip_dir.exists():
                return False

            # Find all .scip.db files
            scip_db_files = list(scip_dir.glob("**/*.scip.db"))
            if not scip_db_files:
                return False

            # Validate at least one .scip.db file has data
            sqlite3_module = None
            try:
                import sqlite3

                sqlite3_module = sqlite3
            except ImportError:
                try:
                    from pysqlite3 import dbapi2 as pysqlite3_compat

                    sqlite3_module = pysqlite3_compat
                except ImportError:
                    pass

            if sqlite3_module is None:
                # If sqlite3 not available, fall back to file size check
                return any(f.stat().st_size > 0 for f in scip_db_files)

            # Check if any database has symbols
            for db_file in scip_db_files:
                if db_file.stat().st_size == 0:
                    continue
                try:
                    conn = sqlite3_module.connect(str(db_file))
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM symbols")
                    count = cursor.fetchone()[0]
                    conn.close()
                    if count > 0:
                        return True
                except Exception:
                    # Database corruption or schema issue - treat as not existing
                    continue

            return False

        return False

    def get_golden_repo_indexes(self, alias: str) -> Dict[str, Any]:
        """
        Get structured status of all index types for a golden repository.

        This method examines the filesystem to determine which index types exist
        for the specified golden repository and returns their current status.

        Args:
            alias: The golden repo alias

        Returns:
            Dictionary with structure:
            {
                "alias": str,
                "indexes": {
                    "semantic_fts": {"exists": bool, "path": str|None, "last_updated": str|None},
                    "temporal": {"exists": bool, "path": str|None, "last_updated": str|None},
                    "scip": {"exists": bool, "path": str|None, "last_updated": str|None}
                }
            }

        Raises:
            ValueError: If alias not found
        """
        # Validate alias exists
        if alias not in self.golden_repos:
            raise ValueError(f"Golden repository '{alias}' not found")

        golden_repo = self.golden_repos[alias]
        # Use canonical path resolution to handle versioned structure repos
        actual_path = self.get_actual_repo_path(alias)
        repo_dir = Path(actual_path)

        # Check each index type using helper method
        # Return separate semantic and fts status (not combined semantic_fts)
        indexes = {
            "semantic": self._get_index_status(repo_dir, "semantic", golden_repo),
            "fts": self._get_index_status(repo_dir, "fts", golden_repo),
            "temporal": self._get_index_status(repo_dir, "temporal", golden_repo),
            "scip": self._get_index_status(repo_dir, "scip", golden_repo),
        }

        return {"alias": alias, "indexes": indexes}

    def _get_index_status(
        self, repo_dir: Path, index_type: str, golden_repo: GoldenRepo
    ) -> Dict[str, Any]:
        """
        Get status dictionary for a single index type.

        Args:
            repo_dir: Repository directory path
            index_type: Index type ("semantic", "fts", "temporal", "scip")
            golden_repo: Golden repository object

        Returns:
            Status dict with exists, path, last_updated keys
        """
        # Map index types to their filesystem paths
        path_map = {
            "semantic": "index",  # Semantic index directory
            "fts": "tantivy_index",  # Full-text search (Tantivy) directory
            "temporal": "index",  # Temporal uses same index directory
            "scip": "scip",  # SCIP directory
        }

        exists = self._index_exists(golden_repo, index_type)
        if exists:
            index_path = (
                repo_dir / ".code-indexer" / path_map.get(index_type, index_type)
            )
            return {
                "exists": True,
                "path": str(index_path),
                "last_updated": self._get_directory_last_modified(index_path),
            }
        return {"exists": False, "path": None, "last_updated": None}

    def _get_directory_last_modified(self, dir_path: Path) -> Optional[str]:
        """
        Get the last modified timestamp of a directory in ISO 8601 format.

        Args:
            dir_path: Path to directory

        Returns:
            ISO 8601 timestamp string or None if directory doesn't exist
        """
        if not dir_path.exists():
            return None

        try:
            mtime = dir_path.stat().st_mtime
            dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
            return dt.isoformat()
        except Exception as e:
            logging.getLogger(__name__).warning(
                f"Failed to get last modified time for {dir_path}: {e}"
            )
            return None


def get_golden_repo_manager() -> "GoldenRepoManager":
    """
    Get the GoldenRepoManager from app state.

    Returns the singleton GoldenRepoManager instance registered on app startup.
    This module-level function allows other services (e.g. ConfigService) to
    access the manager without importing routes or creating circular dependencies.

    Returns:
        GoldenRepoManager instance

    Raises:
        RuntimeError: If the manager has not been initialized (server not started)
    """
    from code_indexer.server import app as app_module

    manager = getattr(app_module.app.state, "golden_repo_manager", None)
    if manager is None:
        raise RuntimeError(
            "golden_repo_manager not initialized. "
            "Server must set app.state.golden_repo_manager during startup."
        )
    return manager  # type: ignore[no-any-return]
