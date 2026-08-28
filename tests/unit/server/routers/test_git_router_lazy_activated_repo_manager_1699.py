"""Bug #1699: routers/git.py module-level eager ActivatedRepoManager()
singleton (which internally constructs a GoldenRepoManager) touches the
live server DB as an import-time side effect.

Discovered as a side effect of Bug #1686's own verification: importing
`routers/inline_routes.py` (which transitively imports `routers/git.py` at
line 48) logs "Loaded 0 golden repos from SQLite" at import time. Confirmed
via strace: a bare `import code_indexer.server.routers.git` opens
~/.cidx-server/data/cidx_server.db (+ -wal/-shm sidecars).

Root cause: `routers/git.py` had

    activated_repo_manager = ActivatedRepoManager(
        data_dir=os.path.join(_server_data_dir, "data") if _server_data_dir else None
    )

running unconditionally at module import time.
`ActivatedRepoManager.__init__` with `golden_repo_manager=None` (the
default, since this call site never passes one) constructs its own
`GoldenRepoManager(data_dir=self.data_dir, resource_config=resource_config)`
-- a real SQLite load -- as a side effect of construction.

Consumer audit performed for this fix (exhaustive grep across src/ AND
tests/ for `routers.git` / bare `activated_repo_manager` references):

  - EXTERNAL consumers: only `routers/inline_routes.py:48` does
    `from ..routers.git import router as git_router` -- it imports the
    `router` APIRouter object only, never the singleton. No other module
    anywhere imports the bare `activated_repo_manager` name from this
    module, and no `startup/lifespan.py` post-hoc wiring touches it either
    (unlike diagnostics_service's Bug #532 injection).
  - INTERNAL consumers: THREE route handlers in this SAME module
    (`git_cat`, `git_blame`, `git_file_history`) read `activated_repo_manager`
    as a bare module global (`LOAD_GLOBAL` bytecode). A module-level
    PEP-562 `__getattr__` (the Bug #1638/#1650 pattern used by
    server/app.py, git_operations_service.py, file_service.py) is NOT
    viable here for the identical reason documented in Bug #1686's fix:
    LOAD_GLOBAL reads `globals()` directly and never triggers a module's
    `__getattr__` -- that hook fires only for external `module.attr`
    access or `from module import attr`.
  - TEST consumers: 6 call sites across test_git_cat_endpoint.py,
    test_git_blame_endpoint.py, test_git_file_history_endpoint.py all use
    `unittest.mock.patch("code_indexer.server.routers.git.activated_repo_manager")`
    (string target, resolved dynamically at patch time, WITHOUT
    `create=True`) -- this works correctly against a real `None` default
    exactly as it did against the diagnostics_service fix.

Fix: identical shape to Bug #1686's `_get_diagnostics_service()` -- a real
`activated_repo_manager: Optional[ActivatedRepoManager] = None` sentinel
plus a `_get_activated_repo_manager()` double-checked-locking getter
(module-level `threading.RLock`, never a plain Lock), which the three
internal handlers call instead of touching the bare name directly.

These tests use a fresh subprocess (mirroring
test_diagnostics_router_lazy_singleton_1686.py and
tests/unit/xray/test_lazy_load.py) so the Python interpreter is clean --
no contamination from other test files having already imported
code_indexer.server.routers.git in-process.
"""

import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

SRC_ROOT = str(Path(__file__).parent.parent.parent.parent.parent / "src")
SUBPROCESS_TIMEOUT_SECONDS = 60
THREAD_JOIN_TIMEOUT_SECONDS = 30


def _make_env(tmp_path: Path, suffix: str) -> dict:
    """Build a subprocess env with CIDX_SERVER_DATA_DIR pointed at an
    isolated fake server directory under tmp_path, so each test observes
    its own on-disk state with no cross-test contamination."""
    fake_server_dir = tmp_path / f"cidx-server-fake-{suffix}"
    return {**os.environ, "CIDX_SERVER_DATA_DIR": str(fake_server_dir)}


def _run(code: str, env: dict) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        env=env,
    )


