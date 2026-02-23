"""Tests for SimpleWatchHandler - Story #166.

Tests SimpleWatchHandler class for watching non-git folders and triggering indexing
callbacks when files change. ALL tests use real filesystem operations (Anti-Mock Foundation).
"""

import pytest
import time
import threading
import tempfile
import shutil
from pathlib import Path
from typing import List


class TestSimpleWatchHandlerInitialization:
    """Test SimpleWatchHandler initialization."""

    @pytest.fixture
    def temp_folder(self):
        """Create a temporary folder for testing (no git required)."""
        temp_dir = tempfile.mkdtemp(prefix="test_simple_watch_")
        folder_path = Path(temp_dir)

        # Create some initial test files
        (folder_path / "test1.py").write_text("def hello(): pass")
        (folder_path / "test2.py").write_text("def world(): pass")

        yield folder_path

        # Cleanup
        shutil.rmtree(temp_dir)

    def test_initialization_without_git(self, temp_folder):
        """Test that handler initializes successfully without git repository."""
        from code_indexer.services.simple_watch_handler import SimpleWatchHandler

        callback_invocations = []

        def callback(changed_files: List[str], event_type: str):
            callback_invocations.append({"files": changed_files, "type": event_type})

        handler = SimpleWatchHandler(
            folder_path=str(temp_folder),
            indexing_callback=callback,
            debounce_seconds=0.2,
            idle_timeout_seconds=3.0,
        )

        # Verify handler created successfully
        assert handler is not None
        assert not handler.is_watching()

    def test_initialization_parameters(self, temp_folder):
        """Test that initialization parameters are stored correctly."""
        from code_indexer.services.simple_watch_handler import SimpleWatchHandler

        def callback(changed_files: List[str], event_type: str):
            pass

        handler = SimpleWatchHandler(
            folder_path=str(temp_folder),
            indexing_callback=callback,
            debounce_seconds=0.3,
            idle_timeout_seconds=5.0,
        )

        # Verify parameters stored (implementation should expose these or be testable)
        assert handler.debounce_seconds == 0.3
        assert handler.idle_timeout_seconds == 5.0


