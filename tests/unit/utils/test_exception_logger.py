"""Unit tests for centralized exception logger.

Tests exception logging functionality including:
- Log file creation with timestamp and PID
- Exception logging with full context
- Mode-specific log file paths (CLI/Daemon vs Server)
- Thread exception handling
"""

import json
import os
import sys
import threading
import time
from datetime import datetime
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def reset_exception_logger_singleton():
    """Reset ExceptionLogger singleton before each test."""
    from code_indexer.utils.exception_logger import ExceptionLogger

    ExceptionLogger._instance = None
    yield
    ExceptionLogger._instance = None


class TestExceptionLoggerInitialization:
    """Test exception logger initialization and log file creation."""

    def test_cli_mode_creates_log_file_in_project_directory(self, tmp_path):
        """Test that CLI mode creates error log in .code-indexer/ directory."""
        from code_indexer.utils.exception_logger import ExceptionLogger

        project_root = tmp_path / "test_project"
        project_root.mkdir()

        logger = ExceptionLogger.initialize(project_root, mode="cli")

        # Verify log file created in project's .code-indexer directory
        assert logger.log_file_path.parent == project_root / ".code-indexer"  # type: ignore[union-attr]
        # Bug #2060: created on first write, not by initialize().
        assert not logger.log_file_path.exists()  # type: ignore[union-attr]

        # Verify filename format: error_<timestamp>_<pid>.log
        filename = logger.log_file_path.name  # type: ignore[union-attr]
        assert filename.startswith("error_")
        assert filename.endswith(".log")

        # Verify filename contains PID
        pid = os.getpid()
        assert str(pid) in filename

    def test_daemon_mode_creates_log_file_in_project_directory(self, tmp_path):
        """Test that Daemon mode creates error log in .code-indexer/ directory."""
        from code_indexer.utils.exception_logger import ExceptionLogger

        project_root = tmp_path / "test_project"
        project_root.mkdir()

        logger = ExceptionLogger.initialize(project_root, mode="daemon")

        # Daemon mode uses same location as CLI
        assert logger.log_file_path.parent == project_root / ".code-indexer"  # type: ignore[union-attr]
        # Bug #2060: created on first write, not by initialize().
        assert not logger.log_file_path.exists()  # type: ignore[union-attr]

    def test_server_mode_creates_log_file_in_home_directory(
        self, tmp_path, monkeypatch
    ):
        """Test that Server mode creates error log in ~/.cidx-server/logs/."""
        from code_indexer.utils.exception_logger import ExceptionLogger

        # Bug #1996: the session isolates CIDX_SERVER_DATA_DIR; this test
        # exercises the home default (Path.home patched to tmp below).
        monkeypatch.delenv("CIDX_SERVER_DATA_DIR", raising=False)
        with patch("pathlib.Path.home") as mock_home:
            mock_home.return_value = tmp_path
            project_root = tmp_path / "test_project"
            project_root.mkdir()

            logger = ExceptionLogger.initialize(project_root, mode="server")

            # Server mode uses ~/.cidx-server/logs/
            expected_log_dir = tmp_path / ".cidx-server" / "logs"
            assert logger.log_file_path.parent == expected_log_dir  # type: ignore[union-attr]
            # Bug #2060: created on first write, not by initialize().
            assert not logger.log_file_path.exists()  # type: ignore[union-attr]

    def test_server_mode_honors_cidx_server_data_dir_env_var(
        self, tmp_path, monkeypatch
    ):
        """Bug #1776: Server mode must honor CIDX_SERVER_DATA_DIR env var.

        An isolated/test server instance sets CIDX_SERVER_DATA_DIR to a
        throwaway directory. Without honoring it, exception_logger.py
        hardcodes Path.home() / ".cidx-server", leaking log files into
        (and potentially overwriting files in) the REAL server directory.
        """
        from code_indexer.utils.exception_logger import ExceptionLogger
        from pathlib import Path

        # Explicit reset (in addition to the autouse fixture above) since
        # initialize() is a no-op idempotent singleton per its own
        # docstring warning -- this test requires a fresh instance.
        ExceptionLogger._instance = None

        isolated_data_dir = tmp_path / "isolated-server-instance"
        monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(isolated_data_dir))

        project_root = tmp_path / "test_project"
        project_root.mkdir()

        logger = ExceptionLogger.initialize(project_root, mode="server")

        assert logger.log_file_path is not None

        expected_log_dir = isolated_data_dir / "logs"
        real_home_log_dir = Path.home() / ".cidx-server" / "logs"

        assert logger.log_file_path.parent == expected_log_dir
        assert logger.log_file_path.parent != real_home_log_dir
        # Bug #2060: created on first write, not by initialize().
        assert not logger.log_file_path.exists()

    def test_log_directory_created_on_first_write(self, tmp_path):
        """The log directory is created by the first logged exception."""
        from code_indexer.utils.exception_logger import ExceptionLogger

        project_root = tmp_path / "test_project"
        project_root.mkdir()

        # .code-indexer doesn't exist yet
        code_indexer_dir = project_root / ".code-indexer"
        assert not code_indexer_dir.exists()

        logger = ExceptionLogger.initialize(project_root, mode="cli")
        # Bug #2060: initialize() alone creates nothing.
        assert not code_indexer_dir.exists()

        try:
            raise OSError("boom")
        except OSError as e:
            logger.log_exception(e)

        assert code_indexer_dir.exists()
        assert logger.log_file_path.exists()  # type: ignore[union-attr]

    def test_filename_contains_timestamp_and_pid(self, tmp_path):
        """Test that log filename contains timestamp and PID for uniqueness."""
        from code_indexer.utils.exception_logger import ExceptionLogger

        project_root = tmp_path / "test_project"
        project_root.mkdir()

        datetime.now()
        logger = ExceptionLogger.initialize(project_root, mode="cli")
        datetime.now()

        filename = logger.log_file_path.name  # type: ignore[union-attr]
        pid = os.getpid()

        # Filename format: error_YYYYMMDD_HHMMSS_<pid>.log
        assert filename.startswith("error_")
        assert str(pid) in filename
        assert filename.endswith(".log")

        # Extract timestamp from filename
        # Format: error_20251109_143022_12345.log
        parts = filename.split("_")
        assert len(parts) >= 4
        date_part = parts[1]  # YYYYMMDD
        time_part = parts[2]  # HHMMSS

        # Basic validation that timestamp is reasonable
        assert len(date_part) == 8
        assert len(time_part) == 6
        current_year = str(datetime.now().year)
        assert date_part.startswith(current_year)  # Current year


