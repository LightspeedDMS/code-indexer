"""Centralized exception logger for CIDX.

Provides global exception logging with full debugging context including:
- Timestamp and process ID-based log files
- Complete stack traces
- Thread information
- Command context (for git operations)
- Mode-specific log file paths (CLI/Daemon vs Server)
"""

import json
import os
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

_VERSIONED_SEGMENT = ".versioned"


def _server_log_dir() -> Path:
    """Server-mode log directory.

    Honors CIDX_SERVER_DATA_DIR (Bug #1776) so an isolated/test server
    instance never leaks log files into the real ~/.cidx-server/ directory;
    falls back to Path.home()/.cidx-server, matching the established pattern
    used across other server-mode modules (e.g. health_service.py,
    diagnostics_service.py).
    """
    server_data_dir = Path(
        os.environ.get("CIDX_SERVER_DATA_DIR", str(Path.home() / ".cidx-server"))
    )
    return server_data_dir / "logs"


def _is_inside_versioned_snapshot(path: Path) -> bool:
    """True when *path* lies in (or is) a ``.versioned`` snapshot tree.

    Rule: any component of the absolute path is ``.versioned``. This is a
    superset of the canonical predicate's shape (``.../.versioned/{ns}/v_<ts>``
    in server/storage/shared/snapshot_paths.is_versioned_snapshot), so every
    canonical snapshot and everything below it is covered. It is kept here,
    pure and import-free, because utils/ is on the CLI start-up path and must
    not import the server package. Paths reached only through a symlink, and
    the mount-point-dependent legacy shapes (unknowable to a CLI process),
    are not detected. No filesystem access.
    """
    return _VERSIONED_SEGMENT in Path(os.path.abspath(path)).parts


class ExceptionLogger:
    """Centralized exception logging facility.

    Logs all exceptions with full context to timestamped log files.
    Supports CLI, Daemon, and Server modes with appropriate log file locations.
    """

    _instance: Optional["ExceptionLogger"] = None
    log_file_path: Optional[Path] = None

    def __init__(self, log_file_path: Path):
        """Initialize exception logger with specific log file path.

        Args:
            log_file_path: Path to the log file for writing exceptions
        """
        self.log_file_path = log_file_path
        # Serializes each complete entry (Bug #2060): concurrent threads
        # sharing this logger must never interleave their writes.
        self._write_lock = threading.Lock()

    @classmethod
    def initialize(cls, project_root: Path, mode: str = "cli") -> "ExceptionLogger":
        """Initialize the global exception logger (idempotent singleton).

        Creates log file with timestamp and PID in the filename for uniqueness.

        WARNING: This is a singleton. If already initialized, returns the existing
        instance rather than creating a new one. Tests should manually reset
        cls._instance = None if they need fresh instances.

        Args:
            project_root: Root directory of the project
                         Note: Ignored in server mode (uses CIDX_SERVER_DATA_DIR
                         env var when set, else ~/.cidx-server/logs)
            mode: Operating mode - "cli", "daemon", or "server"

        Returns:
            Initialized ExceptionLogger instance (singleton)
        """
        # If already initialized, return existing instance (idempotent)
        if cls._instance is not None:
            return cls._instance

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        pid = os.getpid()

        if mode == "server" or _is_inside_versioned_snapshot(project_root):
            # Server mode -- and any CLI/daemon run from inside an immutable
            # .versioned snapshot (the server runs cidx there, e.g. for
            # multi-repo regex queries; Bug #2060), which must never be
            # written to.
            log_dir = _server_log_dir()
        else:
            # CLI/Daemon mode: <project>/.code-indexer/
            log_dir = project_root / ".code-indexer"

        # Log file path with timestamp and PID. Neither the directory nor the
        # file is created here (Bug #2060): log_exception creates them on the
        # first write, so a run without an exception writes nothing -- in
        # particular nothing inside a .versioned/ snapshot the server runs
        # cidx from.
        log_file_path = log_dir / f"error_{timestamp}_{pid}.log"

        # Create the instance
        instance = cls(log_file_path)

        # Store as singleton
        cls._instance = instance

        return instance

    @classmethod
    def get_instance(cls) -> Optional["ExceptionLogger"]:
        """Get the current exception logger instance.

        Returns:
            Current ExceptionLogger instance or None if not initialized
        """
        return cls._instance

    def log_exception(
        self,
        exception: Exception,
        thread_name: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log an exception with full context.

        Args:
            exception: The exception to log
            thread_name: Name of the thread where exception occurred (optional)
            context: Additional context data to include in log (optional)
        """
        if not self.log_file_path:
            return  # Logger not initialized

        try:
            # Everything that renders the exception is inside the guard: a
            # broken __str__ or traceback must not escape (Bug #2060).
            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "thread": thread_name or threading.current_thread().name,
                "exception_type": type(exception).__name__,
                "exception_message": str(exception),
                "stack_trace": traceback.format_exc(),
                "context": context or {},
            }
            entry_text = json.dumps(log_entry, indent=2) + "\n---\n"
            # One complete entry at a time (Bug #2060); the first write
            # creates the directory and file (lazy).
            with self._write_lock:
                self.log_file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.log_file_path, "a") as f:
                    f.write(entry_text)
        except Exception as log_error:
            # Never raise from here: that would replace the exception being
            # logged. Report both types only -- no paths or messages, which
            # can carry sensitive detail.
            try:
                print(
                    f"cidx: could not write the error log "
                    f"({type(log_error).__name__}) while logging "
                    f"{type(exception).__name__}",
                    file=sys.stderr,
                )
            except Exception:
                # stderr itself unusable: nothing left to report to. Only
                # Exception -- KeyboardInterrupt/SystemExit still propagate.
                pass

    def install_thread_exception_hook(self) -> None:
        """Install global thread exception handler.

        Sets up threading.excepthook to capture uncaught exceptions in threads.
        """

        def global_thread_exception_handler(args):
            """Handle uncaught thread exceptions."""
            self.log_exception(
                exception=args.exc_value,
                thread_name=args.thread.name,
                context={
                    "exc_type": args.exc_type.__name__,
                    "thread_identifier": args.thread.ident,
                },
            )

        # Install the hook globally
        threading.excepthook = global_thread_exception_handler