class TestSimpleWatchHandlerFileEvents:
    """Test file event detection and handling."""

    @pytest.fixture
    def temp_folder(self):
        """Create a temporary folder for testing."""
        temp_dir = tempfile.mkdtemp(prefix="test_simple_watch_")
        folder_path = Path(temp_dir)
        yield folder_path
        shutil.rmtree(temp_dir)

    def test_file_created_event(self, temp_folder):
        """Test that file creation events are detected."""
        from code_indexer.services.simple_watch_handler import SimpleWatchHandler

        callback_invocations = []

        def callback(changed_files: List[str], event_type: str):
            callback_invocations.append({"files": changed_files, "type": event_type})

        handler = SimpleWatchHandler(
            folder_path=str(temp_folder),
            indexing_callback=callback,
            debounce_seconds=0.2,
            idle_timeout_seconds=5.0,
        )

        handler.start_watching()
        assert handler.is_watching()

        # Create a new file
        time.sleep(0.1)  # Let observer start
        new_file = temp_folder / "new_file.py"
        new_file.write_text("def new_function(): pass")

        # Wait for debounce + processing
        time.sleep(0.5)

        # Stop watching
        handler.stop_watching()

        # Verify callback was invoked with created event
        assert len(callback_invocations) > 0
        last_invocation = callback_invocations[-1]
        assert (
            "created" in last_invocation["type"]
            or "modified" in last_invocation["type"]
        )
        assert any(str(new_file) in f for f in last_invocation["files"])

    def test_file_modified_event(self, temp_folder):
        """Test that file modification events are detected."""
        from code_indexer.services.simple_watch_handler import SimpleWatchHandler

        # Create initial file
        test_file = temp_folder / "test.py"
        test_file.write_text("def original(): pass")

        callback_invocations = []

        def callback(changed_files: List[str], event_type: str):
            callback_invocations.append({"files": changed_files, "type": event_type})

        handler = SimpleWatchHandler(
            folder_path=str(temp_folder),
            indexing_callback=callback,
            debounce_seconds=0.2,
            idle_timeout_seconds=5.0,
        )

        handler.start_watching()
        time.sleep(0.1)  # Let observer start

        # Modify the file
        test_file.write_text("def modified(): pass")

        # Wait for debounce + processing
        time.sleep(0.5)

        handler.stop_watching()

        # Verify callback was invoked with modified event
        assert len(callback_invocations) > 0
        last_invocation = callback_invocations[-1]
        assert "modified" in last_invocation["type"]
        assert any(str(test_file) in f for f in last_invocation["files"])

    def test_file_deleted_event(self, temp_folder):
        """Test that file deletion events are detected."""
        from code_indexer.services.simple_watch_handler import SimpleWatchHandler

        # Create initial file
        test_file = temp_folder / "to_delete.py"
        test_file.write_text("def to_delete(): pass")

        callback_invocations = []

        def callback(changed_files: List[str], event_type: str):
            callback_invocations.append({"files": changed_files, "type": event_type})

        handler = SimpleWatchHandler(
            folder_path=str(temp_folder),
            indexing_callback=callback,
            debounce_seconds=0.2,
            idle_timeout_seconds=5.0,
        )

        handler.start_watching()
        time.sleep(0.1)  # Let observer start

        # Delete the file
        test_file.unlink()

        # Wait for debounce + processing
        time.sleep(0.5)

        handler.stop_watching()

        # Verify callback was invoked with deleted event
        assert len(callback_invocations) > 0
        last_invocation = callback_invocations[-1]
        assert "deleted" in last_invocation["type"]
        assert any(str(test_file) in f for f in last_invocation["files"])


class TestSimpleWatchHandlerDebouncing:
    """Test event debouncing functionality."""

    @pytest.fixture
    def temp_folder(self):
        """Create a temporary folder for testing."""
        temp_dir = tempfile.mkdtemp(prefix="test_simple_watch_")
        folder_path = Path(temp_dir)
        yield folder_path
        shutil.rmtree(temp_dir)

    def test_rapid_changes_debounced(self, temp_folder):
        """Test that rapid file changes are batched into single callback."""
        from code_indexer.services.simple_watch_handler import SimpleWatchHandler

        callback_invocations = []

        def callback(changed_files: List[str], event_type: str):
            callback_invocations.append({"files": changed_files, "type": event_type})

        handler = SimpleWatchHandler(
            folder_path=str(temp_folder),
            indexing_callback=callback,
            debounce_seconds=0.3,  # 300ms debounce
            idle_timeout_seconds=5.0,
        )

        handler.start_watching()
        time.sleep(0.1)  # Let observer start

        # Make rapid changes to same file
        test_file = temp_folder / "rapid.py"
        for i in range(5):
            test_file.write_text(f"def func_{i}(): pass")
            time.sleep(0.05)  # 50ms between changes (faster than debounce)

        # Wait for debounce to trigger
        time.sleep(0.5)

        handler.stop_watching()

        # Verify single callback invocation (debounced)
        # Should be 1-2 invocations, not 5
        assert len(callback_invocations) <= 2, "Rapid changes should be debounced"