class TestLazyLogFileCreation2060:
    """Bug #2060: the error log is created on the first write, never eagerly.

    The server spawns ``cidx`` constantly (refresh, registration, omni-regex
    queries whose cwd is inside a ``.versioned/`` snapshot). An eager file
    left an empty ``error_*.log`` per call and wrote inside immutable
    snapshots on the query path.
    """

    def test_initialize_without_exception_creates_no_file_or_directory(self, tmp_path):
        from code_indexer.utils.exception_logger import ExceptionLogger

        project_root = tmp_path / "test_project"
        project_root.mkdir()

        logger = ExceptionLogger.initialize(project_root, mode="cli")

        assert logger.log_file_path is not None
        assert logger.log_file_path.parent == project_root / ".code-indexer"
        assert not logger.log_file_path.exists()
        assert not (project_root / ".code-indexer").exists()
        assert list(project_root.rglob("*")) == []

    def test_server_mode_initialize_creates_no_file(self, tmp_path, monkeypatch):
        from code_indexer.utils.exception_logger import ExceptionLogger

        data_dir = tmp_path / "server-data"
        monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(data_dir))

        logger = ExceptionLogger.initialize(tmp_path, mode="server")

        assert logger.log_file_path is not None
        assert logger.log_file_path.parent == data_dir / "logs"
        assert not logger.log_file_path.exists()

    def test_first_exception_creates_file_with_unchanged_location_and_format(
        self, tmp_path
    ):
        from code_indexer.utils.exception_logger import ExceptionLogger

        project_root = tmp_path / "test_project"
        project_root.mkdir()
        logger = ExceptionLogger.initialize(project_root, mode="cli")
        assert logger.log_file_path is not None

        try:
            raise ValueError("first real error")
        except ValueError as e:
            logger.log_exception(e, thread_name="T1", context={"k": "v"})

        log_files = list((project_root / ".code-indexer").glob("error_*.log"))
        assert log_files == [logger.log_file_path]
        assert logger.log_file_path.name.endswith(f"_{os.getpid()}.log")

        content = logger.log_file_path.read_text()
        assert content.endswith("\n---\n")
        entry = json.loads(content[: -len("\n---\n")])
        assert entry["exception_type"] == "ValueError"
        assert entry["exception_message"] == "first real error"
        assert entry["thread"] == "T1"
        assert entry["context"] == {"k": "v"}
        assert content == json.dumps(entry, indent=2) + "\n---\n"


