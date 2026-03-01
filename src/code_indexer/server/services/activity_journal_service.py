"""
Activity Journal Service for Story #329.

Thread-safe service that writes timestamped markdown entries to a file
and supports tail-based reading via byte offset.

Used to provide real-time progress visibility during dependency map analysis.
"""

import logging
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class ActivityJournalService:
    """
    Thread-safe activity journal for tracking analysis progress.

    Writes timestamped entries to a markdown file and supports incremental
    reads via byte offset, enabling live streaming to web clients.

    Entry format: [HH:MM:SS] **{source}** {message}
    """

    def __init__(self) -> None:
        self._journal_path: Optional[Path] = None
        self._lock = threading.Lock()
        self._active = False

    @property
    def journal_path(self) -> Optional[Path]:
        """Return current journal file path, or None if not initialized."""
        return self._journal_path

    @property
    def is_active(self) -> bool:
        """Return True when a journal session is in progress."""
        return self._active

    def init(self, journal_dir: Path) -> Path:
        """
        Initialize a fresh journal session.

        Creates (or truncates) _activity.md in journal_dir, sets active=True,
        and returns the absolute path to the journal file.

        Args:
            journal_dir: Directory in which to create the journal file.
                         Created automatically if it does not exist.

        Returns:
            Absolute path to the journal file.
        """
        journal_dir = Path(journal_dir)
        journal_dir.mkdir(parents=True, exist_ok=True)

        journal_path = journal_dir / "_activity.md"

        with self._lock:
            # Truncate any existing content for a fresh session
            journal_path.write_text("", encoding="utf-8")
            self._journal_path = journal_path.resolve()
            self._active = True

        logger.debug(f"ActivityJournal initialized at {self._journal_path}")
        return self._journal_path

    def log(self, message: str, source: str = "system") -> None:
        """
        Append a timestamped entry to the journal.

        Thread-safe. No-op if the service is not active.

        Args:
            message: Activity description (keep under 120 characters).
            source: Entry source label, default 'system'.
        """
        if not self._active or self._journal_path is None:
            return

        timestamp = datetime.now().strftime("%H:%M:%S")
        entry = f"[{timestamp}] **{source}** {message}\n"

        try:
            with self._lock:
                if not self._active or self._journal_path is None:
                    return
                with open(self._journal_path, "a", encoding="utf-8") as f:
                    f.write(entry)
        except Exception as e:
            logger.debug(f"ActivityJournal log failed (non-fatal): {e}")

    def get_content(self, offset: int = 0) -> Tuple[str, int]:
        """
        Read journal content starting from byte offset.

        Thread-safe. Returns an empty string and offset=0 when not active.

        Args:
            offset: Byte position to start reading from (0 = full content).

        Returns:
            Tuple of (new_content, new_offset). new_content is the text
            appended since offset; new_offset is the updated byte position.
        """
        if not self._active or self._journal_path is None:
            return "", 0

        try:
            with self._lock:
                if not self._active or self._journal_path is None:
                    return "", 0

                file_size = self._journal_path.stat().st_size
                if file_size <= offset:
                    return "", offset

                with open(self._journal_path, "rb") as f:
                    f.seek(offset)
                    raw = f.read()

                content = raw.decode("utf-8", errors="replace")
                return content, file_size

        except Exception as e:
            logger.debug(f"ActivityJournal get_content failed (non-fatal): {e}")
            return "", offset

    def clear(self) -> None:
        """
        Truncate the journal and deactivate the service.

        Thread-safe. No-op if the service was never activated.
        """
        with self._lock:
            self._active = False
            if self._journal_path is not None:
                try:
                    self._journal_path.write_text("", encoding="utf-8")
                except Exception as e:
                    logger.debug(f"ActivityJournal clear failed (non-fatal): {e}")
            self._journal_path = None

    def copy_to_final(self, final_dir: Path) -> None:
        """
        Copy the current journal file to a final output directory.

        Thread-safe. No-op if the service is not active or has no journal.

        Args:
            final_dir: Target directory. Created automatically if it does not exist.
        """
        if not self._active or self._journal_path is None:
            return

        try:
            final_dir = Path(final_dir)
            final_dir.mkdir(parents=True, exist_ok=True)
            dest = final_dir / "_activity.md"
            with self._lock:
                if self._journal_path is not None and self._journal_path.exists():
                    shutil.copy2(self._journal_path, dest)
            logger.debug(f"ActivityJournal copied to {dest}")
        except Exception as e:
            logger.debug(f"ActivityJournal copy_to_final failed (non-fatal): {e}")