class TestSimpleWatchHandlerIdleTimeout:
    """Test auto-shutdown on idle timeout."""

    @pytest.fixture
    def temp_folder(self):
        """Create a temporary folder for testing."""
        temp_dir = tempfile.mkdtemp(prefix="test_simple_watch_")
        folder_path = Path(temp_dir)
        yield folder_path
        shutil.rmtree(temp_dir)

    def test_idle_timeout_auto_shutdown(self, temp_folder):
        """Test that handler auto-stops after idle timeout."""
        from code_indexer.services.simple_watch_handler import SimpleWatchHandler

        callback_invocations = []

        def callback(changed_files: List[str], event_type: str):
            callback_invocations.append({"files": changed_files, "type": event_type})

        handler = SimpleWatchHandler(
            folder_path=str(temp_folder),
            indexing_callback=callback,
            debounce_seconds=0.1,
            idle_timeout_seconds=2.0,  # 2 second idle timeout
        )

        handler.start_watching()
        assert handler.is_watching()

        # Wait for idle timeout to trigger
        time.sleep(2.5)

        # Handler should auto-stop
        assert not handler.is_watching(), "Handler should auto-stop after idle timeout"

    def test_file_activity_resets_idle_timer(self, temp_folder):
        """Test that file events reset the idle timer."""
        from code_indexer.services.simple_watch_handler import SimpleWatchHandler

        callback_invocations = []

        def callback(changed_files: List[str], event_type: str):
            callback_invocations.append({"files": changed_files, "type": event_type})

        handler = SimpleWatchHandler(
            folder_path=str(temp_folder),
            indexing_callback=callback,
            debounce_seconds=0.1,
            idle_timeout_seconds=2.0,  # 2 second idle timeout
        )

        handler.start_watching()
        time.sleep(0.1)

        # Create file activity before timeout
        test_file = temp_folder / "activity.py"
        test_file.write_text("def test(): pass")

        # Wait but not long enough for timeout
        time.sleep(1.0)

        # Create more activity
        test_file.write_text("def test2(): pass")

        # Wait but not long enough for timeout again
        time.sleep(1.0)

        # Handler should still be watching (activity reset timer)
        assert handler.is_watching(), "File activity should reset idle timer"

        handler.stop_watching()


class TestSimpleWatchHandlerThreadSafety:
    """Test thread safety under concurrent operations."""

    @pytest.fixture
    def temp_folder(self):
        """Create a temporary folder for testing."""
        temp_dir = tempfile.mkdtemp(prefix="test_simple_watch_")
        folder_path = Path(temp_dir)
        yield folder_path
        shutil.rmtree(temp_dir)

    def test_concurrent_file_operations(self, temp_folder):
        """Test thread safety with concurrent file operations."""
        from code_indexer.services.simple_watch_handler import SimpleWatchHandler

        callback_invocations = []
        callback_lock = threading.Lock()

        def callback(changed_files: List[str], event_type: str):
            with callback_lock:
                callback_invocations.append(
                    {"files": changed_files, "type": event_type}
                )

        handler = SimpleWatchHandler(
            folder_path=str(temp_folder),
            indexing_callback=callback,
            debounce_seconds=0.2,
            idle_timeout_seconds=5.0,
        )

        handler.start_watching()
        time.sleep(0.1)

        # Create multiple threads performing file operations
        def file_operations(thread_id: int):
            for i in range(3):
                test_file = temp_folder / f"thread_{thread_id}_file_{i}.py"
                test_file.write_text(f"def func_{thread_id}_{i}(): pass")
                time.sleep(0.05)

        threads = []
        for i in range(3):
            thread = threading.Thread(target=file_operations, args=(i,))
            threads.append(thread)
            thread.start()

        # Wait for threads to complete
        for thread in threads:
            thread.join()

        # Wait for processing
        time.sleep(0.5)

        handler.stop_watching()

        # Verify no crashes and callbacks were invoked
        assert len(callback_invocations) > 0, "Callbacks should be invoked"