class TestVersionedSnapshotRedirect2060:
    """Bug #2060: never write inside a ``.versioned`` snapshot.

    The server runs ``cidx`` with its cwd inside immutable snapshots (e.g.
    the multi-repo regex query path). A real error there must be logged
    outside the snapshot: in the server-mode log directory.
    """

    @pytest.fixture
    def server_logs(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "server-data"
        monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(data_dir))
        return data_dir / "logs"

    @pytest.fixture
    def snapshot(self, tmp_path):
        root = (
            tmp_path / "golden-repos" / ".versioned" / "example-repo" / "v_1700000000"
        )
        (root / "src").mkdir(parents=True)
        return root

    @staticmethod
    def _log_one(project_root):
        from code_indexer.utils.exception_logger import ExceptionLogger

        logger = ExceptionLogger.initialize(project_root, mode="cli")
        try:
            raise RuntimeError("error inside snapshot")
        except RuntimeError as e:
            logger.log_exception(e)
        return logger

    @staticmethod
    def _snapshot_files(snapshot):
        return sorted(p.relative_to(snapshot) for p in snapshot.rglob("*"))

    def test_snapshot_root_logs_outside_the_snapshot(self, snapshot, server_logs):
        before = self._snapshot_files(snapshot)

        logger = self._log_one(snapshot)

        assert self._snapshot_files(snapshot) == before
        assert logger.log_file_path is not None
        assert logger.log_file_path.parent == server_logs
        assert "error inside snapshot" in logger.log_file_path.read_text()

    def test_snapshot_subdirectory_logs_outside_the_snapshot(
        self, snapshot, server_logs
    ):
        before = self._snapshot_files(snapshot)

        logger = self._log_one(snapshot / "src")

        assert self._snapshot_files(snapshot) == before
        assert logger.log_file_path is not None
        assert logger.log_file_path.parent == server_logs
        assert logger.log_file_path.exists()

    def test_unwritable_fallback_never_raises_and_reports_to_stderr(
        self, snapshot, tmp_path, monkeypatch, capsys
    ):
        from code_indexer.utils.exception_logger import ExceptionLogger

        # A regular FILE as the data dir: "<file>/logs" can never be created,
        # whatever the process's privileges.
        not_a_dir = tmp_path / "data-dir-is-a-file"
        not_a_dir.write_text("")
        monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(not_a_dir))
        before = self._snapshot_files(snapshot)
        logger = ExceptionLogger.initialize(snapshot, mode="cli")

        try:
            raise KeyError("original failure")
        except KeyError as e:
            logger.log_exception(e)  # must not raise

        err = capsys.readouterr().err
        assert "KeyError" in err
        assert "NotADirectoryError" in err or "FileExistsError" in err
        assert str(tmp_path) not in err and "data-dir-is-a-file" not in err
        assert self._snapshot_files(snapshot) == before

    def test_rule_covers_every_canonical_snapshot(self, snapshot, server_logs):
        """The logger's rule (any `.versioned` path component) must cover
        every path the canonical predicate calls a snapshot."""
        from code_indexer.server.storage.shared.snapshot_paths import (
            is_versioned_snapshot,
        )
        from code_indexer.utils.exception_logger import ExceptionLogger

        assert is_versioned_snapshot(str(snapshot))
        logger = ExceptionLogger.initialize(snapshot, mode="cli")
        assert logger.log_file_path is not None
        assert logger.log_file_path.parent == server_logs


