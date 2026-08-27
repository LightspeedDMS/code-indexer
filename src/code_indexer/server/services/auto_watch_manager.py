"""
AutoWatchManager - Story #640.

Manages auto-watch lifecycle for server file operations, enabling automatic
watch mode activation during file modifications with timeout-based auto-stop.
"""

from code_indexer.server.middleware.correlation import get_correlation_id

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, cast

from code_indexer.daemon.watch_manager import DaemonWatchManager
from code_indexer.config import ConfigManager
from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)


class AutoWatchManager:
    """
    Manages auto-watch lifecycle for server file operations.

    In server context, ALL watch mode is auto-watch (no manual watch exists).
    Automatically starts watch on file operations and stops after inactivity timeout.
    """

    # Timeout checker runs every 30 seconds
    TIMEOUT_CHECK_INTERVAL_SECONDS = 30
    # Thread join timeout during shutdown
    SHUTDOWN_THREAD_JOIN_TIMEOUT_SECONDS = 5

    def __init__(
        self,
        auto_watch_enabled: bool = True,
        default_timeout: int = 300,
    ):
        """
        Initialize AutoWatchManager.

        Args:
            auto_watch_enabled: Enable/disable auto-watch functionality
            default_timeout: Default timeout in seconds for auto-stop (default: 300)
        """
        self.auto_watch_enabled = auto_watch_enabled
        self.default_timeout = default_timeout
        self._watch_state: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()  # Use RLock to allow reentrant calls
        self._shutdown_event = threading.Event()

        # Bug #1689: the background timeout-checker thread used to start
        # unconditionally right here, so the module-level
        # `auto_watch_manager = AutoWatchManager()` singleton (bottom of
        # this file) spawned a thread as a pure import-time side effect --
        # the exact Bug #1638/#1650 anti-pattern documented in CLAUDE.md's
        # "Module-Level Service Singletons Must Be Lazy (PEP 562)" section.
        # __init__ must stay cheap: the thread is now started lazily, on
        # first real start_watch() call, via
        # _ensure_timeout_thread_started() below -- the only entry point
        # that ever adds state for the checker to examine.
        self._timeout_thread: Optional[threading.Thread] = None
        self._timeout_thread_lock = threading.RLock()

    def _ensure_timeout_thread_started(self) -> None:
        """Lazily start the background timeout-checker thread on first
        real use (Bug #1689). Idempotent and thread-safe (double-checked
        locking): concurrent first-time start_watch() callers must only
        ever start ONE checker thread.
        """
        if self._timeout_thread is not None:
            return
        with self._timeout_thread_lock:
            if self._timeout_thread is not None:
                return
            timeout_thread = threading.Thread(
                target=self._timeout_checker_loop,
                daemon=True,
                name="AutoWatchTimeoutChecker",
            )
            timeout_thread.start()
            self._timeout_thread = timeout_thread
            logger.info(
                "AutoWatchManager timeout checker thread started",
                extra={"correlation_id": get_correlation_id()},
            )

    def is_watching(self, repo_path: str) -> bool:
        """
        Check if watch is currently active for repository.

        Args:
            repo_path: Repository path

        Returns:
            True if watch is running, False otherwise
        """
        with self._lock:
            if repo_path not in self._watch_state:
                return False
            watch_running: bool = self._watch_state[repo_path].get(
                "watch_running", False
            )
            return watch_running

    def start_watch(
        self,
        repo_path: str,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Start watch mode with auto-stop timer.

        If watch already running for this repo, reset timeout instead of creating new instance.

        Args:
            repo_path: Path to repository to watch
            timeout: Timeout in seconds for auto-stop (uses default_timeout if not specified)

        Returns:
            Status dictionary with success/error/disabled status and message
        """
        # Check if auto-watch is enabled
        if not self.auto_watch_enabled:
            logger.info(
                f"Auto-watch disabled, not starting watch for {repo_path}",
                extra={"correlation_id": get_correlation_id()},
            )
            return {
                "status": "disabled",
                "message": "Auto-watch is disabled",
            }

        # Bug #1689: lazily start the background timeout-checker thread on
        # first real watch action, instead of eagerly in __init__.
        self._ensure_timeout_thread_started()

        timeout_seconds = timeout if timeout is not None else self.default_timeout

        with self._lock:
            # Check if watch already running - if so, just reset timeout
            if self.is_watching(repo_path):  # RLock allows this reentrant call
                self._watch_state[repo_path]["last_activity"] = datetime.now()
                self._watch_state[repo_path]["timeout_seconds"] = timeout_seconds
                logger.info(
                    f"Watch already running for {repo_path}, timeout reset to {timeout_seconds}s",
                    extra={"correlation_id": get_correlation_id()},
                )
                return {
                    "status": "success",
                    "message": "Timeout reset",
                }

            # Create new watch instance
            try:
                # Initialize configuration
                config_manager = ConfigManager.create_with_backtrack(Path(repo_path))

                # Bug #1683 (round 3): create_with_backtrack() unconditionally
                # returns a ConfigManager even when NO config was found for
                # repo_path (or any parent) -- it silently defaults
                # config_path to `{start_dir}/.code-indexer/config.json`,
                # which does not exist. get_config()/load() then falls back
                # to a bare `Config()` whose codebase_dir is the unresolved
                # relative `Path(".")`, which resolves against the SERVER
                # PROCESS's CWD -- not repo_path. Every caller of this
                # primitive (files.py's 3 handlers today, any future caller
                # tomorrow) was exposed to this trap; guarding one caller is
                # not sufficient (Messi Rule 13 anti-silent-failure: check
                # the return before trusting it; Messi Rule 2 anti-fallback:
                # fail loud instead of substituting a fallback location).
                if not config_manager.config_path.exists():
                    logger.warning(
                        format_error_log(
                            "APP-GENERAL-068",
                            f"Refusing to start watch for {repo_path}: no "
                            f".code-indexer/config.json found for this path "
                            f"or any parent directory (Bug #1683 round 3)",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )
                    return {
                        "status": "error",
                        "message": (
                            f"No .code-indexer config found for {repo_path}; "
                            f"refusing to start watch"
                        ),
                    }

                config = config_manager.get_config()

                # Defense-in-depth: even with a real config file found
                # up-tree, verify it actually describes repo_path exactly.
                # This MUST be strict equality, not "equal-or-ancestor":
                # the only production caller passes the activated-repo
                # root, whose own config lives at
                # `<repo_path>/.code-indexer/`, so codebase_dir ==
                # repo_path always holds for every legitimate case. Any
                # wider check that permits codebase_dir to be a strict
                # ANCESTOR of repo_path is not a safe relaxation -- it is
                # precisely the unsafe direction: `find_config_path` walks
                # UP the tree from repo_path, so a repo lacking its own
                # config.json but sitting under an ancestor that happens
                # to carry one (e.g. the server data root itself) would
                # pass an ancestor-permitting check and then have
                # `DaemonWatchManager` watch/index the ANCESTOR instead of
                # repo_path (Bug #1683 round 4 -- reproduced live: an
                # ancestor `.code-indexer/config.json` whose codebase_dir
                # pointed at the server data directory let a watch request
                # for an activated repo silently index the entire
                # server-data tree, including cidx_server.db and every
                # golden repo). This guard is reachable two ways in
                # practice: (1) the `CODEBASE_DIR` env-var override path in
                # `create_with_backtrack`, which ignores repo_path entirely
                # and can point anywhere; and (2) exactly the ancestor
                # shape just described, whenever repo_path itself has no
                # config of its own.
                resolved_codebase_dir = Path(config.codebase_dir).resolve()
                resolved_repo_path = Path(repo_path).resolve()
                if resolved_repo_path != resolved_codebase_dir:
                    logger.warning(
                        format_error_log(
                            "APP-GENERAL-069",
                            f"Refusing to start watch for {repo_path}: "
                            f"resolved config.codebase_dir "
                            f"({resolved_codebase_dir}) does not exactly "
                            f"match the requested repo path "
                            f"(Bug #1683 round 4)",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )
                    return {
                        "status": "error",
                        "message": (
                            f"config.codebase_dir ({resolved_codebase_dir}) "
                            f"does not match repo path {repo_path}; refusing "
                            f"to start watch"
                        ),
                    }

                # Create daemon watch manager
                watch_instance = DaemonWatchManager()

                # Start watch
                result = cast(
                    dict[str, Any],
                    watch_instance.start_watch(
                        project_path=repo_path,
                        config=config,
                    ),
                )

                if result.get("status") != "success":
                    logger.error(
                        format_error_log(
                            "APP-GENERAL-049",
                            f"Failed to start watch for {repo_path}: {result}",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )
                    return result

                # Track watch state
                self._watch_state[repo_path] = {
                    "watch_running": True,
                    "last_activity": datetime.now(),
                    "timeout_seconds": timeout_seconds,
                    "watch_instance": watch_instance,
                }

                logger.info(
                    f"Auto-watch started for {repo_path} with {timeout_seconds}s timeout",
                    extra={"correlation_id": get_correlation_id()},
                )
                return {
                    "status": "success",
                    "message": f"Watch started with {timeout_seconds}s timeout",
                }

            except Exception as e:
                logger.exception(
                    f"Error starting auto-watch for {repo_path}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
                return {
                    "status": "error",
                    "message": f"Failed to start watch: {str(e)}",
                }

    def stop_watch(self, repo_path: str) -> Dict[str, Any]:
        """
        Stop auto-watch for repository.

        Args:
            repo_path: Repository path

        Returns:
            Status dictionary with success/error status and message
        """
        with self._lock:
            # Check if watch exists
            if repo_path not in self._watch_state or not self._watch_state[
                repo_path
            ].get("watch_running", False):
                logger.warning(
                    format_error_log(
                        "APP-GENERAL-050",
                        f"No watch running for {repo_path}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return {
                    "status": "error",
                    "message": "Watch not running",
                }

            try:
                # Stop watch instance
                watch_instance = self._watch_state[repo_path]["watch_instance"]
                result = watch_instance.stop_watch()

                # Clear state
                del self._watch_state[repo_path]

                logger.info(
                    f"Auto-watch stopped for {repo_path}",
                    extra={"correlation_id": get_correlation_id()},
                )
                return {
                    "status": "success",
                    "message": "Watch stopped",
                    "stats": result.get("stats", {}),
                }

            except Exception as e:
                logger.exception(
                    f"Error stopping auto-watch for {repo_path}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
                return {
                    "status": "error",
                    "message": f"Failed to stop watch: {str(e)}",
                }

    def reset_timeout(self, repo_path: str) -> Dict[str, Any]:
        """
        Reset auto-stop timer on file activity.

        Args:
            repo_path: Repository path

        Returns:
            Status dictionary with success/error status
        """
        with self._lock:
            if repo_path not in self._watch_state or not self._watch_state[
                repo_path
            ].get("watch_running", False):
                logger.warning(
                    format_error_log(
                        "APP-GENERAL-051",
                        f"No watch running for {repo_path}, cannot reset timeout",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return {
                    "status": "error",
                    "message": "Watch not running",
                }

            # Update last activity timestamp
            self._watch_state[repo_path]["last_activity"] = datetime.now()
            logger.debug(
                f"Timeout reset for {repo_path}",
                extra={"correlation_id": get_correlation_id()},
            )

            return {
                "status": "success",
                "message": "Timeout reset",
            }

    def _timeout_checker_loop(self) -> None:
        """
        Background thread loop that checks for timeout expiration every 30 seconds.

        Runs until shutdown event is set.
        """
        logger.info(
            "Timeout checker loop started",
            extra={"correlation_id": get_correlation_id()},
        )
        while not self._shutdown_event.is_set():
            # Wait for check interval or until shutdown event
            if self._shutdown_event.wait(timeout=self.TIMEOUT_CHECK_INTERVAL_SECONDS):
                break  # Shutdown requested

            # Check for expired timeouts
            try:
                self._check_timeouts()
            except Exception as e:
                logger.exception(
                    f"Error in timeout checker loop: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )

        logger.info(
            "Timeout checker loop stopped",
            extra={"correlation_id": get_correlation_id()},
        )

    def _check_timeouts(self) -> None:
        """
        Check all watches for timeout expiration and stop expired ones.

        Called periodically by background thread (every 30 seconds) to enforce auto-stop.
        """
        with self._lock:
            repos_to_stop = []

            for repo_path, state in self._watch_state.items():
                if not state.get("watch_running", False):
                    continue

                last_activity = state["last_activity"]
                timeout_seconds = state["timeout_seconds"]
                elapsed = (datetime.now() - last_activity).total_seconds()

                if elapsed > timeout_seconds:
                    logger.info(
                        f"Watch timeout expired for {repo_path} "
                        f"({elapsed:.1f}s > {timeout_seconds}s)",
                        extra={"correlation_id": get_correlation_id()},
                    )
                    repos_to_stop.append(repo_path)

            # Stop expired watches (outside iteration to avoid dict modification during iteration)
            for repo_path in repos_to_stop:
                try:
                    watch_instance = self._watch_state[repo_path]["watch_instance"]
                    watch_instance.stop_watch()
                    del self._watch_state[repo_path]
                    logger.info(
                        f"Auto-stopped watch for {repo_path} due to timeout",
                        extra={"correlation_id": get_correlation_id()},
                    )
                except Exception as e:
                    logger.exception(
                        f"Error auto-stopping watch for {repo_path}: {e}",
                        extra={"correlation_id": get_correlation_id()},
                    )

    def shutdown(self) -> None:
        """
        Shutdown AutoWatchManager and stop background timeout checker thread.

        Should be called when server is shutting down to ensure clean resource cleanup.
        """
        logger.info(
            "Shutting down AutoWatchManager...",
            extra={"correlation_id": get_correlation_id()},
        )

        # Signal background thread to stop
        self._shutdown_event.set()

        # Wait for thread to terminate (with timeout). Bug #1689: the
        # checker thread is now started lazily (may still be None if
        # start_watch() was never called, or was only ever called while
        # auto_watch_enabled=False) -- must not raise AttributeError.
        if self._timeout_thread is not None and self._timeout_thread.is_alive():
            self._timeout_thread.join(timeout=self.SHUTDOWN_THREAD_JOIN_TIMEOUT_SECONDS)
            if self._timeout_thread.is_alive():
                logger.warning(
                    format_error_log(
                        "APP-GENERAL-052",
                        f"Timeout checker thread did not stop within "
                        f"{self.SHUTDOWN_THREAD_JOIN_TIMEOUT_SECONDS} seconds",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
            else:
                logger.info(
                    "Timeout checker thread stopped successfully",
                    extra={"correlation_id": get_correlation_id()},
                )

        # Stop all active watches
        with self._lock:
            repos_to_stop = list(self._watch_state.keys())

        for repo_path in repos_to_stop:
            try:
                self.stop_watch(repo_path)
            except Exception as e:
                logger.exception(
                    f"Error stopping watch during shutdown for {repo_path}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )

        logger.info(
            "AutoWatchManager shutdown complete",
            extra={"correlation_id": get_correlation_id()},
        )

    def get_state(self, repo_path: str) -> Optional[Dict[str, Any]]:
        """
        Get current watch state for repository.

        Args:
            repo_path: Repository path

        Returns:
            Watch state dictionary or None if no watch running
        """
        with self._lock:
            if repo_path not in self._watch_state:
                return None

            state = self._watch_state[repo_path]
            return {
                "watch_running": state.get("watch_running", False),
                "last_activity": state.get("last_activity"),
                "timeout_seconds": state.get("timeout_seconds"),
            }

    def get_all_states(self) -> Dict[str, Dict[str, Any]]:
        """
        Get watch state for all watched repositories.

        Returns:
            Dictionary mapping repository paths to watch state dictionaries.
            Each state dictionary contains:
            - watch_running: bool - Whether watch is currently active
            - last_activity: datetime - Last activity timestamp
            - timeout_seconds: int - Timeout duration in seconds
        """
        with self._lock:
            return {
                path: {
                    "watch_running": state.get("watch_running", False),
                    "last_activity": state.get("last_activity"),
                    "timeout_seconds": state.get("timeout_seconds"),
                }
                for path, state in self._watch_state.items()
            }


# Singleton instance
auto_watch_manager = AutoWatchManager()