class TestSimpleWatchHandlerResourceCleanup:
    """Test proper resource cleanup on stop."""

    @pytest.fixture
    def temp_folder(self):
        """Create a temporary folder for testing."""
        temp_dir = tempfile.mkdtemp(prefix="test_simple_watch_")
        folder_path = Path(temp_dir)
        yield folder_path
        shutil.rmtree(temp_dir)

    def test_stop_watching_releases_resources(self, temp_folder):
        """Test that stop_watching() releases all resources."""
        from code_indexer.services.simple_watch_handler import SimpleWatchHandler

        callback_invocations = []

        def callback(changed_files: List[str], event_type: str):
            callback_invocations.append({"files": changed_files, "type": event_type})

        handler = SimpleWatchHandler(
            folder_path=str(temp_folder),
            indexing_callback=callback,
            debounce_seconds=0.2,
            idle_timeout_seconds=5.0,
        )

        handler.start_watching()
        assert handler.is_watching()

        # Stop watching
        handler.stop_watching()

        # Verify resources released
        assert not handler.is_watching()

        # Verify observer stopped
        if hasattr(handler, "observer") and handler.observer:
            assert not handler.observer.is_alive()

    def test_multiple_start_stop_cycles(self, temp_folder):
        """Test multiple start/stop cycles don't leak resources."""
        from code_indexer.services.simple_watch_handler import SimpleWatchHandler

        callback_invocations = []

        def callback(changed_files: List[str], event_type: str):
            callback_invocations.append({"files": changed_files, "type": event_type})

        handler = SimpleWatchHandler(
            folder_path=str(temp_folder),
            indexing_callback=callback,
            debounce_seconds=0.1,
            idle_timeout_seconds=10.0,
        )

        # Perform multiple start/stop cycles
        for _ in range(3):
            handler.start_watching()
            assert handler.is_watching()
            time.sleep(0.2)
            handler.stop_watching()
            assert not handler.is_watching()
            time.sleep(0.1)


class TestSimpleWatchHandlerStats:
    """Test statistics tracking."""

    @pytest.fixture
    def temp_folder(self):
        """Create a temporary folder for testing."""
        temp_dir = tempfile.mkdtemp(prefix="test_simple_watch_")
        folder_path = Path(temp_dir)
        yield folder_path
        shutil.rmtree(temp_dir)

    def test_get_stats_returns_required_fields(self, temp_folder):
        """Test that get_stats() returns all required fields."""
        from code_indexer.services.simple_watch_handler import SimpleWatchHandler

        def callback(changed_files: List[str], event_type: str):
            pass

        handler = SimpleWatchHandler(
            folder_path=str(temp_folder),
            indexing_callback=callback,
            debounce_seconds=0.2,
            idle_timeout_seconds=5.0,
        )

        # Get stats before starting
        stats = handler.get_stats()

        # Verify required fields
        assert isinstance(stats, dict)
        assert "files_processed" in stats
        assert "indexing_cycles" in stats
        assert "pending_changes" in stats
        assert "current_branch" in stats
        assert stats["current_branch"] is None  # SimpleWatchHandler always returns None

    def test_stats_tracking_accuracy(self, temp_folder):
        """Test that stats accurately track operations."""
        from code_indexer.services.simple_watch_handler import SimpleWatchHandler

        callback_invocations = []

        def callback(changed_files: List[str], event_type: str):
            callback_invocations.append({"files": changed_files, "type": event_type})

        handler = SimpleWatchHandler(
            folder_path=str(temp_folder),
            indexing_callback=callback,
            debounce_seconds=0.2,
            idle_timeout_seconds=5.0,
        )

        handler.start_watching()
        time.sleep(0.1)

        # Create files
        (temp_folder / "file1.py").write_text("def func1(): pass")
        time.sleep(0.05)
        (temp_folder / "file2.py").write_text("def func2(): pass")

        # Wait for processing
        time.sleep(0.5)

        # Get stats
        stats = handler.get_stats()

        handler.stop_watching()

        # Verify stats
        assert stats["files_processed"] > 0
        assert stats["indexing_cycles"] > 0


