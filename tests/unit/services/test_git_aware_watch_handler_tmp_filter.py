"""Tests for GitAwareWatchHandler .tmp_ prefix filtering.

Bug #274: _atomic_write_file() in file_crud_service creates temp files with
prefix=".tmp_" and suffix=f"_{filename}".  e.g. .tmp_abc123_somefile.md

GitAwareWatchHandler has NO temp file filtering at all (unlike SimpleWatchHandler
which at least filters .tmp suffix).  This means every atomic write produces two
watch events: one for the .tmp_ file (which should be ignored) and one for the
real file.  The spurious event causes wasted VoyageAI API calls and can race with
the final exit_write_mode refresh.

Fix: GitAwareWatchHandler._add_pending_change() (or its callers on_created /
on_modified / on_deleted) must skip files whose basename starts with ".tmp_".
"""

from pathlib import Path
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------


def _make_minimal_git_aware_handler():
    """Create a GitAwareWatchHandler with all heavy dependencies mocked.

    We only care about the file-filtering logic, not the full git indexing
    pipeline, so mock SmartIndexer, GitTopologyService, and WatchMetadata.
    """
    from code_indexer.services.git_aware_watch_handler import GitAwareWatchHandler
    from code_indexer.services.watch_metadata import WatchMetadata

    config = MagicMock()
    config.codebase_dir = Path("/fake/repo")
    config.file_extensions = ["md", "py", "txt"]

    smart_indexer = MagicMock()
    git_topology = MagicMock()
    git_topology.get_current_branch.return_value = "main"

    watch_metadata = MagicMock(spec=WatchMetadata)

    handler = GitAwareWatchHandler(
        config=config,
        smart_indexer=smart_indexer,
        git_topology_service=git_topology,
        watch_metadata=watch_metadata,
        debounce_seconds=0.1,
    )
    return handler


def _make_event(src_path: str, is_directory: bool = False):
    """Create a minimal watchdog-style event object."""
    event = MagicMock()
    event.src_path = src_path
    event.is_directory = is_directory
    return event


# ---------------------------------------------------------------------------
# Tests: .tmp_ prefix filtering in GitAwareWatchHandler
# ---------------------------------------------------------------------------


class TestGitAwareWatchHandlerTmpPrefixFiltering:
    """GitAwareWatchHandler must ignore .tmp_ prefixed files.

    Bug #274 Bug 2: GitAwareWatchHandler currently has NO temp file filtering.
    These tests will FAIL before the fix is applied.
    """

    def test_on_created_ignores_tmp_prefix_file(self):
        """on_created: .tmp_ prefixed file must NOT be added to pending_changes."""
        handler = _make_minimal_git_aware_handler()

        event = _make_event("/fake/repo/.tmp_abc123_somefile.md")
        handler.on_created(event)

        assert len(handler.pending_changes) == 0, (
            f"pending_changes must be empty after .tmp_ file event. "
            f"Got: {handler.pending_changes}"
        )

    def test_on_modified_ignores_tmp_prefix_file(self):
        """on_modified: .tmp_ prefixed file must NOT be added to pending_changes."""
        handler = _make_minimal_git_aware_handler()

        event = _make_event("/fake/repo/.tmp_XYZ99_README.md")
        handler.on_modified(event)

        assert len(handler.pending_changes) == 0, (
            f"pending_changes must be empty after .tmp_ modified event. "
            f"Got: {handler.pending_changes}"
        )

    def test_on_deleted_ignores_tmp_prefix_file(self):
        """on_deleted: .tmp_ prefixed file must NOT be added to pending_changes."""
        handler = _make_minimal_git_aware_handler()

        event = _make_event("/fake/repo/.tmp_DEL42_file.py")
        handler.on_deleted(event)

        assert len(handler.pending_changes) == 0, (
            f"pending_changes must be empty after .tmp_ deleted event. "
            f"Got: {handler.pending_changes}"
        )

    def test_on_created_accepts_regular_file(self):
        """on_created: regular files (no .tmp_ prefix) must still be accepted.

        Patches _should_include_file to return True so the file passes all checks.
        """
        handler = _make_minimal_git_aware_handler()
        handler._should_include_file = lambda p: True

        event = _make_event("/fake/repo/src/real_file.md")
        handler.on_created(event)

        assert len(handler.pending_changes) == 1, (
            f"regular file must be added to pending_changes. "
            f"Got: {handler.pending_changes}"
        )

    def test_on_modified_accepts_regular_file(self):
        """on_modified: regular files must still be accepted."""
        handler = _make_minimal_git_aware_handler()
        handler._should_include_file = lambda p: True

        event = _make_event("/fake/repo/src/regular.py")
        handler.on_modified(event)

        assert len(handler.pending_changes) == 1, (
            f"regular .py file must be added to pending_changes. "
            f"Got: {handler.pending_changes}"
        )

    def test_tmp_prefix_md_file_ignored_not_just_tmp_suffix(self):
        """.tmp_ prefixed .md file must be ignored even though .md is a valid extension.

        This is the key regression: Path(".tmp_abc_file.md").suffix == ".md"
        which passes extension checks.  The prefix filter must catch it first.
        """
        handler = _make_minimal_git_aware_handler()
        # Even if inclusion checks would accept .md extension:
        handler._should_include_deleted_file = lambda p: True
        handler._should_include_file = lambda p: True

        event = _make_event("/fake/repo/.tmp_abc123_important.md")
        handler.on_created(event)

        assert len(handler.pending_changes) == 0, (
            ".tmp_ .md file must be ignored despite .md being a valid extension"
        )

    def test_directory_events_still_ignored(self):
        """Directory events (existing behavior) must still be ignored."""
        handler = _make_minimal_git_aware_handler()

        dir_event = _make_event("/fake/repo/src/", is_directory=True)
        handler.on_created(dir_event)
        handler.on_modified(dir_event)
        handler.on_deleted(dir_event)

        assert len(handler.pending_changes) == 0, (
            "Directory events must always be ignored"
        )
