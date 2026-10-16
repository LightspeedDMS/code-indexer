"""
FTS Watch Handler for real-time FTS index maintenance.

Monitors file system changes and updates the Tantivy FTS index incrementally
alongside the semantic index in watch mode.

Bug #2056: every change goes through the SAME code normal indexing uses
(fts_file_documents): a changed file's documents are replaced by one per
current chunk under its repo-relative path; a deleted (or blanked) file
has none. No document is built here.
"""

import logging
from pathlib import Path
from watchdog.events import FileSystemEventHandler

from .fts_file_documents import FileFtsDocuments
from .tantivy_index_manager import TantivyIndexManager

logger = logging.getLogger(__name__)


class FTSWatchHandler(FileSystemEventHandler):
    """File system event handler for FTS index maintenance in watch mode."""

    def __init__(
        self,
        tantivy_index_manager: TantivyIndexManager,
        config,
    ):
        """
        Initialize FTS watch handler.

        Args:
            tantivy_index_manager: TantivyIndexManager instance for FTS operations
            config: Application configuration
        """
        super().__init__()
        self.tantivy_manager = tantivy_index_manager
        self.config = config
        self._documents = FileFtsDocuments(config)

        # Statistics
        self.files_updated_count = 0
        self.files_deleted_count = 0

    def _replace_and_commit(self, file_path: Path) -> None:
        """Replace the file's FTS documents with its current chunks (none
        when it no longer exists) and make the change visible. ANY failure
        -- reading or chunking the file, writing its documents, committing
        -- may leave the index without the file's current content, so it
        drops the index's content marker (the next `cidx index --fts`
        rebuilds it from disk) before propagating."""
        from .fts_file_documents import invalidate_fts_content_marker
        from .fts_lifecycle import fts_index_dir

        try:
            self._documents.replace_in_index(self.tantivy_manager, file_path)
            self.tantivy_manager.commit()
        except Exception:
            invalidate_fts_content_marker(fts_index_dir(self.config))
            raise

    def on_modified(self, event):
        """Handle file modification events."""
        if event.is_directory:
            return

        file_path = Path(event.src_path)

        # Check if file should be indexed
        if not self._should_include_file(file_path):
            return

        try:
            self._replace_and_commit(file_path)
            self.files_updated_count += 1
            logger.debug(f"Updated FTS index for: {file_path}")

        except Exception as e:
            logger.warning(f"Failed to update FTS index for {file_path}: {e}")

    def on_deleted(self, event):
        """Handle file deletion events."""
        if event.is_directory:
            return

        file_path = Path(event.src_path)

        # Check if file extension would have been indexed
        if not self._should_include_deleted_file(file_path):
            return

        try:
            self._replace_and_commit(file_path)
            self.files_deleted_count += 1
            logger.debug(f"Deleted from FTS index: {file_path}")

        except Exception as e:
            logger.warning(f"Failed to delete from FTS index {file_path}: {e}")

    def on_created(self, event):
        """Handle file creation events (same as modification)."""
        if event.is_directory:
            return

        # Treat creation same as modification
        self.on_modified(event)

    def on_moved(self, event):
        """Handle file move events."""
        if event.is_directory:
            return

        # Treat move as delete old + create new
        old_path = Path(event.src_path)

        # Delete old path
        if self._should_include_deleted_file(old_path):
            try:
                self._replace_and_commit(old_path)
                self.files_deleted_count += 1
            except Exception as e:
                logger.warning(
                    f"Failed to delete old path from FTS index {old_path}: {e}"
                )

        # Add new path (will be handled by on_created via watchdog)
        # Note: watchdog fires both on_moved and on_created for destination

    def _should_include_file(self, file_path: Path) -> bool:
        """Check if file should be included in FTS indexing."""
        try:
            # Use the same logic as regular indexing
            from ..indexing import FileFinder

            file_finder = FileFinder(self.config)
            return file_finder._should_include_file(file_path)
        except Exception as e:
            logger.warning(f"Failed to check if file should be included: {e}")
            return False

    def _should_include_deleted_file(self, file_path: Path) -> bool:
        """Check if a deleted file would have been included in indexing."""
        try:
            # For deleted files, just check the file extension
            extension = file_path.suffix.lstrip(".")
            return extension in self.config.file_extensions
        except Exception as e:
            logger.warning(f"Failed to check if deleted file should be included: {e}")
            return False

    def get_statistics(self) -> dict:
        """Get FTS watch handler statistics."""
        return {
            "fts_files_updated": self.files_updated_count,
            "fts_files_deleted": self.files_deleted_count,
        }