class TestSimpleWatchHandlerTemporaryFileFiltering:
    """Test that temporary files are ignored."""

    @pytest.fixture
    def temp_folder(self):
        """Create a temporary folder for testing."""
        temp_dir = tempfile.mkdtemp(prefix="test_simple_watch_")
        folder_path = Path(temp_dir)
        yield folder_path
        shutil.rmtree(temp_dir)

    def test_temporary_files_ignored(self, temp_folder):
        """Test that *.tmp, *.swp, *.json.tmp files are ignored."""
        from code_indexer.services.simple_watch_handler import SimpleWatchHandler

        callback_invocations = []

        def callback(changed_files: List[str], event_type: str):
            callback_invocations.append({"files": changed_files, "type": event_type})

        handler = SimpleWatchHandler(
            folder_path=str(temp_folder),
            indexing_callback=callback,
            debounce_seconds=0.2,
            idle_timeout_seconds=5.0,
        )

        handler.start_watching()
        time.sleep(0.1)

        # Create temporary files
        (temp_folder / "test.tmp").write_text("temp")
        (temp_folder / "test.swp").write_text("swap")
        (temp_folder / "data.json.tmp").write_text("json tmp")

        # Create regular file
        (temp_folder / "regular.py").write_text("def test(): pass")

        # Wait for processing
        time.sleep(0.5)

        handler.stop_watching()

        # Verify only regular file triggered callback
        if len(callback_invocations) > 0:
            all_files = []
            for invocation in callback_invocations:
                all_files.extend(invocation["files"])

            # Temporary files should not be in callbacks
            assert not any(".tmp" in f for f in all_files)
            assert not any(".swp" in f for f in all_files)


class TestSimpleWatchHandlerFullLifecycle:
    """Test full lifecycle from start to idle shutdown."""

    @pytest.fixture
    def temp_folder(self):
        """Create a temporary folder for testing."""
        temp_dir = tempfile.mkdtemp(prefix="test_simple_watch_")
        folder_path = Path(temp_dir)
        yield folder_path
        shutil.rmtree(temp_dir)

    def test_full_lifecycle(self, temp_folder):
        """Test complete lifecycle: start → events → debounce → callback → idle → stop."""
        from code_indexer.services.simple_watch_handler import SimpleWatchHandler

        callback_invocations = []

        def callback(changed_files: List[str], event_type: str):
            callback_invocations.append(
                {"files": changed_files, "type": event_type, "timestamp": time.time()}
            )

        handler = SimpleWatchHandler(
            folder_path=str(temp_folder),
            indexing_callback=callback,
            debounce_seconds=0.2,
            idle_timeout_seconds=2.0,
        )

        # Start watching
        handler.start_watching()
        assert handler.is_watching()
        start_time = time.time()

        # Generate file events
        time.sleep(0.1)
        (temp_folder / "test1.py").write_text("def test1(): pass")
        time.sleep(0.1)
        (temp_folder / "test2.py").write_text("def test2(): pass")

        # Wait for debounce to process
        time.sleep(0.5)

        # Verify callbacks invoked
        assert len(callback_invocations) > 0

        # Wait for idle timeout
        time.sleep(2.5)

        # Verify auto-stopped
        assert not handler.is_watching()

        # Verify lifecycle completed in reasonable time
        total_time = time.time() - start_time
        assert total_time < 5.0, "Full lifecycle should complete quickly"


