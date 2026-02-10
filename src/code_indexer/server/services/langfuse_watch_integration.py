"""
LangfuseWatchIntegration - Auto-start and status management for Langfuse folders.

Manages automatic watch activation for Langfuse trace folders when files are written.
Note: Indexing logic is handled by DaemonWatchManager's inline callback, not this class.
"""

import logging
import threading
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# Default idle timeout for Langfuse folder watches (5 minutes)
DEFAULT_LANGFUSE_WATCH_IDLE_TIMEOUT_SECONDS = 300


class LangfuseWatchIntegration:
    """
    Integration service for auto-starting watches on Langfuse trace folders.

    Responsibilities:
    - Auto-start watching when files are written to Langfuse folders
    - Manage watch lifecycle via AutoWatchManager
    - Provide status reporting for watched folders

    Note: This class does NOT handle indexing callbacks. The actual indexing
    logic is implemented as an inline closure in DaemonWatchManager._create_watch_handler()
    which bridges SimpleWatchHandler file events to SmartIndexer.
    """

    def __init__(self, auto_watch_manager):
        """
        Initialize Langfuse watch integration.

        Args:
            auto_watch_manager: AutoWatchManager instance for managing watch lifecycle
        """
        self.auto_watch_manager = auto_watch_manager
        self._lock = threading.RLock()  # Thread-safe operations

    def on_file_written(self, folder_path: Path) -> None:
        """
        Notification that a file was written to a folder.

        Auto-starts watching if not already watching.

        Args:
            folder_path: Path to the folder where file was written
        """
        with self._lock:
            # Check if already watching
            if self.auto_watch_manager.is_watching(str(folder_path)):
                # Reset timeout to keep watch active
                self.auto_watch_manager.reset_timeout(str(folder_path))
                logger.debug(f"Watch timeout reset for {folder_path}")
            else:
                # Start watching
                result = self.auto_watch_manager.start_watch(
                    repo_path=str(folder_path),
                    timeout=DEFAULT_LANGFUSE_WATCH_IDLE_TIMEOUT_SECONDS,
                )
                if result.get("status") == "success":
                    logger.info(f"Auto-started watching for {folder_path}")
                else:
                    logger.warning(
                        f"Failed to start watch for {folder_path}: {result.get('message')}"
                    )

    def has_git_directory(self, folder_path: Path) -> bool:
        """
        Check if folder contains a .git directory.

        Args:
            folder_path: Path to check

        Returns:
            True if folder contains .git directory, False otherwise
        """
        git_dir = folder_path / ".git"
        return git_dir.exists()

    def is_langfuse_folder(self, folder_path: Path) -> bool:
        """
        Check if folder is a Langfuse trace folder.

        Args:
            folder_path: Path to check

        Returns:
            True if folder name starts with "langfuse_"
        """
        return folder_path.name.startswith("langfuse_")

    def get_watch_status(self, folder_path: Path) -> Optional[Dict[str, Any]]:
        """
        Get watch status for a folder.

        Args:
            folder_path: Path to folder

        Returns:
            Watch status dictionary or None if not watching
        """
        return self.auto_watch_manager.get_state(str(folder_path))

    def get_all_watch_statuses(self) -> Dict[str, Dict[str, Any]]:
        """
        Get watch status for all watched folders.

        Returns:
            Dictionary mapping folder paths to watch status dictionaries
        """
        return self.auto_watch_manager.get_all_states()
