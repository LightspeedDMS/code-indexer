"""Bug #1407 Foundation: TemporalProgressiveMetadata's progress-file
directory must be fsynced after os.replace() (precedent:
id_index_manager.py's save_index()) so a crash immediately after a
flush cannot lose the rename on power-loss.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.code_indexer.services.temporal import temporal_progressive_metadata as tpm
from src.code_indexer.services.temporal.temporal_progressive_metadata import (
    TemporalProgressiveMetadata,
)


class TestTemporalProgressiveMetadataDirectoryFsync(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.temporal_dir = Path(self.temp_dir) / ".code-indexer/index/temporal"
        self.temporal_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_flush_pending_opens_and_fsyncs_directory_after_replace(self):
        """Real call-order spy: os.open(temporal_dir) must happen, and the
        resulting fd must be passed to nfs_safe_fsync, STRICTLY AFTER
        os.replace() has already renamed the tmp file into place."""
        metadata = TemporalProgressiveMetadata(self.temporal_dir)
        metadata.mark_commit_indexed("commit1")

        events: list = []
        real_replace = os.replace
        real_open = os.open
        real_fsync = tpm.nfs_safe_fsync
        dir_fd_holder = {}

        def spy_replace(src, dst):
            events.append(("replace", src, dst))
            return real_replace(src, dst)

        def spy_open(path, flags, *a, **kw):
            fd = real_open(path, flags, *a, **kw)
            if str(path) == str(self.temporal_dir):
                events.append(("open_dir", fd))
                dir_fd_holder["fd"] = fd
            return fd

        def spy_fsync(fd):
            if fd == dir_fd_holder.get("fd"):
                events.append(("fsync_dir", fd))
            return real_fsync(fd)

        with (
            patch("os.replace", side_effect=spy_replace),
            patch("os.open", side_effect=spy_open),
            patch(
                "src.code_indexer.services.temporal.temporal_progressive_metadata.nfs_safe_fsync",
                side_effect=spy_fsync,
            ),
        ):
            metadata.flush_pending()

        event_names = [e[0] for e in events]
        assert "replace" in event_names
        assert "open_dir" in event_names
        assert "fsync_dir" in event_names
        # The directory fsync must come strictly AFTER the replace.
        assert event_names.index("replace") < event_names.index("fsync_dir")

    def test_flush_pending_still_correct_after_dir_fsync_added(self):
        metadata = TemporalProgressiveMetadata(self.temporal_dir)
        metadata.mark_commit_indexed("commit1")
        metadata.mark_commit_indexed("commit2")

        metadata.flush_pending()

        completed = metadata.load_completed()
        assert completed == {"commit1", "commit2"}


if __name__ == "__main__":
    unittest.main()
