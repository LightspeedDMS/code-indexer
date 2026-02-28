"""
Unit tests for ActivityJournalService (Story #329).

Tests all journal operations:
- init: creates fresh journal file, clears existing content, sets active state
- log: writes timestamped entries, no-op when not active
- get_content: reads from byte offset, returns incremental content
- clear: truncates journal, deactivates
- copy_to_final: copies journal to output directory
- thread_safety: concurrent writes do not corrupt the journal
"""

import threading
from pathlib import Path


from code_indexer.server.services.activity_journal_service import ActivityJournalService


class TestInit:
    """init() creates a fresh journal and activates the service."""

    def test_init_creates_journal_file(self, tmp_path):
        """init() creates _activity.md inside the given directory."""
        journal = ActivityJournalService()
        result_path = journal.init(tmp_path)

        assert result_path.exists()
        assert result_path.name == "_activity.md"
        assert result_path.parent == tmp_path

    def test_init_returns_absolute_path(self, tmp_path):
        """init() returns the absolute path to the journal file."""
        journal = ActivityJournalService()
        result_path = journal.init(tmp_path)

        assert result_path.is_absolute()

    def test_init_sets_active_true(self, tmp_path):
        """init() activates the service."""
        journal = ActivityJournalService()
        assert not journal.is_active  # not active before init

        journal.init(tmp_path)
        assert journal.is_active

    def test_init_clears_previous_journal(self, tmp_path):
        """init() on a directory that already has _activity.md truncates it."""
        journal = ActivityJournalService()

        # First session - write some content
        journal.init(tmp_path)
        journal.log("First session entry")
        content_before, _ = journal.get_content(0)
        assert "First session entry" in content_before

        # Second session - should clear previous content
        journal.init(tmp_path)
        content_after, _ = journal.get_content(0)
        assert content_after == ""

    def test_init_creates_parent_directory_if_needed(self, tmp_path):
        """init() creates the journal directory if it doesn't exist."""
        journal_dir = tmp_path / "subdir" / "journal"
        journal = ActivityJournalService()
        result_path = journal.init(journal_dir)

        assert journal_dir.exists()
        assert result_path.exists()

    def test_journal_path_property_returns_none_before_init(self):
        """journal_path property returns None before any init() call."""
        journal = ActivityJournalService()
        assert journal.journal_path is None

    def test_journal_path_property_returns_path_after_init(self, tmp_path):
        """journal_path property returns Path after init() call."""
        journal = ActivityJournalService()
        journal.init(tmp_path)
        assert journal.journal_path is not None
        assert isinstance(journal.journal_path, Path)


class TestLog:
    """log() writes timestamped entries to the journal file."""

    def test_log_writes_timestamped_entry(self, tmp_path):
        """log() writes an entry with [HH:MM:SS] timestamp prefix."""
        journal = ActivityJournalService()
        journal.init(tmp_path)
        journal.log("Starting analysis")

        content = journal.journal_path.read_text()
        # Check format: [HH:MM:SS] **system** Starting analysis
        assert "[" in content
        assert "] **system** Starting analysis" in content

    def test_log_uses_default_source_system(self, tmp_path):
        """log() uses 'system' as default source."""
        journal = ActivityJournalService()
        journal.init(tmp_path)
        journal.log("Test message")

        content = journal.journal_path.read_text()
        assert "**system**" in content

    def test_log_uses_custom_source(self, tmp_path):
        """log() accepts a custom source parameter."""
        journal = ActivityJournalService()
        journal.init(tmp_path)
        journal.log("Test message", source="claude")

        content = journal.journal_path.read_text()
        assert "**claude**" in content

    def test_log_appends_newline(self, tmp_path):
        """log() appends a newline after each entry."""
        journal = ActivityJournalService()
        journal.init(tmp_path)
        journal.log("Entry one")
        journal.log("Entry two")

        content = journal.journal_path.read_text()
        lines = content.split("\n")
        # Two entries + trailing empty line from final newline
        assert len([line for line in lines if line.strip()]) == 2

    def test_log_noop_when_not_active(self, tmp_path):
        """log() does nothing when service is not active."""
        journal = ActivityJournalService()
        # Never called init(), so not active
        journal.log("This should not be written")

        # Should not raise and journal_path is None
        assert journal.journal_path is None

    def test_log_noop_after_clear(self, tmp_path):
        """log() does nothing after clear() is called."""
        journal = ActivityJournalService()
        journal.init(tmp_path)
        journal.clear()

        journal.log("Post-clear message")

        # File may still exist but is cleared, and no new content after clear
        assert not journal.is_active