class TestSimpleWatchHandlerTmpPrefixFiltering:
    """Test that .tmp_ prefixed files (from _atomic_write_file) are filtered.

    Bug #274: _atomic_write_file() creates temp files like .tmp_XXXXXX_filename.md.
    Path(".tmp_abc_file.md").suffix == ".md" so they pass the extension check.
    SimpleWatchHandler only filters *.tmp suffix, not .tmp_ prefix.
    Fix: _should_ignore_file must also ignore files whose basename starts with .tmp_
    """

    def test_tmp_prefix_file_is_ignored(self):
        """_should_ignore_file must return True for .tmp_XXXXXX_somefile.md."""
        from code_indexer.services.simple_watch_handler import SimpleWatchHandler
        from pathlib import Path

        handler = SimpleWatchHandler(
            folder_path="/tmp",
            indexing_callback=lambda f, t: None,
        )

        # Atomic write temp files: prefix=".tmp_", suffix=f"_{filename}"
        assert handler._should_ignore_file(
            Path("/some/dir/.tmp_abc123_somefile.md")
        ), ".tmp_ prefixed file must be ignored"

    def test_tmp_prefix_with_only_md_extension_is_ignored(self):
        """Even when the final extension is .md (not .tmp), .tmp_ prefix must be ignored."""
        from code_indexer.services.simple_watch_handler import SimpleWatchHandler
        from pathlib import Path

        handler = SimpleWatchHandler(
            folder_path="/tmp",
            indexing_callback=lambda f, t: None,
        )

        # Suffix is .md but basename starts with .tmp_
        p = Path("/repo/.tmp_XYZ12345_README.md")
        assert handler._should_ignore_file(p), (
            f"File {p.name} has .md extension but .tmp_ prefix — must be ignored"
        )

    def test_regular_md_file_is_not_ignored(self):
        """Regular .md files without .tmp_ prefix must NOT be ignored."""
        from code_indexer.services.simple_watch_handler import SimpleWatchHandler
        from pathlib import Path

        handler = SimpleWatchHandler(
            folder_path="/tmp",
            indexing_callback=lambda f, t: None,
        )

        assert not handler._should_ignore_file(
            Path("/some/dir/README.md")
        ), "Regular .md file must not be ignored"

    def test_tmp_suffix_still_ignored(self):
        """Existing .tmp suffix filter must still work."""
        from code_indexer.services.simple_watch_handler import SimpleWatchHandler
        from pathlib import Path

        handler = SimpleWatchHandler(
            folder_path="/tmp",
            indexing_callback=lambda f, t: None,
        )

        assert handler._should_ignore_file(
            Path("/some/dir/somefile.tmp")
        ), ".tmp suffix file must still be ignored"

    def test_tmp_prefix_and_tmp_suffix_both_ignored(self):
        """File that is both .tmp_ prefixed and .tmp suffixed must be ignored."""
        from code_indexer.services.simple_watch_handler import SimpleWatchHandler
        from pathlib import Path

        handler = SimpleWatchHandler(
            folder_path="/tmp",
            indexing_callback=lambda f, t: None,
        )

        assert handler._should_ignore_file(
            Path("/some/dir/.tmp_abc123_somefile.tmp")
        ), ".tmp_ prefix + .tmp suffix file must be ignored"

    def test_tmp_prefix_watch_does_not_trigger_callback(self, tmp_path):
        """Live watch: creating a .tmp_ prefixed file must NOT trigger the indexing callback."""
        import time
        from code_indexer.services.simple_watch_handler import SimpleWatchHandler

        callback_files: list = []

        def callback(changed_files, event_type):
            callback_files.extend(changed_files)

        handler = SimpleWatchHandler(
            folder_path=str(tmp_path),
            indexing_callback=callback,
            debounce_seconds=0.2,
            idle_timeout_seconds=5.0,
        )
        handler.start_watching()
        time.sleep(0.1)

        # Create an atomic write temp file (as _atomic_write_file would)
        tmp_file = tmp_path / ".tmp_abc123_content.md"
        tmp_file.write_text("temp content")

        # Create a regular file that SHOULD trigger callback
        real_file = tmp_path / "real_content.md"
        real_file.write_text("real content")

        time.sleep(0.5)
        handler.stop_watching()

        # .tmp_ prefixed file must not appear in any callback
        assert not any(".tmp_" in f for f in callback_files), (
            f".tmp_ prefixed file must not trigger callback. Got: {callback_files}"
        )
        # The real file should have triggered callback
        assert any("real_content.md" in f for f in callback_files), (
            f"real_content.md should have triggered callback. Got: {callback_files}"
        )