def _run_and_assert_ok(code: str, env: dict) -> str:
    """Run `code` in a fresh subprocess, assert clean exit, return stdout."""
    result = _run(code, env)
    assert result.returncode == 0, (
        f"Subprocess failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result.stdout


class TestBareImportDoesNotConstructActivatedRepoManager:
    """A bare import of routers/git.py (directly or transitively) must be
    inert -- it must never construct ActivatedRepoManager/GoldenRepoManager
    or touch any on-disk DB file."""

    def test_direct_import_does_not_create_db_file(self, tmp_path) -> None:
        env = _make_env(tmp_path, "direct")
        code = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "import code_indexer.server.routers.git"
        )
        _run_and_assert_ok(code, env)

        db_path = Path(env["CIDX_SERVER_DATA_DIR"]) / "data" / "cidx_server.db"
        assert not db_path.exists(), (
            "BUG #1699: bare import of routers.git constructed "
            f"ActivatedRepoManager/GoldenRepoManager and created a db file at {db_path}"
        )

    def test_transitive_import_via_inline_routes_leaves_manager_none(
        self, tmp_path
    ) -> None:
        """inline_routes.py:48 imports the git router -- this is the exact
        transitive path the issue reports (and the exact path #1686's own
        verification stumbled on).

        Bug #1702 removed the module-level `activated_repo_manager`
        singleton entirely (resolution now goes through app.state at call
        time), so a bare/transitive import must leave NO such attribute
        on the module at all -- not merely leave it set to None.
        """
        env = _make_env(tmp_path, "transitive")
        code = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "import code_indexer.server.routers.inline_routes; "
            "import code_indexer.server.routers.git as m; "
            "print('arm_absent:', not hasattr(m, 'activated_repo_manager'))"
        )
        stdout = _run_and_assert_ok(code, env)
        assert "arm_absent: True" in stdout, (
            "BUG #1699/#1702: transitive import via inline_routes.py left a "
            f"stale activated_repo_manager module attribute. Got: {stdout!r}"
        )

    def test_activated_repo_manager_global_is_none_before_first_real_access(
        self, tmp_path
    ) -> None:
        """Bug #1702 removed the module-level singleton -- a bare import
        must leave no `activated_repo_manager` module attribute at all."""
        env = _make_env(tmp_path, "none-check")
        code = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "import code_indexer.server.routers.git as m; "
            "print('arm_absent:', not hasattr(m, 'activated_repo_manager'))"
        )
        stdout = _run_and_assert_ok(code, env)
        assert "arm_absent: True" in stdout, (
            "BUG #1699/#1702: after a bare import, the module must not have "
            f"a stale activated_repo_manager attribute. Got: {stdout!r}"
        )


class TestBareImportNeverLogsGoldenRepoLoad:
    """The observable symptom from the issue report: a real logging.INFO
    'Loaded N golden repos from SQLite' line must never appear from a bare
    import."""

    def test_bare_import_never_logs_golden_repo_load(self, tmp_path) -> None:
        env = _make_env(tmp_path, "log-check")
        code = (
            "import sys, logging; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "logging.basicConfig(level=logging.INFO); "
            "import code_indexer.server.routers.git"
        )
        result = _run(code, env)
        assert result.returncode == 0, (
            f"Subprocess failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "golden repos from SQLite" not in result.stderr, (
            "BUG #1699: bare import of routers.git triggered a real "
            "golden-repo SQLite load as an import-time side effect. "
            f"stderr: {result.stderr!r}"
        )


class TestExplicitAccessStillConstructsRealManagerCorrectly:
    """Real usage -- a route handler genuinely needing the manager -- must
    still construct a fully-functional ActivatedRepoManager (regression
    guard against an over-broad fix that breaks the feature)."""

    def test_get_activated_repo_manager_constructs_real_instance_on_first_call(
        self, tmp_path
    ) -> None:
        env = _make_env(tmp_path, "explicit")
        expected_data_dir = str(Path(env["CIDX_SERVER_DATA_DIR"]) / "data")
        code = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "import code_indexer.server.routers.git as m; "
            "arm = m._get_activated_repo_manager(); "
            "print('type:', type(arm).__name__); "
            "print('same_instance:', arm is m._get_activated_repo_manager()); "
            "print('data_dir:', arm.data_dir)"
        )
        stdout = _run_and_assert_ok(code, env)
        assert "type: ActivatedRepoManager" in stdout, stdout
        assert "same_instance: True" in stdout, (
            "_get_activated_repo_manager() must return the SAME cached "
            f"singleton on repeated calls, not construct a new one each time. Got: {stdout!r}"
        )
        assert f"data_dir: {expected_data_dir}" in stdout, (
            "Explicit access must construct a real ActivatedRepoManager "
            f"honoring CIDX_SERVER_DATA_DIR ({expected_data_dir}). Got: {stdout!r}"
        )


class TestConcurrentAccessReturnsSameSingleton:
    """Bug #1650-class re-entrancy/thread-safety concern from #1699's fix
    (double-checked-locking construction) no longer applies: Bug #1702
    removed all node-local construction from `_get_activated_repo_manager()`
    -- it is now a pure `getattr(app.state, ...)` read, which is
    inherently safe under concurrent access with no lock required. This
    test proves that property still holds: concurrent readers all observe
    the SAME app.state-resolved instance."""

    def test_concurrent_get_activated_repo_manager_calls_share_one_instance(
        self,
    ) -> None:
        from unittest.mock import MagicMock

        from code_indexer.server import app as app_module
        from code_indexer.server.routers import git as git_router_module

        sentinel_manager = MagicMock(name="sentinel-activated-repo-manager")
        original = getattr(app_module.app.state, "activated_repo_manager", None)
        app_module.app.state.activated_repo_manager = sentinel_manager
        try:
            results_queue: "queue.Queue" = queue.Queue()
            errors_queue: "queue.Queue" = queue.Queue()

            def worker() -> None:
                try:
                    results_queue.put(git_router_module._get_activated_repo_manager())
                except Exception as e:  # pragma: no cover - failure diagnostic
                    errors_queue.put(e)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)

            assert not any(t.is_alive() for t in threads), (
                "A worker thread failed to join within the bounded timeout"
            )
            errors = list(errors_queue.queue)
            assert not errors, f"Worker threads raised: {errors}"
            results = list(results_queue.queue)
            assert len(results) == 8
            assert all(r is sentinel_manager for r in results), (
                "Concurrent _get_activated_repo_manager() calls must all "
                "resolve the SAME app.state singleton."
            )
        finally:
            app_module.app.state.activated_repo_manager = original
