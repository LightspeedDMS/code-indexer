"""
Unit tests for Bug #241: mtime truncation in RefreshScheduler._has_local_changes().

Before the fix, `int(max_mtime) > latest_timestamp` truncated sub-second precision,
causing files modified fractions-of-a-second after the version snapshot to appear
unchanged.

After the fix: `max_mtime > latest_timestamp` uses float comparison, preserving
sub-second precision correctly.
"""

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager


# ---------------------------------------------------------------------------
# Fixtures (same pattern as test_refresh_scheduler_mtime.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_golden_repos_dir():
    """Create temporary golden repos directory."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def mock_registry():
    """Create a mock registry."""
    registry = MagicMock()
    registry.list_global_repos.return_value = []
    registry.get_global_repo.return_value = None
    return registry


@pytest.fixture
def scheduler(temp_golden_repos_dir, mock_registry):
    """Create a RefreshScheduler with injected mock registry."""
    config_source = MagicMock()
    config_source.get_global_refresh_interval.return_value = 3600
    return RefreshScheduler(
        golden_repos_dir=temp_golden_repos_dir,
        config_source=config_source,
        query_tracker=MagicMock(spec=QueryTracker),
        cleanup_manager=MagicMock(spec=CleanupManager),
        registry=mock_registry,
    )


# ---------------------------------------------------------------------------
# Bug #241: sub-second mtime precision tests
# ---------------------------------------------------------------------------


class TestMtimeSubsecondPrecision:
    """
    Verify that _has_local_changes() correctly handles float mtime vs int
    version timestamp comparisons without int() truncation.

    With the old buggy code `int(max_mtime) > latest_timestamp`:
    - int(1234.5) == 1234, so 1234 > 1234 is False  (BUG: change missed!)
    - int(1234.999) == 1234, so 1234 > 1234 is False (BUG: change missed!)

    With the fixed code `max_mtime > latest_timestamp`:
    - 1234.5 > 1234 is True   (CORRECT: sub-second change detected)
    - 1234.999 > 1234 is True  (CORRECT: sub-second change detected)
    """

    def test_subsecond_mtime_detected(self, scheduler, temp_golden_repos_dir):
        """
        max_mtime=1234.5, latest_timestamp=1234 → has_changes=True.

        The old int() truncation would give int(1234.5)=1234, making
        1234 > 1234 → False (missing a real change). The fix preserves
        the float: 1234.5 > 1234 → True.
        """
        source_path = Path(temp_golden_repos_dir) / "test-repo"
        source_path.mkdir(parents=True, exist_ok=True)

        # Version timestamp 1234
        versioned_dir = (
            Path(temp_golden_repos_dir) / ".versioned" / "test-repo" / "v_1234"
        )
        versioned_dir.mkdir(parents=True, exist_ok=True)

        # File with mtime 1234.5 (0.5s after the version snapshot)
        test_file = source_path / "file.md"
        test_file.write_text("# content")
        os.utime(test_file, (1234.5, 1234.5))

        result = scheduler._has_local_changes(str(source_path), "test-repo-global")

        assert result is True, (
            "max_mtime=1234.5 > latest_timestamp=1234 must be True. "
            "The old int() truncation would give int(1234.5)=1234 → 1234>1234=False (BUG)."
        )

    def test_same_second_mtime_detected(self, scheduler, temp_golden_repos_dir):
        """
        max_mtime=1234.999, latest_timestamp=1234 → has_changes=True.

        A file modified 999ms after the snapshot (within the same second)
        must be detected as a change.
        """
        source_path = Path(temp_golden_repos_dir) / "test-repo"
        source_path.mkdir(parents=True, exist_ok=True)

        # Version timestamp 1234
        versioned_dir = (
            Path(temp_golden_repos_dir) / ".versioned" / "test-repo" / "v_1234"
        )
        versioned_dir.mkdir(parents=True, exist_ok=True)

        # File with mtime 1234.999 (999ms into second 1234)
        test_file = source_path / "file.md"
        test_file.write_text("# content")
        os.utime(test_file, (1234.999, 1234.999))

        result = scheduler._has_local_changes(str(source_path), "test-repo-global")

        assert result is True, (
            "max_mtime=1234.999 > latest_timestamp=1234 must be True. "
            "The old int() truncation would give int(1234.999)=1234 → 1234>1234=False (BUG)."
        )

    def test_equal_mtime_no_change(self, scheduler, temp_golden_repos_dir):
        """
        max_mtime=1234.0, latest_timestamp=1234 → has_changes=False.

        A file with exactly the same mtime as the version snapshot should
        NOT be treated as a change (1234.0 > 1234 is False).
        """
        source_path = Path(temp_golden_repos_dir) / "test-repo"
        source_path.mkdir(parents=True, exist_ok=True)

        # Version timestamp 1234
        versioned_dir = (
            Path(temp_golden_repos_dir) / ".versioned" / "test-repo" / "v_1234"
        )
        versioned_dir.mkdir(parents=True, exist_ok=True)

        # File with mtime exactly 1234.0 (same moment as snapshot)
        test_file = source_path / "file.md"
        test_file.write_text("# content")
        os.utime(test_file, (1234.0, 1234.0))

        result = scheduler._has_local_changes(str(source_path), "test-repo-global")

        assert result is False, (
            "max_mtime=1234.0 is NOT > latest_timestamp=1234 (equal). "
            "File at exact snapshot time should NOT be flagged as changed."
        )

    def test_older_mtime_no_change(self, scheduler, temp_golden_repos_dir):
        """
        max_mtime=1233.5, latest_timestamp=1234 → has_changes=False.

        A file modified before the snapshot should clearly not be a change.
        """
        source_path = Path(temp_golden_repos_dir) / "test-repo"
        source_path.mkdir(parents=True, exist_ok=True)

        # Version timestamp 1234
        versioned_dir = (
            Path(temp_golden_repos_dir) / ".versioned" / "test-repo" / "v_1234"
        )
        versioned_dir.mkdir(parents=True, exist_ok=True)

        # File with mtime 1233.5 (0.5s BEFORE the version snapshot)
        test_file = source_path / "file.md"
        test_file.write_text("# content")
        os.utime(test_file, (1233.5, 1233.5))

        result = scheduler._has_local_changes(str(source_path), "test-repo-global")

        assert result is False, (
            "max_mtime=1233.5 < latest_timestamp=1234 must be False. "
            "File is older than the snapshot, no change."
        )