class TestSimpleWatchHandlerAdditionalHandlers:
    """Test additional_handlers functionality for attaching extra FileSystemEventHandlers."""

    @pytest.fixture
    def temp_folder(self):
        """Create a temporary folder for testing."""
        temp_dir = tempfile.mkdtemp(prefix="test_simple_watch_")
        folder_path = Path(temp_dir)
        yield folder_path
        shutil.rmtree(temp_dir)

    def test_additional_handlers_default_empty(self, temp_folder):
        """Test that additional_handlers defaults to empty list."""
        from code_indexer.services.simple_watch_handler import SimpleWatchHandler

        def callback(changed_files: List[str], event_type: str):
            pass

        handler = SimpleWatchHandler(
            folder_path=str(temp_folder),
            indexing_callback=callback,
        )

        # Verify additional_handlers exists and is empty
        assert hasattr(handler, "additional_handlers")
        assert handler.additional_handlers == []

    def test_additional_handlers_scheduled_on_observer(self, temp_folder):
        """Test that additional handlers are scheduled on the observer and receive events."""
        from code_indexer.services.simple_watch_handler import SimpleWatchHandler
        from watchdog.events import FileSystemEventHandler, FileSystemEvent

        # Create event recorder handler (Anti-Mock: real FileSystemEventHandler)
        class EventRecorder(FileSystemEventHandler):
            def __init__(self):
                super().__init__()
                self.events = []
                self.events_lock = threading.Lock()

            def on_created(self, event):
                if not event.is_directory:
                    with self.events_lock:
                        self.events.append(("created", event.src_path))

            def on_modified(self, event):
                if not event.is_directory:
                    with self.events_lock:
                        self.events.append(("modified", event.src_path))

        # Create recorder
        recorder = EventRecorder()

        # Create callback for main handler
        callback_invocations = []

        def callback(changed_files: List[str], event_type: str):
            callback_invocations.append({"files": changed_files, "type": event_type})

        # Create handler with additional handler
        handler = SimpleWatchHandler(
            folder_path=str(temp_folder),
            indexing_callback=callback,
            debounce_seconds=0.2,
            idle_timeout_seconds=5.0,
            additional_handlers=[recorder],
        )

        # Start watching
        handler.start_watching()
        assert handler.is_watching()
        time.sleep(0.1)

        # Create a file
        test_file = temp_folder / "test.py"
        test_file.write_text("def test(): pass")

        # Wait for events to be processed
        time.sleep(0.5)

        # Stop watching
        handler.stop_watching()

        # Verify main handler received callback
        assert len(callback_invocations) > 0

        # Verify additional handler received events
        assert len(recorder.events) > 0
        # Check that test file is in recorded events
        assert any(str(test_file) in event_path for _, event_path in recorder.events)

    def test_additional_handlers_set_after_init(self, temp_folder):
        """Test that additional_handlers can be set after initialization."""
        from code_indexer.services.simple_watch_handler import SimpleWatchHandler
        from watchdog.events import FileSystemEventHandler

        # Create event recorder
        class EventRecorder(FileSystemEventHandler):
            def __init__(self):
                super().__init__()
                self.events = []
                self.events_lock = threading.Lock()

            def on_created(self, event):
                if not event.is_directory:
                    with self.events_lock:
                        self.events.append(("created", event.src_path))

        recorder = EventRecorder()

        def callback(changed_files: List[str], event_type: str):
            pass

        # Create handler WITHOUT additional handlers
        handler = SimpleWatchHandler(
            folder_path=str(temp_folder),
            indexing_callback=callback,
            debounce_seconds=0.2,
            idle_timeout_seconds=5.0,
        )

        # Set additional_handlers AFTER init
        handler.additional_handlers = [recorder]

        # Start watching (should schedule the additional handler)
        handler.start_watching()
        time.sleep(0.1)

        # Create a file
        test_file = temp_folder / "test2.py"
        test_file.write_text("def test2(): pass")

        # Wait for events
        time.sleep(0.5)

        # Stop watching
        handler.stop_watching()

        # Verify additional handler received events
        assert len(recorder.events) > 0
        assert any(str(test_file) in event_path for _, event_path in recorder.events)
