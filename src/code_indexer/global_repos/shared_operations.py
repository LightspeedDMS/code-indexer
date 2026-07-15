"""
Shared business logic for global repo operations across all protocols.

Provides consistent operations for CLI, REST, and MCP protocols to ensure
feature parity and eliminate code duplication.
"""

import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


# Default configuration values
DEFAULT_REFRESH_INTERVAL = 3600  # 1 hour in seconds
MINIMUM_REFRESH_INTERVAL = 60  # Minimum 60 seconds


class GlobalRepoOperations:
    """
    Shared business logic for global repository operations.

    Provides consistent operations used by all protocol handlers (CLI, REST, MCP):
    - List global repos
    - Get repo status
    - Get/set global configuration

    Ensures feature parity across all protocols by centralizing business logic.
    """

    def __init__(self, golden_repos_dir: str):
        """
        Initialize global repo operations.

        Args:
            golden_repos_dir: Path to golden repos directory
        """
        self.golden_repos_dir = Path(golden_repos_dir)

        # Ensure directory structure exists
        self.golden_repos_dir.mkdir(parents=True, exist_ok=True)

        # Do NOT resolve the backend registry here — app.state.backend_registry
        # is not yet populated at construction time during server startup.
        # Resolution is deferred to the `registry` property (accessed at request
        # time, when backend_registry is guaranteed to be set).
        self._registry = None  # cache; None means "not yet resolved"
        self._registry_lock = threading.Lock()

    def _resolve_registry_backend(self):
        """
        Inspect app.state to determine the postgres backend object (if any).

        Bug #1308 remediation item #4: delegates to the shared
        resolve_backend_registry_state() helper (registry_factory.py) so
        RefreshScheduler and GlobalActivator use the EXACT SAME resolution
        logic instead of re-implementing the app.state introspection a
        third time. That helper also fixes item #5: it uses a
        sys.modules.get() lookup instead of a real import, so CLI usage of
        GlobalRepoOperations (cli.py) never pays the cost of building the
        whole FastAPI app just to discover "not in server mode, use SQLite".

        Returns:
            Tuple of (backend, postgres_mode_without_backend):
            - backend: the postgres global_repos backend, or None for SQLite/CLI mode
            - postgres_mode_without_backend: True when storage_mode=postgres but
              backend_registry is not yet set on app.state (transient startup window)
        """
        from code_indexer.server.utils.registry_factory import (
            resolve_backend_registry_state,
        )

        return resolve_backend_registry_state(caller_name="GlobalRepoOperations")

    @property
    def registry(self):
        """
        Lazily resolve the GlobalRegistry or PostgresGlobalRegistryAdapter.

        Defers resolution to request time so that app.state.backend_registry
        is guaranteed to be set in postgres/cluster mode.  In SQLite/CLI mode
        falls back without warning.  Result is cached after first successful
        resolution.  In postgres mode with backend not yet available, result is
        NOT cached so the next request re-checks.

        Double-checked locking (check, lock, re-check) prevents concurrent
        threads from constructing duplicate instances in cacheable paths.  The
        lock also serializes concurrent callers in the postgres-without-backend
        transient window, though sequential re-accesses may each rebuild the
        fallback until backend_registry is populated (intentional: cheap and
        stateless).
        """
        if self._registry is not None:
            return self._registry

        with self._registry_lock:
            # Re-check after acquiring the lock (double-checked locking).
            if self._registry is not None:
                return self._registry

            # Lazy import to avoid circular dependency (Story #713)
            from code_indexer.server.utils.registry_factory import (
                get_server_global_registry,
            )

            backend, postgres_mode_without_backend = self._resolve_registry_backend()
            resolved = get_server_global_registry(
                str(self.golden_repos_dir), backend=backend
            )

            # Cache unless in postgres mode without a backend yet.
            if not postgres_mode_without_backend:
                self._registry = resolved

            return resolved

    def list_repos(self, filters: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """
        List all global repositories with API-normalized field names.

        Reuses GlobalRegistry.list_global_repos() to avoid code duplication.
        Normalizes field names for protocol parity (CLI/REST/MCP).

        Args:
            filters: Optional filters for future use (not currently implemented)

        Returns:
            List of repository metadata dicts with fields:
            - repo_name: Repository name
            - alias: Global alias name (normalized from alias_name)
            - url: Git repository URL or None for meta-directory (normalized from repo_url)
            - last_refresh: ISO timestamp of last refresh
        """
        # Get repos from registry
        repos = self.registry.list_global_repos()

        # Apply filters if provided (placeholder for future functionality)
        if filters:
            # Future: Apply filtering logic here
            pass

        # Normalize field names for protocol parity
        normalized = []
        for repo in repos:
            normalized.append(
                {
                    "alias": repo.get("alias_name"),  # alias_name → alias
                    "repo_name": repo.get("repo_name"),
                    "url": repo.get("repo_url"),  # repo_url → url
                    "last_refresh": repo.get("last_refresh"),
                }
            )

        return normalized

    def get_status(self, alias: str) -> Dict[str, Any]:
        """
        Get detailed status of a specific global repository with API-normalized field names.

        Args:
            alias: Global repository alias name

        Returns:
            Repository status dict with fields:
            - alias: Global alias name (normalized from alias_name)
            - repo_name: Repository name
            - url: Git repository URL (normalized from repo_url)
            - last_refresh: ISO timestamp of last refresh
            - enable_temporal: Whether temporal indexing is enabled

        Raises:
            ValueError: If repository alias not found
        """
        # Get repo from registry
        repo = self.registry.get_global_repo(alias)

        if repo is None:
            raise ValueError(
                f"Global repo '{alias}' not found. "
                f"Run 'cidx global list' to see available repos."
            )

        # Normalize field names for protocol parity
        return {
            "alias": repo.get("alias_name"),  # alias_name → alias
            "repo_name": repo.get("repo_name"),
            "url": repo.get("repo_url"),  # repo_url → url
            "last_refresh": repo.get("last_refresh"),
            "enable_temporal": repo.get(
                "enable_temporal", False
            ),  # Default to False for legacy repos
            # Bug #1204: next_refresh and enable_scip are in the already-loaded record
            # (both SQLite and PG backends SELECT these columns in get_repo/get_global_repo).
            # No extra DB query — values come directly from the record fetched above.
            "next_refresh": repo.get("next_refresh"),  # None when not scheduled
            "enable_scip": repo.get(
                "enable_scip", False
            ),  # Default False for legacy repos
        }

    def get_config(self) -> Dict[str, Any]:
        """
        Get global configuration.

        Returns:
            Configuration dict with fields:
            - refresh_interval: Refresh interval in seconds
            - externally_managed: True when an external owner manages golden-repo
              presence/refresh (server skips self-refresh and startup restore)

        Story #3 - Configuration Consolidation:
        Now reads from centralized config.json via ConfigService instead of
        separate global_config.json file.
        """
        # Story #3: Use ConfigService for centralized configuration
        from code_indexer.server.services.config_service import get_config_service

        try:
            config_service = get_config_service()
            golden_repos_config = config_service.get_config().golden_repos_config
            # golden_repos_config is guaranteed non-None by ServerConfig.__post_init__
            return {
                "refresh_interval": golden_repos_config.refresh_interval_seconds,
                "externally_managed": golden_repos_config.externally_managed,
            }
        except (RuntimeError, ValueError, IOError) as e:
            logger.warning(
                f"Failed to load config from ConfigService, using defaults: {e}"
            )
            # Return default config on error
            return {
                "refresh_interval": DEFAULT_REFRESH_INTERVAL,
                "externally_managed": False,
            }

    def set_config(self, refresh_interval: int) -> None:
        """
        Update global configuration.

        Args:
            refresh_interval: Refresh interval in seconds (minimum 60)

        Raises:
            ValueError: If refresh_interval < 60 seconds

        Story #3 - Configuration Consolidation:
        Now writes to centralized config.json via ConfigService instead of
        separate global_config.json file. Validation is handled by ConfigService.
        """
        # Story #3: Use ConfigService for centralized configuration
        from code_indexer.server.services.config_service import get_config_service

        # Validate refresh interval (matches ConfigService validation rules)
        if refresh_interval < MINIMUM_REFRESH_INTERVAL:
            raise ValueError(
                f"Refresh interval must be at least {MINIMUM_REFRESH_INTERVAL} seconds. "
                f"Got: {refresh_interval} seconds."
            )

        # Update via ConfigService
        config_service = get_config_service()
        config_service.update_setting(
            "golden_repos", "refresh_interval_seconds", refresh_interval
        )

        logger.info(f"Updated global config: refresh_interval={refresh_interval}s")
