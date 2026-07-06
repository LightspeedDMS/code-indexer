"""
Global Activator for orchestrating automatic global activation.

Coordinates alias creation and registry updates when a golden repo
is registered, implementing the automatic activation workflow.
"""

import logging
import threading
from pathlib import Path
from typing import Any, Optional, Dict, Union

from .alias_manager import AliasManager


logger = logging.getLogger(__name__)


class GlobalActivationError(Exception):
    """Exception raised when global activation fails."""

    pass


class GlobalActivator:
    """
    Orchestrates automatic global activation of golden repositories.

    Handles the complete workflow of creating aliases and updating
    the global registry when a golden repo is registered.
    """

    def __init__(self, golden_repos_dir: str):
        """
        Initialize the global activator.

        Args:
            golden_repos_dir: Path to golden repos directory
        """
        self.golden_repos_dir = Path(golden_repos_dir)

        # Initialize components
        aliases_dir = self.golden_repos_dir / "aliases"
        self.alias_manager = AliasManager(str(aliases_dir))

        # Registry resolution (Bug #1308): DEFERRED to the `registry`
        # property below instead of eagerly binding a per-node SQLite
        # GlobalRegistry here. Eager construction-time binding split-brained
        # cluster activation against the shared PostgreSQL registry that the
        # read/list path already used, because app.state.backend_registry is
        # not guaranteed to be populated yet at construction time during
        # server startup.
        self._registry: Optional[Any] = None
        self._registry_lock = threading.Lock()

    @property
    def registry(self) -> Any:
        """
        Lazily resolve the GlobalRegistry / PostgresGlobalRegistryAdapter.

        Bug #1308: mirrors GlobalRepoOperations.registry (shared_operations.py)
        -- resolution is deferred to first access (not __init__) so
        app.state.backend_registry is guaranteed to be populated in
        postgres/cluster mode. Falls back to the per-node SQLite
        GlobalRegistry in solo/CLI mode (no app.state), preserving existing
        behavior byte-for-byte. Result is cached after first successful
        resolution; in postgres mode with backend not yet available, the
        result is NOT cached so the next access re-checks.
        """
        if self._registry is not None:
            return self._registry

        with self._registry_lock:
            if self._registry is not None:
                return self._registry

            # Lazy import to avoid circular dependency (Story #713)
            from code_indexer.server.utils.registry_factory import (
                get_server_global_registry,
                resolve_backend_registry_state,
            )

            backend, postgres_mode_without_backend = resolve_backend_registry_state(
                caller_name="GlobalActivator"
            )
            resolved = get_server_global_registry(
                str(self.golden_repos_dir), backend=backend
            )

            if not postgres_mode_without_backend:
                self._registry = resolved

            return resolved

    def activate_golden_repo(
        self,
        repo_name: str,
        repo_url: str,
        clone_path: str,
        enable_temporal: bool = False,
        temporal_options: Optional[Dict[str, Union[int, str]]] = None,
    ) -> None:
        """
        Activate a golden repository globally.

        Creates an alias and registers the repo in the global registry.
        Uses {repo-name}-global naming convention for aliases.

        Args:
            repo_name: Repository name (e.g., "my-repo")
            repo_url: Git repository URL
            clone_path: Path to the cloned/indexed repository
            enable_temporal: Whether to enable temporal indexing (git history search)
            temporal_options: Temporal indexing options (max_commits, since_date, diff_context)

        Raises:
            GlobalActivationError: If activation fails
        """
        alias_name = f"{repo_name}-global"

        try:
            # Step 1: Create alias pointer file (atomically)
            logger.info(f"Creating global alias: {alias_name}")
            self.alias_manager.create_alias(
                alias_name=alias_name, target_path=clone_path, repo_name=repo_name
            )

            # Step 2: Register in global registry (atomically)
            # Include temporal settings for RefreshScheduler to use (Story #527)
            logger.info(f"Registering in global registry: {alias_name}")
            self.registry.register_global_repo(
                repo_name=repo_name,
                alias_name=alias_name,
                repo_url=repo_url,
                index_path=clone_path,
                enable_temporal=enable_temporal,
                temporal_options=temporal_options,
            )

            # Note: Meta-directory description file is now created automatically
            # via lifecycle hook in golden_repo_manager.py (Story #538)

            logger.info(f"Global activation complete: {alias_name}")

        except Exception as e:
            # Clean up partial state on failure
            error_msg = f"Global activation failed for {repo_name}: {e}"
            logger.error(error_msg)

            # Attempt cleanup of any partial state
            try:
                if self.alias_manager.alias_exists(alias_name):
                    logger.warning(f"Cleaning up alias after failure: {alias_name}")
                    self.alias_manager.delete_alias(alias_name)

                if self.registry.get_global_repo(alias_name):
                    logger.warning(
                        f"Cleaning up registry entry after failure: {alias_name}"
                    )
                    self.registry.unregister_global_repo(alias_name)

            except Exception as cleanup_error:
                logger.error(f"Cleanup failed after activation error: {cleanup_error}")

            # Re-raise as GlobalActivationError
            raise GlobalActivationError(error_msg) from e

    def deactivate_golden_repo(self, repo_name: str) -> None:
        """
        Deactivate a golden repository globally.

        Removes the alias, unregisters from the global registry, and cleans up
        the meta-directory description file.

        Args:
            repo_name: Repository name

        Raises:
            GlobalActivationError: If deactivation fails
        """
        alias_name = f"{repo_name}-global"

        try:
            # Remove from registry
            self.registry.unregister_global_repo(alias_name)

            # Remove alias
            self.alias_manager.delete_alias(alias_name)

            # Note: Meta-directory description file is now deleted automatically
            # via lifecycle hook in golden_repo_manager.py (Story #538)

            logger.info(f"Global deactivation complete: {alias_name}")

        except Exception as e:
            error_msg = f"Global deactivation failed for {repo_name}: {e}"
            logger.error(error_msg)
            raise GlobalActivationError(error_msg) from e

    def is_globally_active(self, repo_name: str) -> bool:
        """
        Check if a repository is globally active.

        Args:
            repo_name: Repository name

        Returns:
            True if globally active, False otherwise
        """
        alias_name = f"{repo_name}-global"
        return self.registry.get_global_repo(alias_name) is not None

    def get_global_alias_name(self, repo_name: str) -> str:
        """
        Get the global alias name for a repository.

        Args:
            repo_name: Repository name

        Returns:
            Global alias name (e.g., "my-repo-global")
        """
        return f"{repo_name}-global"