class TestGetContent:
    """get_content() reads journal from a byte offset."""

    def test_get_content_returns_full_content_from_zero(self, tmp_path):
        """get_content(0) returns the full journal content."""
        journal = ActivityJournalService()
        journal.init(tmp_path)
        journal.log("First entry")
        journal.log("Second entry")

        content, new_offset = journal.get_content(0)
        assert "First entry" in content
        assert "Second entry" in content
        assert new_offset > 0

    def test_get_content_returns_incremental_from_offset(self, tmp_path):
        """get_content(offset) returns only new content since that offset."""
        journal = ActivityJournalService()
        journal.init(tmp_path)
        journal.log("First entry")

        _, offset_after_first = journal.get_content(0)

        journal.log("Second entry")
        content, new_offset = journal.get_content(offset_after_first)

        assert "First entry" not in content
        assert "Second entry" in content
        assert new_offset > offset_after_first

    def test_get_content_returns_empty_when_no_new_content(self, tmp_path):
        """get_content(offset) returns empty string when nothing new was written."""
        journal = ActivityJournalService()
        journal.init(tmp_path)
        journal.log("Only entry")

        _, offset = journal.get_content(0)
        content, same_offset = journal.get_content(offset)

        assert content == ""
        assert same_offset == offset

    def test_get_content_returns_empty_when_not_active(self, tmp_path):
        """get_content() returns empty string and zero offset when not active."""
        journal = ActivityJournalService()
        # No init() called

        content, offset = journal.get_content(0)
        assert content == ""
        assert offset == 0

    def test_get_content_offset_advances_correctly(self, tmp_path):
        """get_content() offset tracks byte position correctly across multiple calls."""
        journal = ActivityJournalService()
        journal.init(tmp_path)

        journal.log("Entry A")
        _, offset1 = journal.get_content(0)

        journal.log("Entry B")
        _, offset2 = journal.get_content(offset1)

        journal.log("Entry C")
        content3, offset3 = journal.get_content(offset2)

        assert "Entry C" in content3
        assert "Entry A" not in content3
        assert "Entry B" not in content3
        assert offset3 > offset2 > offset1


class TestClear:
    """clear() truncates the journal and deactivates the service."""

    def test_clear_sets_active_false(self, tmp_path):
        """clear() deactivates the service."""
        journal = ActivityJournalService()
        journal.init(tmp_path)
        assert journal.is_active

        journal.clear()
        assert not journal.is_active

    def test_clear_truncates_journal(self, tmp_path):
        """clear() truncates the journal file to zero bytes."""
        journal = ActivityJournalService()
        journal.init(tmp_path)
        journal.log("Some content")
        journal_path = journal.journal_path

        journal.clear()

        # File should exist but be empty (or not exist)
        if journal_path.exists():
            assert journal_path.stat().st_size == 0

    def test_clear_is_noop_when_not_active(self):
        """clear() does not raise when service was never activated."""
        journal = ActivityJournalService()
        journal.clear()  # Should not raise
        assert not journal.is_active

    def test_clear_resets_journal_path(self, tmp_path):
        """clear() resets journal_path property to None (M3 fix)."""
        journal = ActivityJournalService()
        journal.init(tmp_path)
        assert journal.journal_path is not None  # Confirm path set after init

        journal.clear()
        assert journal.journal_path is None  # Path must be None after clear


class TestCopyToFinal:
    """copy_to_final() copies journal to final output directory."""

    def test_copy_to_final_copies_journal_file(self, tmp_path):
        """copy_to_final() copies _activity.md to the target directory."""
        journal_dir = tmp_path / "staging"
        final_dir = tmp_path / "final"
        final_dir.mkdir()

        journal = ActivityJournalService()
        journal.init(journal_dir)
        journal.log("Analysis completed")

        journal.copy_to_final(final_dir)

        copied_file = final_dir / "_activity.md"
        assert copied_file.exists()
        assert "Analysis completed" in copied_file.read_text()

    def test_copy_to_final_creates_target_dir_if_needed(self, tmp_path):
        """copy_to_final() creates target directory if it does not exist."""
        journal_dir = tmp_path / "staging"
        final_dir = tmp_path / "final" / "subdir"

        journal = ActivityJournalService()
        journal.init(journal_dir)
        journal.log("Test entry")

        journal.copy_to_final(final_dir)

        assert final_dir.exists()
        assert (final_dir / "_activity.md").exists()

    def test_copy_to_final_noop_when_not_active(self, tmp_path):
        """copy_to_final() does not raise when service is not active."""
        journal = ActivityJournalService()
        final_dir = tmp_path / "final"
        final_dir.mkdir()

        journal.copy_to_final(final_dir)  # Should not raise

        # No file should be created
        assert not (final_dir / "_activity.md").exists()


class TestThreadSafety:
    """Thread safety: concurrent writes do not corrupt the journal."""

    def test_thread_safety_concurrent_writes(self, tmp_path):
        """Multiple threads writing simultaneously do not lose entries or corrupt file."""
        journal = ActivityJournalService()
        journal.init(tmp_path)

        num_threads = 10
        entries_per_thread = 20
        results = []
        errors = []

        def write_entries(thread_id):
            try:
                for i in range(entries_per_thread):
                    journal.log(f"Thread-{thread_id} entry-{i}")
                results.append(thread_id)
            except Exception as e:
                errors.append(str(e))

        threads = [
            threading.Thread(target=write_entries, args=(t,))
            for t in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert not errors, f"Thread errors: {errors}"
        assert len(results) == num_threads

        # All entries should be present
        content, _ = journal.get_content(0)
        for thread_id in range(num_threads):
            for i in range(entries_per_thread):
                assert f"Thread-{thread_id} entry-{i}" in content

    def test_thread_safety_get_content_concurrent_with_writes(self, tmp_path):
        """get_content() can be called concurrently with log() without errors."""
        journal = ActivityJournalService()
        journal.init(tmp_path)

        stop_event = threading.Event()
        read_errors = []

        def reader():
            while not stop_event.is_set():
                try:
                    journal.get_content(0)
                except Exception as e:
                    read_errors.append(str(e))

        reader_thread = threading.Thread(target=reader)
        reader_thread.start()

        for i in range(50):
            journal.log(f"Entry {i}")

        stop_event.set()
        reader_thread.join(timeout=5.0)

        assert not read_errors, f"Read errors during concurrent access: {read_errors}"
