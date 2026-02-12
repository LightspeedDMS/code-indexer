"""
Simple watch handler for non-git folders.

Monitors file system changes and triggers indexing callbacks with debouncing
and automatic idle timeout. No git dependency required.
"""

import logging
import time
import threading
from pathlib import Path
from typing import Set, Dict, Any, Optional, Callable, List
from watchdog.events import FileSystemEventHandler, FileSystemEvent

logger = logging.getLogger(__name__)


class SimpleWatchHandler(FileSystemEventHandler):
    """Simple file system event handler for non-git folders."""

    def __init__(
        self,
        folder_path: str,
        indexing_callback: Callable[[List[str], str], None],
        debounce_seconds: float = 1.0,
        idle_timeout_seconds: float = 300.0,
        additional_handlers: Optional[List[Any]] = None,
    ):
        """Initialize simple watch handler.

        Args:
            folder_path: Path to folder to watch
            indexing_callback: Callback function(changed_files, event_type)
            debounce_seconds: Time to wait before processing accumulated changes
            idle_timeout_seconds: Auto-stop after this many seconds of inactivity
            additional_handlers: Optional list of additional FileSystemEventHandler instances to schedule
        """
        super().__init__()
        self.folder_path = Path(folder_path)
        self.indexing_callback = indexing_callback
        self.debounce_seconds = debounce_seconds
        self.idle_timeout_seconds = idle_timeout_seconds
        self.additional_handlers = additional_handlers or []
        # Thread-safe change tracking
        self.pending_changes: Set[Path] = set()
        self.change_lock = threading.Lock()
        self.event_types: Dict[Path, str] = {}
        # Processing state
        self.observer: Optional[Any] = None
        self.processing_thread: Optional[threading.Thread] = None
        self._stop_processing = threading.Event()
        self._processing_in_progress = False
        # Idle timeout tracking
        self.last_activity_time = time.time()
        self.activity_lock = threading.Lock()
        # Statistics
        self.files_processed_count = 0
        self.indexing_cycles_count = 0

    def start_watching(self) -> None:
        """Start watching the folder."""
        if self.is_watching():
            logger.warning(
                "Handler is already watching - ignoring start_watching() call"
            )
            return
        logger.info(f"Starting simple watch handler for {self.folder_path}")
        self._stop_processing.clear()
        with self.activity_lock:
            self.last_activity_time = time.time()
        from watchdog.observers import Observer

        self.observer = Observer()
        self.observer.schedule(self, str(self.folder_path), recursive=True)

        # Schedule additional handlers
        for handler in self.additional_handlers:
            self.observer.schedule(handler, str(self.folder_path), recursive=True)
            logger.debug(f"Scheduled additional handler: {type(handler).__name__}")

        self.observer.start()
        logger.info(f"File system observer started for {self.folder_path}")
        self.processing_thread = threading.Thread(
            target=self._process_changes_loop, daemon=True
        )
        self.processing_thread.start()
        logger.info("Simple watch handler started successfully")

    def stop_watching(self) -> None:
        """Stop watching the folder."""
        logger.info("Stopping simple watch handler")
        self._stop_processing.set()
        if self.observer and self.observer.is_alive():
            self.observer.stop()
            self.observer.join(timeout=5.0)
            logger.info("File system observer stopped")
        if self.processing_thread and self.processing_thread.is_alive():
            self.processing_thread.join(timeout=5.0)
            if self.processing_thread.is_alive():
                logger.warning(
                    "Processing thread did not stop within timeout - skipping final processing"
                )
            else:
                logger.info("Processing thread stopped")
                self._process_pending_changes()
        else:
            self._process_pending_changes()
        logger.info("Simple watch handler stopped")

    def is_watching(self) -> bool:
        """Check if actively watching."""
        return self.processing_thread is not None and self.processing_thread.is_alive()

    def on_created(self, event: FileSystemEvent) -> None:
        """Handle file creation events."""
        if event.is_directory:
            return
        file_path = Path(str(event.src_path))
        if not self._should_ignore_file(file_path):
            self._add_pending_change(file_path, "created")

    def on_modified(self, event: FileSystemEvent) -> None:
        """Handle file modification events."""
        if event.is_directory:
            return
        file_path = Path(str(event.src_path))
        if not self._should_ignore_file(file_path):
            self._add_pending_change(file_path, "modified")

    def on_deleted(self, event: FileSystemEvent) -> None:
        """Handle file deletion events."""
        if event.is_directory:
            return
        file_path = Path(str(event.src_path))
        if not self._should_ignore_file(file_path):
            self._add_pending_change(file_path, "deleted")

    def on_moved(self, event: FileSystemEvent) -> None:
        """Handle file move events."""
        if event.is_directory:
            return
        old_path = Path(str(event.src_path))
        new_path = Path(str(event.dest_path))
        if not self._should_ignore_file(old_path):
            self._add_pending_change(old_path, "deleted")
        if not self._should_ignore_file(new_path):
            self._add_pending_change(new_path, "created")

    def _should_ignore_file(self, file_path: Path) -> bool:
        """Check if file should be ignored."""
        file_str = str(file_path)
        ignore_file_patterns = [".tmp", ".swp", ".pyc", "~"]
        for pattern in ignore_file_patterns:
            if file_str.endswith(pattern):
                return True
        ignore_dir_patterns = [".git/", ".svn/", "__pycache__/", ".code-indexer/"]
        for pattern in ignore_dir_patterns:
            if pattern in file_str:
                return True
        return False

    def _add_pending_change(self, file_path: Path, event_type: str) -> None:
        """Add a file change to pending queue."""
        try:
            with self.change_lock:
                self.pending_changes.add(file_path)
                self.event_types[file_path] = event_type
                logger.debug(f"Added pending change: {event_type} {file_path}")
            with self.activity_lock:
                self.last_activity_time = time.time()
        except Exception as e:
            logger.warning(f"Failed to add pending change for {file_path}: {e}")

    def _process_changes_loop(self) -> None:
        """Main loop for processing file changes with debouncing and idle timeout."""
        while not self._stop_processing.is_set():
            try:
                with self.activity_lock:
                    idle_duration = time.time() - self.last_activity_time
                if idle_duration >= self.idle_timeout_seconds:
                    logger.info(
                        f"Idle timeout reached ({idle_duration:.1f}s >= {self.idle_timeout_seconds}s), stopping"
                    )
                    self._stop_processing.set()
                    if self.observer and self.observer.is_alive():
                        self.observer.stop()
                        self.observer.join(timeout=5.0)
                    break
                if self._stop_processing.wait(timeout=self.debounce_seconds):
                    break
                self._process_pending_changes()
            except Exception as e:
                logger.error(f"Error in change processing loop: {e}")
                if self._stop_processing.wait(timeout=5):
                    break

    def _process_pending_changes(self) -> None:
        """Process all pending file changes."""
        if self._processing_in_progress:
            logger.debug("Processing already in progress - skipping")
            return
        self._processing_in_progress = True
        try:
            with self.change_lock:
                if not self.pending_changes:
                    return
                changes_to_process = self.pending_changes.copy()
                event_types_to_process = self.event_types.copy()
                self.pending_changes.clear()
                self.event_types.clear()
            if not changes_to_process:
                return
            successfully_processed: Set[Path] = set()
            try:
                files_by_type: Dict[str, List[str]] = {}
                for file_path in changes_to_process:
                    event_type = event_types_to_process.get(file_path, "modified")
                    if event_type not in files_by_type:
                        files_by_type[event_type] = []
                    files_by_type[event_type].append(str(file_path))
                for event_type, files in files_by_type.items():
                    logger.info(f"Processing {len(files)} {event_type} file(s)")
                    try:
                        self.indexing_callback(files, event_type)
                        for file_str in files:
                            successfully_processed.add(Path(file_str))
                        self.files_processed_count += len(files)
                    except Exception as e:
                        logger.error(f"Failed to process {event_type} files: {e}")
                self.indexing_cycles_count += 1
            except Exception as e:
                logger.error(f"Failed to process file changes: {e}")
            failed_changes = changes_to_process - successfully_processed
            if failed_changes:
                logger.info(f"Re-queuing {len(failed_changes)} failed changes")
                with self.change_lock:
                    for file_path in failed_changes:
                        self.pending_changes.add(file_path)
                        if file_path in event_types_to_process:
                            self.event_types[file_path] = event_types_to_process[
                                file_path
                            ]
        finally:
            self._processing_in_progress = False

    def get_stats(self) -> Dict[str, Any]:
        """Get watch statistics."""
        with self.change_lock:
            return {
                "files_processed": self.files_processed_count,
                "indexing_cycles": self.indexing_cycles_count,
                "pending_changes": len(self.pending_changes),
                "current_branch": None,
            }