class TestConcurrentWrites2060:
    """Bug #2060: one logger, many threads: entries must never interleave."""

    THREADS = 16
    ENTRIES_PER_THREAD = 5
    # Larger than the default file buffer (8 KiB), so an unserialized entry
    # reaches the file in several write() calls that other threads can split.
    PAD_CHARS = 40_000

    def test_concurrent_first_writes_never_interleave(self, tmp_path):
        from code_indexer.utils.exception_logger import ExceptionLogger

        logger = ExceptionLogger.initialize(tmp_path, mode="cli")
        assert logger.log_file_path is not None
        barrier = threading.Barrier(self.THREADS)

        def worker(t: int) -> None:
            barrier.wait()  # all threads race the FIRST write together
            for n in range(self.ENTRIES_PER_THREAD):
                try:
                    raise ValueError(f"t{t}-n{n}")
                except ValueError as e:
                    logger.log_exception(e, context={"pad": "x" * self.PAD_CHARS})

        threads = [
            threading.Thread(target=worker, args=(t,)) for t in range(self.THREADS)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        content = logger.log_file_path.read_text()
        entries = [e for e in content.split("\n---\n") if e.strip()]
        assert len(entries) == self.THREADS * self.ENTRIES_PER_THREAD
        messages = sorted(json.loads(e)["exception_message"] for e in entries)
        assert messages == sorted(
            f"t{t}-n{n}"
            for t in range(self.THREADS)
            for n in range(self.ENTRIES_PER_THREAD)
        )


class _UnprintableError(Exception):
    """An exception whose message cannot be rendered."""

    def __str__(self) -> str:
        raise RuntimeError("__str__ failed")


class _RaisingStream:
    """A stderr replacement whose every write fails."""

    def write(self, _text: str) -> int:
        raise RuntimeError("stream broken")

    def flush(self) -> None:
        raise RuntimeError("stream broken")


class TestLogExceptionNeverRaises2060:
    """Bug #2060: log_exception must never raise -- raising would replace
    the exception being logged."""

    def test_exception_whose_str_raises_does_not_escape(self, tmp_path, capsys):
        from code_indexer.utils.exception_logger import ExceptionLogger

        logger = ExceptionLogger.initialize(tmp_path, mode="cli")
        try:
            raise _UnprintableError()
        except _UnprintableError as e:
            logger.log_exception(e)  # must return normally

        assert "_UnprintableError" in capsys.readouterr().err

    def test_broken_stderr_during_diagnostic_does_not_escape(
        self, tmp_path, monkeypatch
    ):
        from code_indexer.utils.exception_logger import ExceptionLogger

        # The log write fails: the log directory path is a regular file.
        (tmp_path / ".code-indexer").write_text("")
        logger = ExceptionLogger.initialize(tmp_path, mode="cli")
        monkeypatch.setattr(sys, "stderr", _RaisingStream())

        try:
            raise ValueError("original")
        except ValueError as e:
            logger.log_exception(e)  # must return normally


class TestExceptionLogging:
    """Test exception logging functionality."""

    def test_log_exception_writes_json_to_file(self, tmp_path):
        """Test that logging an exception writes JSON data to the log file."""
        from code_indexer.utils.exception_logger import ExceptionLogger

        project_root = tmp_path / "test_project"
        project_root.mkdir()

        logger = ExceptionLogger.initialize(project_root, mode="cli")

        # Create and log an exception
        try:
            raise ValueError("Test error message")
        except ValueError as e:
            logger.log_exception(e, context={"test": "data"})

        # Read and verify log file contents
        with open(logger.log_file_path) as f:  # type: ignore[arg-type]
            content = f.read()

        # Should contain JSON
        assert "ValueError" in content
        assert "Test error message" in content
        assert "test" in content
        assert "data" in content

        # Verify it's valid JSON (between separators)
        log_entries = content.split("\n---\n")
        first_entry = log_entries[0]
        log_data = json.loads(first_entry)

        assert log_data["exception_type"] == "ValueError"
        assert log_data["exception_message"] == "Test error message"
        assert "stack_trace" in log_data
        assert log_data["context"]["test"] == "data"

    def test_log_exception_includes_timestamp(self, tmp_path):
        """Test that logged exception includes ISO timestamp."""
        from code_indexer.utils.exception_logger import ExceptionLogger

        project_root = tmp_path / "test_project"
        project_root.mkdir()

        logger = ExceptionLogger.initialize(project_root, mode="cli")

        before = datetime.now()

        try:
            raise RuntimeError("Timestamp test")
        except RuntimeError as e:
            logger.log_exception(e)

        after = datetime.now()

        with open(logger.log_file_path) as f:  # type: ignore[arg-type]
            content = f.read()

        log_data = json.loads(content.split("\n---\n")[0])

        # Verify timestamp exists and is ISO format
        assert "timestamp" in log_data
        timestamp = datetime.fromisoformat(log_data["timestamp"])

        # Timestamp should be between before and after
        assert before <= timestamp <= after

    def test_log_exception_includes_thread_info(self, tmp_path):
        """Test that logged exception includes thread name and ID."""
        from code_indexer.utils.exception_logger import ExceptionLogger

        project_root = tmp_path / "test_project"
        project_root.mkdir()

        logger = ExceptionLogger.initialize(project_root, mode="cli")

        try:
            raise KeyError("Thread test")
        except KeyError as e:
            logger.log_exception(e, thread_name="TestThread")

        with open(logger.log_file_path) as f:  # type: ignore[arg-type]
            content = f.read()

        log_data = json.loads(content.split("\n---\n")[0])

        assert "thread" in log_data
        assert log_data["thread"] == "TestThread"

    def test_log_exception_includes_stack_trace(self, tmp_path):
        """Test that logged exception includes complete stack trace."""
        from code_indexer.utils.exception_logger import ExceptionLogger

        project_root = tmp_path / "test_project"
        project_root.mkdir()

        logger = ExceptionLogger.initialize(project_root, mode="cli")

        def inner_function():
            raise ZeroDivisionError("Division by zero")

        def outer_function():
            inner_function()

        try:
            outer_function()
        except ZeroDivisionError as e:
            logger.log_exception(e)

        with open(logger.log_file_path) as f:  # type: ignore[arg-type]
            content = f.read()

        log_data = json.loads(content.split("\n---\n")[0])

        # Verify stack trace includes function names
        assert "stack_trace" in log_data
        assert "inner_function" in log_data["stack_trace"]
        assert "outer_function" in log_data["stack_trace"]
        assert "Division by zero" in log_data["stack_trace"]

    def test_multiple_exceptions_appended_to_same_file(self, tmp_path):
        """Test that multiple exceptions are appended with separators."""
        from code_indexer.utils.exception_logger import ExceptionLogger

        project_root = tmp_path / "test_project"
        project_root.mkdir()

        logger = ExceptionLogger.initialize(project_root, mode="cli")

        # Log multiple exceptions
        try:
            raise ValueError("First exception")
        except ValueError as e:
            logger.log_exception(e)

        try:
            raise TypeError("Second exception")
        except TypeError as e:
            logger.log_exception(e)

        try:
            raise RuntimeError("Third exception")
        except RuntimeError as e:
            logger.log_exception(e)

        with open(logger.log_file_path) as f:  # type: ignore[arg-type]
            content = f.read()

        # Split by separator
        entries = content.split("\n---\n")

        # Should have 3 entries (last one may be empty after final separator)
        assert len([e for e in entries if e.strip()]) == 3

        # Verify each entry
        entry1 = json.loads(entries[0])
        assert entry1["exception_type"] == "ValueError"
        assert entry1["exception_message"] == "First exception"

        entry2 = json.loads(entries[1])
        assert entry2["exception_type"] == "TypeError"
        assert entry2["exception_message"] == "Second exception"

        entry3 = json.loads(entries[2])
        assert entry3["exception_type"] == "RuntimeError"
        assert entry3["exception_message"] == "Third exception"


class TestThreadExceptionHook:
    """Test global thread exception handler."""

    def test_install_thread_exception_hook(self, tmp_path):
        """Test that threading.excepthook can be installed globally."""
        from code_indexer.utils.exception_logger import ExceptionLogger

        project_root = tmp_path / "test_project"
        project_root.mkdir()

        logger = ExceptionLogger.initialize(project_root, mode="cli")

        # Install the hook
        original_excepthook = threading.excepthook
        logger.install_thread_exception_hook()

        # Verify hook was installed
        assert threading.excepthook != original_excepthook

        # Restore original
        threading.excepthook = original_excepthook

    def test_thread_exception_captured_and_logged(self, tmp_path):
        """Test that uncaught thread exceptions are captured and logged."""
        from code_indexer.utils.exception_logger import ExceptionLogger

        project_root = tmp_path / "test_project"
        project_root.mkdir()

        logger = ExceptionLogger.initialize(project_root, mode="cli")
        logger.install_thread_exception_hook()

        exception_raised = threading.Event()

        def failing_thread_function():
            try:
                raise ValueError("Uncaught thread exception")
            finally:
                exception_raised.set()

        # Start thread that will raise exception
        thread = threading.Thread(target=failing_thread_function, name="FailingThread")
        thread.start()
        thread.join(timeout=2)

        # Wait for exception to be logged
        assert exception_raised.wait(timeout=2)
        time.sleep(0.1)  # Brief delay for log write

        # Verify exception was logged
        with open(logger.log_file_path) as f:  # type: ignore[arg-type]
            content = f.read()

        assert "ValueError" in content
        assert "Uncaught thread exception" in content
        assert "FailingThread" in content
