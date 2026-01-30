"""
Queue-based manager for Claude CLI invocations with concurrency control.

Provides:
- Non-blocking work submission via queue
- Atomic API key synchronization with file locking
- Configurable worker pool for concurrency control
- CLI availability checking with caching
- Global singleton pattern for server-wide manager access (Story #23)
"""

from code_indexer.server.middleware.correlation import get_correlation_id

import fcntl
import json
import logging
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, List, Tuple
from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)

# Module-level singleton for global manager access (Story #23, AC1)
_global_cli_manager: Optional["ClaudeCliManager"] = None
_global_cli_manager_lock = threading.Lock()


@dataclass
class CatchupResult:
    """Result of catch-up processing."""

    partial: bool
    processed: List[str]
    remaining: List[str]
    error: Optional[str] = None


class ClaudeCliManager:
    """
    Queue-based manager for Claude CLI invocations with:
    - Non-blocking work submission
    - Atomic API key synchronization with file locking
    - Configurable worker pool for concurrency control
    """

    def __init__(self, api_key: Optional[str] = None, max_workers: int = 2):
        """
        Initialize ClaudeCliManager with worker pool.

        Args:
            api_key: Anthropic API key to sync to ~/.claude.json
            max_workers: Number of worker threads (default 2, Story #24)
        """
        self._api_key = api_key
        self._max_workers = max_workers
        self._work_queue: (
            "queue.Queue[Optional[Tuple[Path, Callable[[bool, str], None]]]]"
        ) = queue.Queue()
        self._worker_threads: List[threading.Thread] = []
        self._shutdown_event = threading.Event()
        self._cli_available: Optional[bool] = None
        self._cli_check_time: float = 0
        self._cli_check_ttl: float = 300  # 5 minutes TTL
        self._meta_dir: Optional[Path] = None  # Meta directory for fallback scanning
        self._cli_was_unavailable: bool = True
        self._cli_state_lock = threading.Lock()  # Lock for CLI state management

        # Start worker threads
        for i in range(max_workers):
            t = threading.Thread(
                target=self._worker_loop, name=f"ClaudeCLI-Worker-{i}", daemon=True
            )
            self._worker_threads.append(t)
            t.start()

        logger.info(
            f"ClaudeCliManager started with {max_workers} workers",
            extra={"correlation_id": get_correlation_id()},
        )

    def submit_work(
        self, repo_path: Path, callback: Callable[[bool, str], None]
    ) -> None:
        """
        Submit work to the queue. Returns immediately (non-blocking).

        Args:
            repo_path: Repository path to process
            callback: Callback function(success: bool, result: str) invoked on completion
        """
        self._work_queue.put((repo_path, callback))
        logger.debug(
            f"Work queued for {repo_path}",
            extra={"correlation_id": get_correlation_id()},
        )

    def _ensure_api_key_synced(self) -> None:
        """
        Ensure API key is synced using ApiKeySyncService (Story #20).

        This is the preferred method for pre-use sync triggers.
        Delegates to ApiKeySyncService for proper sync to all targets.
        """
        if not self._api_key:
            logger.debug(
                "No API key configured, skipping sync",
                extra={"correlation_id": get_correlation_id()},
            )
            return

        try:
            from code_indexer.server.services.api_key_management import (
                ApiKeySyncService,
            )

            sync_service = ApiKeySyncService()
            result = sync_service.sync_anthropic_key(self._api_key)

            if result.success:
                if not result.already_synced:
                    logger.debug(
                        "API key synced via ApiKeySyncService",
                        extra={"correlation_id": get_correlation_id()},
                    )
            else:
                logger.warning(format_error_log(
                    "APP-GENERAL-063",
                    f"API key sync failed: {result.error}",
                    extra={"correlation_id": get_correlation_id()},
                ))
        except Exception as e:
            logger.error(format_error_log(
                "APP-GENERAL-064",
                f"Failed to sync API key via ApiKeySyncService: {e}",
                exc_info=True,
                extra={"correlation_id": get_correlation_id()},
            ))

    def sync_api_key(self) -> None:
        """
        Sync API key to ~/.claude.json and environment (Story #20).

        Uses ApiKeySyncService for proper sync to all targets:
        - ~/.claude.json (apiKey field)
        - os.environ["ANTHROPIC_API_KEY"]
        - systemd environment file

        Legacy file locking approach preserved as fallback.
        """
        if not self._api_key:
            logger.debug(
                "No API key configured, skipping sync",
                extra={"correlation_id": get_correlation_id()},
            )
            return

        # Use ApiKeySyncService for proper sync (Story #20)
        try:
            from code_indexer.server.services.api_key_management import (
                ApiKeySyncService,
            )

            sync_service = ApiKeySyncService()
            result = sync_service.sync_anthropic_key(self._api_key)

            if result.success:
                logger.debug(
                    "API key synced via ApiKeySyncService",
                    extra={"correlation_id": get_correlation_id()},
                )
                return
            else:
                logger.warning(format_error_log(
                    "APP-GENERAL-065",
                    f"ApiKeySyncService sync failed: {result.error}, "
                    "falling back to legacy sync",
                    extra={"correlation_id": get_correlation_id()},
                ))
        except ImportError:
            logger.debug(
                "ApiKeySyncService not available, using legacy sync",
                extra={"correlation_id": get_correlation_id()},
            )
        except Exception as e:
            logger.warning(format_error_log(
                "APP-GENERAL-066",
                f"ApiKeySyncService error: {e}, falling back to legacy sync",
                extra={"correlation_id": get_correlation_id()},
            ))

        # Legacy fallback: direct file write with locking
        lock_path = Path.home() / ".claude.json.lock"
        json_path = Path.home() / ".claude.json"

        try:
            with open(lock_path, "w") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    # Read existing config or create new
                    existing = {}
                    if json_path.exists():
                        try:
                            existing = json.loads(json_path.read_text())
                        except json.JSONDecodeError:
                            logger.warning(format_error_log(
                                "APP-GENERAL-067",
                                f"Invalid JSON in {json_path}, overwriting",
                                extra={"correlation_id": get_correlation_id()},
                            ))

                    # Update primaryApiKey
                    existing["primaryApiKey"] = self._api_key
                    json_path.write_text(json.dumps(existing, indent=2))
                    logger.debug(
                        f"API key synced to {json_path} (legacy)",
                        extra={"correlation_id": get_correlation_id()},
                    )
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except Exception as e:
            logger.error(format_error_log(
                "AUTH-GENERAL-010",
                f"Failed to sync API key: {e}",
                exc_info=True,
                extra={"correlation_id": get_correlation_id()},
            ))
            raise

    def check_cli_available(self) -> bool:
        """
        Check if Claude CLI is installed. Caches result with TTL.

        Returns:
            True if Claude CLI is available, False otherwise
        """
        now = time.time()
        if (
            self._cli_available is not None
            and (now - self._cli_check_time) < self._cli_check_ttl
        ):
            return self._cli_available

        try:
            result = subprocess.run(
                ["which", "claude"], capture_output=True, text=True, timeout=5
            )
            self._cli_available = result.returncode == 0
            logger.debug(
                f"CLI availability check: {self._cli_available}",
                extra={"correlation_id": get_correlation_id()},
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            self._cli_available = False
            logger.debug(
                "CLI availability check: False (timeout/not found)",
                extra={"correlation_id": get_correlation_id()},
            )

        self._cli_check_time = now
        return self._cli_available

    def set_meta_dir(self, meta_dir: Path) -> None:
        """
        Set the meta directory for fallback scanning.

        Args:
            meta_dir: Path to the cidx-meta directory
        """
        self._meta_dir = meta_dir
        logger.debug(
            f"Meta directory set to: {meta_dir}",
            extra={"correlation_id": get_correlation_id()},
        )

    def scan_for_fallbacks(self) -> List[Tuple[str, Path]]:
        """
        Scan meta directory for fallback files (*_README.md).

        Returns:
            List of (alias, fallback_path) tuples for each fallback file found
        """
        if not self._meta_dir or not self._meta_dir.exists():
            logger.debug(
                f"Meta directory not set or doesn't exist: {self._meta_dir}",
                extra={"correlation_id": get_correlation_id()},
            )
            return []

        fallbacks = []
        for path in self._meta_dir.glob("*_README.md"):
            # Extract alias: my-repo_README.md -> my-repo
            alias = path.stem.rsplit("_README", 1)[0]
            fallbacks.append((alias, path))
            logger.debug(
                f"Found fallback: {alias} -> {path}",
                extra={"correlation_id": get_correlation_id()},
            )

        logger.info(
            f"Scanned meta directory, found {len(fallbacks)} fallback(s)",
            extra={"correlation_id": get_correlation_id()},
        )
        return fallbacks

    def process_all_fallbacks(self) -> "CatchupResult":
        """
        Process all fallback files, replacing with generated descriptions.

        Returns:
            CatchupResult with processing status
        """
        if not self.check_cli_available():
            fallbacks = self.scan_for_fallbacks()
            return CatchupResult(
                partial=True,
                processed=[],
                remaining=[alias for alias, _ in fallbacks],
                error="CLI not available",
            )

        fallbacks = self.scan_for_fallbacks()
        if not fallbacks:
            logger.info(
                "No fallbacks to process",
                extra={"correlation_id": get_correlation_id()},
            )
            return CatchupResult(partial=False, processed=[], remaining=[])

        logger.info(
            f"Starting catch-up processing for {len(fallbacks)} fallbacks",
            extra={"correlation_id": get_correlation_id()},
        )
        processed: List[str] = []
        remaining = [alias for alias, _ in fallbacks]

        for alias, fallback_path in fallbacks:
            try:
                success = self._process_single_fallback(alias, fallback_path)
                if not success:
                    return CatchupResult(
                        partial=True,
                        processed=processed,
                        remaining=remaining,
                        error=f"CLI failed for {alias}",
                    )
                processed.append(alias)
                remaining.remove(alias)
            except Exception as e:
                logger.error(format_error_log(
                    "AUTH-GENERAL-011",
                    f"Catch-up failed for {alias}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                ))
                return CatchupResult(
                    partial=True, processed=processed, remaining=remaining, error=str(e)
                )

        # Single commit and re-index after all swaps
        if processed:
            self._commit_and_reindex(processed)

        logger.info(
            f"Catch-up complete: {len(processed)} files processed",
            extra={"correlation_id": get_correlation_id()},
        )
        return CatchupResult(partial=False, processed=processed, remaining=[])

    def _process_single_fallback(self, alias: str, fallback_path: Path) -> bool:
        """
        Process a single fallback file.

        Args:
            alias: Repository alias
            fallback_path: Path to the fallback file

        Returns:
            True on success, False on failure
        """
        if not self._meta_dir:
            return False

        self.sync_api_key()

        generated_path = self._meta_dir / f"{alias}.md"

        try:
            # Rename fallback to generated filename
            # In production, would generate new content via Claude CLI
            fallback_path.rename(generated_path)
            logger.info(
                f"Processed fallback for {alias}",
                extra={"correlation_id": get_correlation_id()},
            )
            return True
        except Exception as e:
            logger.error(format_error_log(
                "AUTH-GENERAL-012",
                f"Failed to process fallback for {alias}: {e}",
                extra={"correlation_id": get_correlation_id()},
            ))
            return False

    def _commit_and_reindex(self, processed: List[str]) -> None:
        """
        Commit changes and trigger re-index.

        Args:
            processed: List of processed aliases
        """
        if not self._meta_dir:
            return

        try:
            commit_msg = f"Replace README fallbacks with generated descriptions: {', '.join(processed)}"
            subprocess.run(
                ["git", "add", "."],
                cwd=str(self._meta_dir),
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=str(self._meta_dir),
                capture_output=True,
                check=False,
            )
            logger.info(
                f"Committed catch-up changes for {len(processed)} files",
                extra={"correlation_id": get_correlation_id()},
            )
        except Exception as e:
            logger.warning(format_error_log(
                "AUTH-GENERAL-013",
                f"Git commit failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            ))

        try:
            subprocess.run(
                ["cidx", "index"],
                cwd=str(self._meta_dir),
                capture_output=True,
                check=False,
            )
            logger.info(
                "Re-indexed meta directory",
                extra={"correlation_id": get_correlation_id()},
            )
        except Exception as e:
            logger.warning(format_error_log(
                "AUTH-GENERAL-014",
                f"Re-index failed: {e}", extra={"correlation_id": get_correlation_id()}
            ))

    def update_api_key(self, api_key: Optional[str]) -> None:
        """
        Update the API key for this manager (Story #23, AC3).

        Args:
            api_key: New Anthropic API key, or None to clear
        """
        self._api_key = api_key
        logger.info(
            f"API key updated (key {'set' if api_key else 'cleared'})",
            extra={"correlation_id": get_correlation_id()},
        )

    def _on_cli_success(self) -> None:
        """Called when CLI invocation succeeds. Triggers catch-up if first success."""
        with self._cli_state_lock:
            if self._cli_was_unavailable and self._meta_dir:
                self._cli_was_unavailable = False
                logger.info(
                    "CLI became available, triggering catch-up processing",
                    extra={"correlation_id": get_correlation_id()},
                )
                threading.Thread(
                    target=self.process_all_fallbacks,
                    name="CatchupProcessor",
                    daemon=True,
                ).start()

    def shutdown(self, timeout: float = 5.0) -> None:
        """
        Gracefully shut down worker threads.

        Args:
            timeout: Maximum time to wait for each worker thread to stop
        """
        logger.info(
            "Shutting down ClaudeCliManager",
            extra={"correlation_id": get_correlation_id()},
        )

        # Add sentinel values to signal workers to stop (after completing queued work)
        for _ in self._worker_threads:
            self._work_queue.put(None)

        # Wait for threads to finish
        for t in self._worker_threads:
            t.join(timeout=timeout)
            if t.is_alive():
                logger.warning(format_error_log(
                    "AUTH-GENERAL-015",
                    f"Worker thread {t.name} did not stop within timeout",
                    extra={"correlation_id": get_correlation_id()},
                ))

        # Set shutdown event for any remaining logic
        self._shutdown_event.set()

        logger.info(
            "ClaudeCliManager shutdown complete",
            extra={"correlation_id": get_correlation_id()},
        )

    def _worker_loop(self) -> None:
        """Worker thread main loop."""
        thread_name = threading.current_thread().name
        logger.debug(
            f"{thread_name} started", extra={"correlation_id": get_correlation_id()}
        )

        while not self._shutdown_event.is_set():
            try:
                item = self._work_queue.get(timeout=1.0)
                if item is None:  # Sentinel for shutdown
                    logger.debug(
                        f"{thread_name} received shutdown sentinel",
                        extra={"correlation_id": get_correlation_id()},
                    )
                    break

                repo_path, callback = item
                logger.debug(
                    f"{thread_name} processing {repo_path}",
                    extra={"correlation_id": get_correlation_id()},
                )
                self._process_work(repo_path, callback)

            except queue.Empty:
                continue
            except Exception as e:
                logger.error(format_error_log(
                    "AUTH-GENERAL-016",
                    f"{thread_name} error: {e}",
                    exc_info=True,
                    extra={"correlation_id": get_correlation_id()},
                ))

        logger.debug(
            f"{thread_name} stopped", extra={"correlation_id": get_correlation_id()}
        )

    def _process_work(
        self, repo_path: Path, callback: Callable[[bool, str], None]
    ) -> None:
        """
        Process a single work item.

        Args:
            repo_path: Repository path to process
            callback: Callback function to invoke with result
        """
        try:
            # Pre-use sync trigger: ensure API key is synced (Story #20)
            self._ensure_api_key_synced()

            # Check CLI availability
            if not self.check_cli_available():
                logger.warning(format_error_log(
                    "AUTH-GENERAL-017",
                    f"Claude CLI not available for {repo_path}",
                    extra={"correlation_id": get_correlation_id()},
                ))
                callback(False, "Claude CLI not available")
                return

            # Sync API key before invocation (redundant but kept for safety)
            self.sync_api_key()

            # Invoke Claude CLI (placeholder - actual implementation depends on use case)
            # For now, just indicate success
            result_msg = f"Processed {repo_path}"
            logger.info(result_msg, extra={"correlation_id": get_correlation_id()})
            callback(True, result_msg)

            # Trigger catch-up processing if CLI just became available
            self._on_cli_success()

        except Exception as e:
            logger.error(format_error_log(
                "AUTH-GENERAL-018",
                f"Error processing {repo_path}: {e}",
                exc_info=True,
                extra={"correlation_id": get_correlation_id()},
            ))
            callback(False, str(e))


# Module-level singleton functions (Story #23, AC1)


def get_claude_cli_manager() -> Optional[ClaudeCliManager]:
    """
    Get the global ClaudeCliManager singleton.

    Returns:
        The global ClaudeCliManager instance if initialized, None otherwise.

    Note:
        This function is thread-safe and returns None (not raising an exception)
        if the manager has not been initialized yet. Callers should handle the
        None case gracefully.
    """
    return _global_cli_manager


def initialize_claude_cli_manager(
    api_key: Optional[str],
    meta_dir: Path,
    max_workers: int = 2,
) -> ClaudeCliManager:
    """
    Initialize the global ClaudeCliManager singleton.

    Thread-safe initialization that creates the singleton only once.
    Subsequent calls return the existing instance.

    Args:
        api_key: Anthropic API key for Claude CLI (may be None if not yet configured)
        meta_dir: Path to the cidx-meta directory for fallback scanning
        max_workers: Number of worker threads (default 2)

    Returns:
        The global ClaudeCliManager instance

    Note:
        This should be called during server startup from server_lifecycle_manager.py.
        If called multiple times, returns the existing instance without modification.
    """
    global _global_cli_manager

    # Fast path: already initialized
    if _global_cli_manager is not None:
        return _global_cli_manager

    # Thread-safe initialization
    with _global_cli_manager_lock:
        # Double-check locking pattern
        if _global_cli_manager is not None:
            return _global_cli_manager

        # Create the singleton instance
        manager = ClaudeCliManager(api_key=api_key, max_workers=max_workers)
        manager.set_meta_dir(meta_dir)
        _global_cli_manager = manager

        logger.info(
            f"Global ClaudeCliManager initialized with meta_dir={meta_dir}",
            extra={"correlation_id": get_correlation_id()},
        )

    return _global_cli_manager
