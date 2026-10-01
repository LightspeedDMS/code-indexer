"""Story #1488 Codex Finding 1 (daemon-startup race): the cidx daemon must
acquire the SAME repo-scoped ``.index-mutation.lock`` the chunk migration uses,
at startup, and hold it for its serving lifetime.

Rationale: the migration's one-time daemon socket-connect probe races a daemon
that STARTS after the probe. Making the daemon acquire the shared lock at
startup (non-blocking, fail-closed) closes that race in both orders -- a daemon
that finds the lock held by a migration refuses to start with a clear message
(never hangs), and a migration that finds a live daemon fails via its socket
probe.

Real ``fcntl`` locks on a real filesystem -- the lock logic under test is NEVER
mocked. Only the blocking rpyc ``ThreadedServer``, the process-global signal
handler setup, the daemon service, and the debug mapping-file are replaced with
test doubles: they are external infrastructure (the server blocks forever), not
the lock wiring being validated.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import code_indexer.daemon.server as daemon_server
import code_indexer.services.chunk_migration_cli as chunk_migration_cli
from code_indexer.config import ConfigManager
from code_indexer.services.chunk_migration_cli import (
    MigrationLockError,
    acquire_index_mutation_lock,
)


class _ServingReached(Exception):
    """Sentinel: the fake server reached its (non-blocking) serving phase."""


def _make_config(tmp_path: Path) -> Path:
    codebase = tmp_path / "proj"
    codebase.mkdir()
    cfg_path = codebase / ".code-indexer" / "config.json"
    cm = ConfigManager(cfg_path)
    config = cm.create_default_config(codebase)
    cm.save(config)
    return cfg_path


@pytest.fixture
def _neutralize_blocking_infra(monkeypatch):
    """Replace the process-global / blocking infrastructure so ``start_daemon``
    can be driven to completion in-process without setting real signal handlers
    or blocking on a live socket. The lock logic itself is untouched."""
    monkeypatch.setattr(daemon_server, "_setup_signal_handlers", lambda *a, **k: None)
    monkeypatch.setattr(daemon_server, "create_mapping_file", lambda *a, **k: None)
    monkeypatch.setattr(daemon_server, "CIDXDaemonService", lambda *a, **k: object())
    return monkeypatch


def test_daemon_startup_fails_closed_when_migration_holds_lock(
    tmp_path: Path, _neutralize_blocking_infra
) -> None:
    cfg_path = _make_config(tmp_path)
    config_dir = cfg_path.parent

    # A fake server that must NEVER be reached when the lock is held.
    def _never(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("daemon proceeded to serve while migration held lock")

    _neutralize_blocking_infra.setattr(daemon_server, "ThreadedServer", _never)

    # Simulate an in-progress migration holding the shared lock.
    with acquire_index_mutation_lock(config_dir):
        with pytest.raises(SystemExit) as exc_info:
            daemon_server.start_daemon(cfg_path)

    assert exc_info.value.code == 1


def test_daemon_startup_does_not_clean_socket_before_acquiring_lock(
    tmp_path: Path, _neutralize_blocking_infra
) -> None:
    """Codex HIGH (startup-cleanup-before-lock race): the STARTUP stale-socket
    cleanup (``cleanup_old_socket`` / ``_clean_stale_socket``) must run only AFTER
    the shared ``.index-mutation.lock`` is acquired -- never before.

    Race (reproduced by this test's ordering): daemon A acquires the lock and
    binds its socket but has not begun accepting; daemon B starts, its startup
    stale-check sees connection-refused on A's socket, treats it as stale and
    UNLINKS it, THEN fails to acquire A's lock -- leaving A serving on an
    unlinked, unreachable socket while still holding the mutation lock (wedged).

    Here the test itself plays daemon A / an in-progress migration by HOLDING the
    lock, plants a sentinel socket file at the exact daemon socket path, then
    invokes ``start_daemon`` (daemon B). Correct ordering => B acquires the lock
    FIRST, fails closed (SystemExit 1) BEFORE any startup cleanup, so the sentinel
    survives.

    RED (pre-fix): startup ``_clean_stale_socket`` runs before the acquire, so it
    connects to the sentinel (ConnectionRefusedError) and unlinks it -> sentinel
    gone -> assert fails. GREEN (post-fix): acquire runs first and fails closed,
    so the sentinel is never touched.
    """
    cfg_path = _make_config(tmp_path)
    config_dir = cfg_path.parent
    socket_path = ConfigManager(cfg_path).get_socket_path()

    # Plant a sentinel socket file at the daemon socket path. A regular file is
    # sufficient: _clean_stale_socket's AF_UNIX connect() to it raises
    # ConnectionRefusedError, driving it to the stale-socket unlink branch.
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.touch()
    assert socket_path.exists()

    # The blocking server must NEVER be reached when the lock is held.
    def _never(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("daemon proceeded to serve while migration held lock")

    _neutralize_blocking_infra.setattr(daemon_server, "ThreadedServer", _never)

    # Simulate an in-progress migration / live daemon A holding the shared lock.
    with acquire_index_mutation_lock(config_dir):
        with pytest.raises(SystemExit) as exc_info:
            daemon_server.start_daemon(cfg_path)

    assert exc_info.value.code == 1
    assert socket_path.exists(), (
        "start_daemon ran its startup stale-socket cleanup BEFORE acquiring the "
        "index-mutation lock -- it unlinked a socket held by another daemon / "
        "migration. The lock must be acquired first so a failed acquire never "
        "touches another holder's live socket."
    )


def test_lone_daemon_holds_lock_during_serving_and_releases_on_exit(
    tmp_path: Path, _neutralize_blocking_infra
) -> None:
    cfg_path = _make_config(tmp_path)
    config_dir = cfg_path.parent

    class _FakeServer:
        def __init__(self, service, socket_path, protocol_config):
            # Materialize the socket file so the subsequent os.chmod succeeds.
            p = Path(socket_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()

        def start(self):
            # The daemon must be holding the shared lock right now: a
            # migration-style acquire MUST fail closed while we "serve".
            with pytest.raises(MigrationLockError):
                with acquire_index_mutation_lock(config_dir):
                    pass
            raise _ServingReached()

    _neutralize_blocking_infra.setattr(daemon_server, "ThreadedServer", _FakeServer)

    with pytest.raises(_ServingReached):
        daemon_server.start_daemon(cfg_path)

    # After the daemon exits, the lock must be released -- a fresh acquire works.
    with acquire_index_mutation_lock(config_dir):
        assert (config_dir / ".index-mutation.lock").exists()


def test_daemon_removes_own_socket_before_releasing_lock(
    tmp_path: Path, _neutralize_blocking_infra
) -> None:
    """Codex new-high finding: the daemon's own socket cleanup MUST happen WHILE
    the index-mutation lock is still held, and the lock released only AFTER.

    Otherwise this race wedges a freshly-started daemon: (1) the old daemon's
    finally releases the lock; (2) a new daemon starts, removes the stale old
    socket, acquires the lock, binds its NEW socket at the same path; (3) the old
    daemon's finally then unlinks the socket -- deleting the NEW daemon's live
    socket, leaving it serving-but-unreachable while holding the lock.

    This test wraps the REAL lock context manager and records whether the
    daemon's own socket file still exists at the exact moment the lock is
    released. Correct ordering => the socket is already gone at release time.

    RED (pre-fix): the finally releases the lock FIRST, so the socket still
    exists when ``__exit__`` runs (observation is True) -> assert fails.
    GREEN (post-fix): socket removed under the lock, THEN released, so the socket
    is already gone at release time (observation is False).
    """
    cfg_path = _make_config(tmp_path)
    socket_path = ConfigManager(cfg_path).get_socket_path()

    observations: dict[str, bool] = {}

    class _FakeServer:
        def __init__(self, service, socket_path, protocol_config):
            # Materialize the socket file so the subsequent os.chmod succeeds
            # and the daemon has a real socket of its own to clean up.
            p = Path(socket_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()

        def start(self):
            # Graceful shutdown: return normally so start_daemon proceeds into
            # its cleanup finally.
            return None

    _neutralize_blocking_infra.setattr(daemon_server, "ThreadedServer", _FakeServer)

    real_acquire = acquire_index_mutation_lock

    class _RecordingLockCtx:
        """Wraps the REAL lock ctx and records socket existence at release."""

        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            return self._inner.__enter__()

        def __exit__(self, exc_type, exc, tb):
            observations["socket_exists_at_release"] = socket_path.exists()
            return self._inner.__exit__(exc_type, exc, tb)

    def _patched_acquire(config_dir_arg):
        # Real lock logic underneath -- only the release moment is observed.
        return _RecordingLockCtx(real_acquire(config_dir_arg))

    # start_daemon imports acquire_index_mutation_lock locally at call time from
    # this module, so patching the module attribute is picked up.
    _neutralize_blocking_infra.setattr(
        chunk_migration_cli, "acquire_index_mutation_lock", _patched_acquire
    )

    daemon_server.start_daemon(cfg_path)

    assert observations.get("socket_exists_at_release") is False, (
        "daemon released the index-mutation lock while its own socket still "
        "existed -- a newly-started daemon could bind a new socket at this path "
        "and then have it unlinked by this daemon's finally"
    )


def test_daemon_releases_lock_even_when_socket_cleanup_raises(
    tmp_path: Path, _neutralize_blocking_infra
) -> None:
    """The socket cleanup and the lock release must be nested so that a failure
    unlinking the socket STILL releases the lock.

    A real directory is planted at the socket path so the daemon's own
    ``socket_path.unlink()`` raises ``IsADirectoryError`` -- a genuine,
    deterministic cleanup failure (no mocking). The lock must still be released,
    proven by a fresh acquire succeeding afterwards.
    """
    cfg_path = _make_config(tmp_path)
    config_dir = cfg_path.parent
    socket_path = ConfigManager(cfg_path).get_socket_path()

    class _DirServer:
        def __init__(self, service, socket_path, protocol_config):
            # Create a DIRECTORY at the socket path so socket_path.unlink()
            # raises IsADirectoryError during cleanup.
            p = Path(socket_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.mkdir()

        def start(self):
            return None

    _neutralize_blocking_infra.setattr(daemon_server, "ThreadedServer", _DirServer)

    # The failing unlink propagates out of start_daemon; the lock must still be
    # released by the nested finally.
    with pytest.raises(OSError):
        daemon_server.start_daemon(cfg_path)

    assert socket_path.is_dir()  # cleanup genuinely failed (dir still present)

    with acquire_index_mutation_lock(config_dir):
        assert (config_dir / ".index-mutation.lock").exists()


def test_daemon_startup_never_calls_exit_when_lock_enter_fails(
    tmp_path: Path, _neutralize_blocking_infra
) -> None:
    """Codex LOW test-hardening: when the shared-lock context manager's
    ``__enter__`` raises ``MigrationLockError`` (a migration already holds the
    lock), ``start_daemon`` must fail closed WITHOUT ever invoking the CM's
    ``__exit__`` -- nothing was acquired, so there is nothing to release, and
    calling ``__exit__`` on an enter-failed CM is a latent double-release bug.

    The prior acquire-failure test only proved the fail-closed ``SystemExit``;
    it did not directly assert ``__exit__`` was never called. This recording CM
    (whose ``__exit__`` fails if invoked) closes that gap so a regression that
    wrongly enters the release try/finally on an enter-failure is caught.
    """
    cfg_path = _make_config(tmp_path)

    class _RecordingEnterFailsCtx:
        def __init__(self) -> None:
            self.exit_called = False

        def __enter__(self):
            raise MigrationLockError("a chunk migration already holds the lock")

        def __exit__(self, exc_type, exc, tb):
            self.exit_called = True
            raise AssertionError(
                "__exit__ must NEVER run when __enter__ raised -- nothing was "
                "acquired, so there is nothing to release"
            )

    ctx = _RecordingEnterFailsCtx()

    def _never(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("daemon proceeded to serve after a failed lock acquire")

    _neutralize_blocking_infra.setattr(daemon_server, "ThreadedServer", _never)
    # start_daemon imports acquire_index_mutation_lock locally from this module
    # at call time, so patching the module attribute is picked up.
    _neutralize_blocking_infra.setattr(
        chunk_migration_cli, "acquire_index_mutation_lock", lambda _cd: ctx
    )

    with pytest.raises(SystemExit) as exc_info:
        daemon_server.start_daemon(cfg_path)

    assert exc_info.value.code == 1
    assert ctx.exit_called is False, (
        "start_daemon called __exit__ on a context manager whose __enter__ "
        "raised -- an enter-failed CM must never be exited"
    )


class _PostAcquireSetupBoom(Exception):
    """Sentinel: a post-acquisition setup step failed."""


def test_daemon_startup_releases_lock_when_post_acquire_setup_fails(
    tmp_path: Path, _neutralize_blocking_infra
) -> None:
    """Codex Finding A: a failure in a post-acquisition setup step (signal-handler
    setup, service construction, socket bind) must NOT leak the shared
    ``.index-mutation.lock``. The lock is acquired at startup and, if any step
    after acquisition raises, it must still be released -- otherwise every future
    ``cidx index`` / migration / daemon start on that repo fails closed forever.

    Injects a failure in ``_setup_signal_handlers`` (a real post-acquisition step)
    and asserts a fresh acquire succeeds after ``start_daemon`` fails -- i.e. the
    lock was released despite the setup failure.
    """
    cfg_path = _make_config(tmp_path)
    config_dir = cfg_path.parent

    def _boom(*a, **k):
        raise _PostAcquireSetupBoom("signal-handler setup failed after lock acquire")

    # Override the fixture's no-op with a raiser: this step runs AFTER the lock
    # is acquired. The blocking server must never be reached.
    _neutralize_blocking_infra.setattr(daemon_server, "_setup_signal_handlers", _boom)

    def _never(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("reached ThreadedServer after a setup failure")

    _neutralize_blocking_infra.setattr(daemon_server, "ThreadedServer", _never)

    # Retain the propagated exception so its ``__traceback__`` keeps
    # ``start_daemon``'s frame -- and therefore the acquired generator-based lock
    # context manager referenced by its locals -- ALIVE. This is exactly Codex
    # Finding A's real trigger ("the traceback frame retains the context
    # manager"): without an explicit release in a finally, CPython refcounting
    # cannot collect the paused generator and run its own funlock/close, so the
    # OS flock stays held. (A naive same-process re-acquire would otherwise be
    # masked by immediate GC of the leaked generator.)
    leaked_exc = None
    try:
        daemon_server.start_daemon(cfg_path)
    except _PostAcquireSetupBoom as exc:
        leaked_exc = exc  # keeps exc.__traceback__ -> start_daemon frame -> lock CM

    assert leaked_exc is not None
    assert leaked_exc.__traceback__ is not None  # frames (and the lock CM) kept alive

    # With the traceback still referenced, the leaked lock can only be free if the
    # code released it EXPLICITLY on the failure path. RED (pre-fix): setup ran
    # before the lock's try/finally, so the lock is still held and this acquire
    # raises MigrationLockError. GREEN (post-fix): finally released it, so this
    # succeeds.
    with acquire_index_mutation_lock(config_dir):
        assert (config_dir / ".index-mutation.lock").exists()
