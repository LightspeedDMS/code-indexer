"""Daemon server startup with Unix socket binding.

Provides socket-based atomic lock for single daemon instance per project.
"""

import logging
import signal
import socket
import sys
from pathlib import Path

from rpyc.utils.server import ThreadedServer

from .service import CIDXDaemonService

# Import socket helper for /tmp/cidx socket management (fixes 108-char limit bug)
from code_indexer.config import ConfigManager
from code_indexer.daemon.socket_helper import create_mapping_file, cleanup_old_socket

logger = logging.getLogger(__name__)


def start_daemon(config_path: Path) -> None:
    """Start daemon with socket binding as atomic lock.

    Socket binding provides atomic exclusion - only one daemon can bind
    to a socket at a time. No PID files needed.

    Args:
        config_path: Path to project's .code-indexer/config.json

    Raises:
        SystemExit: If daemon already running or socket binding fails
    """
    # Get socket path using ConfigManager (uses /tmp/cidx/ to avoid 108-char limit)
    config_manager = ConfigManager(config_path)
    socket_path = config_manager.get_socket_path()

    config_dir = config_path.parent
    logger.info(f"Starting CIDX daemon for {config_dir}")
    logger.info(f"Socket path: {socket_path}")

    # Story #1488 Codex Finding 1: acquire the shared repo-scoped index-mutation
    # lock BEFORE binding/serving and hold it for the daemon's serving lifetime,
    # so a chunk migration cannot run while the daemon is able to mutate chunks.
    # The migration's own socket-connect probe covers the other order (a live
    # daemon blocks a migration); this lock closes the remaining race where a
    # daemon STARTS after the migration's one-time probe. Non-blocking: fail
    # CLOSED with a clear message if a migration holds it (never hang). Released
    # in the finally on daemon exit.
    #
    # Story #1488 Codex HIGH (startup-cleanup-before-lock race): the acquisition
    # runs at the VERY TOP -- BEFORE any startup stale-socket cleanup
    # (cleanup_old_socket / _clean_stale_socket). Previously that cleanup ran
    # first, so a daemon B starting while daemon A held the lock and had bound but
    # not yet begun accepting would see connection-refused on A's socket, treat it
    # as stale and UNLINK it, then fail to acquire A's lock -- leaving A serving on
    # an unlinked, unreachable socket while still holding the mutation lock
    # (wedged: indexing / migration / another daemon start all blocked). Acquiring
    # the lock FIRST makes the whole startup-cleanup + bind window mutually
    # exclusive across daemons: a dead daemon auto-released its flock, so a new
    # daemon takes the free lock first, then safely cleans the truly-stale socket
    # and binds -- no second daemon can be in that window concurrently.
    from code_indexer.services.chunk_migration_cli import (
        MigrationLockError,
        acquire_index_mutation_lock,
    )

    index_mutation_lock_ctx = acquire_index_mutation_lock(config_dir)
    try:
        index_mutation_lock_ctx.__enter__()
    except MigrationLockError as exc:
        # Acquire FAILED: nothing was acquired, so we must NOT enter the release
        # try/finally below (there is nothing to release), and -- critically -- we
        # must NOT run the startup socket cleanup, since a failed acquire means
        # another daemon / migration owns this repo and its socket must be left
        # untouched. Fail closed.
        logger.error(f"Cannot start daemon while a chunk migration is running: {exc}")
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    # Story #1488 Codex Finding A: the lock is now HELD. Enter the release
    # try/finally IMMEDIATELY -- so EVERY subsequent step (startup stale-socket
    # cleanup, signal-handler setup, service construction, server bind, chmod,
    # mapping-file creation, and the blocking serve) runs INSIDE it. If ANY of
    # these raises (including SystemExit / KeyboardInterrupt), the finally still
    # releases the lock. Previously signal setup and service construction ran
    # BEFORE this try, so a failure there leaked the lock forever (all future cidx
    # index / migration / daemon start / watch on this repo would fail closed).
    try:
        # Startup stale-socket cleanup -- now runs UNDER the held lock (Codex HIGH
        # race fix above). Any socket present at this point belongs to a daemon
        # that is NOT holding the lock (i.e. a dead daemon's leftover), so cleaning
        # it here can never remove a live, lock-holding daemon's socket.
        # Clean up old socket in .code-indexer/ if it exists (backward compat).
        cleanup_old_socket(config_dir)
        # Clean stale socket if exists.
        _clean_stale_socket(socket_path)

        # Setup signal handlers for graceful shutdown
        _setup_signal_handlers(socket_path)

        # Create shared service instance (shared across all connections)
        # This ensures cache and watch state are shared, not per-connection
        shared_service = CIDXDaemonService()

        # Create and start RPyC server with shared service instance
        try:
            server = ThreadedServer(
                shared_service,  # Pass instance, not class
                socket_path=str(socket_path),
                protocol_config={
                    "allow_public_attrs": True,
                    "allow_pickle": True,
                    "sync_request_timeout": 300,  # 5 min timeout for long ops
                },
            )

            logger.info(f"CIDX daemon listening on {socket_path}")
            print(f"CIDX daemon started on {socket_path}")

            # CRITICAL: Set socket to group-writable for multi-user access
            # Socket needs rw permissions for other users to connect
            import os
            import stat

            os.chmod(socket_path, stat.S_IRWXU | stat.S_IRWXG)  # 770 (rwxrwx---)

            # Create mapping file for debugging (links socket to repo path)
            repo_path = config_path.parent
            create_mapping_file(repo_path, socket_path)

            # Blocks here until shutdown
            server.start()

        except OSError as e:
            if "Address already in use" in str(e):
                logger.error(f"Daemon already running on {socket_path}")
                print(
                    f"ERROR: Daemon already running on {socket_path}",
                    file=sys.stderr,
                )
                sys.exit(1)
            raise

    finally:
        # Story #1488 Codex new-high finding: the daemon MUST remove its own
        # socket file WHILE STILL HOLDING the index-mutation lock, and release
        # the lock only AFTER. Otherwise this race wedges a freshly-started
        # daemon: (1) this daemon's finally releases the lock; (2) a NEW daemon
        # starts, removes the stale socket, acquires the lock, and binds its NEW
        # socket at the same path; (3) this daemon's finally then unlinks the
        # socket -- deleting the NEW daemon's live socket, leaving it
        # serving-but-unreachable while holding the lock. Cleaning up under the
        # lock prevents any other daemon from binding a new socket at this path
        # until this daemon has both stopped serving AND removed its own socket.
        #
        # Nested so the lock release (Story #1488 Codex Finding A: guaranteed on
        # EVERY exit path -- clean shutdown, setup failure, serve failure,
        # SystemExit) still runs even if the socket unlink raises. The acquire
        # context manager's own finally handles fd close / unlock cleanup.
        try:
            # Cleanup socket on exit -- WHILE THE LOCK IS STILL HELD.
            if socket_path.exists():
                socket_path.unlink()
                logger.info(f"Cleaned up socket {socket_path}")
        finally:
            # Release the shared index-mutation lock LAST so a subsequent
            # migration / foreground index / daemon can acquire it.
            index_mutation_lock_ctx.__exit__(None, None, None)


def _clean_stale_socket(socket_path: Path) -> None:
    """Clean stale socket if no daemon is listening.

    Args:
        socket_path: Path to Unix socket

    Raises:
        SystemExit: If daemon is already running
    """
    if not socket_path.exists():
        return

    # Try to connect to see if daemon is actually running
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(socket_path))
        sock.close()

        # Connection succeeded - daemon is running
        logger.error(f"Daemon already running on {socket_path}")
        print(f"ERROR: Daemon already running on {socket_path}", file=sys.stderr)
        sys.exit(1)

    except (ConnectionRefusedError, FileNotFoundError):
        # Connection failed - socket is stale, remove it
        logger.info(f"Removing stale socket {socket_path}")
        socket_path.unlink()
        sock.close()


def _setup_signal_handlers(socket_path: Path) -> None:
    """Setup signal handlers for graceful shutdown.

    Args:
        socket_path: Path to Unix socket to clean up
    """

    def signal_handler(signum, frame):
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, shutting down")
        if socket_path.exists():
            socket_path.unlink()
        sys.exit(0)

    # Handle SIGTERM and SIGINT
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
