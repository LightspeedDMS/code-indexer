"""Bug #1686: routers/diagnostics.py module-level eager DiagnosticsService()
singleton touches the live server DB as an import-time side effect.

Prior to the fix, `routers/diagnostics.py` had:

    diagnostics_service = DiagnosticsService()

running unconditionally at module import time. DiagnosticsService.__init__
with no db_path resolves to CIDX_SERVER_DATA_DIR (or
~/.cidx-server/data/cidx_server.db by default) and calls
_load_results_from_db() -- a real SQLite read -- as a side effect of
construction. Since routers/inline_routes.py imports this router, any test
collection that transitively imports it (even a bare pytest --collect-only)
read the live DB. Confirmed via strace: bare `import
...routers.diagnostics` -> 1 open (plus -wal/-shm sidecars).

Unlike the established Bug #1638/#1650 fix pattern (server/app.py,
git_operations_service.py, file_service.py) where the only consumers are
EXTERNAL modules, this router's OWN route handlers reference
`diagnostics_service` as a bare module global. A module-level PEP-562
`__getattr__` never fires for a bare-name global lookup performed by code
that lives inside the same module (`LOAD_GLOBAL` reads `globals()`
directly), so pure `__getattr__` deferral is not viable here. The fix
instead uses a `None`-sentinel module global plus a `_get_diagnostics_service()`
lazy-construct-and-cache getter (guarded by a class-independent module-level
`threading.RLock`), which every route handler in this module calls instead
of referencing the bare name directly. This shape also keeps
`unittest.mock.patch("...routers.diagnostics.diagnostics_service")` (used
without `create=True` in test_diagnostics_router.py) working correctly:
patching a `None` default via setattr/delattr never raises AttributeError
and never forces real construction at patch time.

These tests use a fresh subprocess (mirroring the established pattern in
tests/unit/xray/test_lazy_load.py and test_app_import_no_side_effects_1638.py)
so the Python interpreter is clean -- no contamination from other test files
having already imported code_indexer.server.routers.diagnostics in-process.
"""

import os
import subprocess
import sys
import threading
from pathlib import Path

SRC_ROOT = str(Path(__file__).parent.parent.parent.parent.parent / "src")
SUBPROCESS_TIMEOUT_SECONDS = 60


def _run_and_assert_ok(code: str, env: dict) -> str:
    """Run `code` in a fresh subprocess, assert clean exit, return stdout."""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        env=env,
    )
    assert result.returncode == 0, (
        f"Subprocess failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result.stdout


class TestBareImportDoesNotConstructDiagnosticsService:
    """A bare import of routers/diagnostics.py (directly or transitively)
    must be inert -- it must never construct DiagnosticsService or touch
    any on-disk DB file."""

    def test_direct_import_does_not_create_db_file(self, tmp_path) -> None:
        fake_server_dir = tmp_path / "cidx-server-fake-direct"
        env = {**os.environ, "CIDX_SERVER_DATA_DIR": str(fake_server_dir)}
        code = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "import code_indexer.server.routers.diagnostics"
        )
        _run_and_assert_ok(code, env)

        db_path = fake_server_dir / "data" / "cidx_server.db"
        assert not db_path.exists(), (
            "BUG #1686: bare import of routers.diagnostics constructed "
            f"DiagnosticsService and created a db file at {db_path}"
        )

    def test_transitive_import_via_inline_routes_leaves_diagnostics_service_none(
        self, tmp_path
    ) -> None:
        """inline_routes.py:54 imports the diagnostics router -- this is
        the exact transitive path the issue reports.

        Asserts the diagnostics-specific fact (module attribute stays None)
        rather than "no db file was created anywhere", because inline_routes
        transitively imports many OTHER routers with their own pre-existing,
        out-of-scope eager singletons (confirmed live: GoldenRepoManager logs
        "Loaded 0 golden repos from SQLite" purely from importing
        inline_routes.py, unrelated to DiagnosticsService) that independently
        create/touch the same shared cidx_server.db file for unrelated
        tables. That is a separate pre-existing defect class, not part of
        Bug #1686's scope -- this test must isolate the diagnostics
        singleton's own behavior from that noise.
        """
        fake_server_dir = tmp_path / "cidx-server-fake-transitive"
        env = {**os.environ, "CIDX_SERVER_DATA_DIR": str(fake_server_dir)}
        code = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "import code_indexer.server.routers.inline_routes; "
            "import code_indexer.server.routers.diagnostics as m; "
            "print('svc_is_none:', getattr(m, 'diagnostics_service', 'MISSING') is None)"
        )
        stdout = _run_and_assert_ok(code, env)
        assert "svc_is_none: True" in stdout, (
            "BUG #1686: transitive import via inline_routes.py constructed "
            f"DiagnosticsService as a side effect. Got: {stdout!r}"
        )

    def test_diagnostics_service_global_is_none_before_first_real_access(
        self, tmp_path
    ) -> None:
        fake_server_dir = tmp_path / "cidx-server-fake-none-check"
        env = {**os.environ, "CIDX_SERVER_DATA_DIR": str(fake_server_dir)}
        code = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "import code_indexer.server.routers.diagnostics as m; "
            "print('svc_is_none:', getattr(m, 'diagnostics_service', 'MISSING') is None)"
        )
        stdout = _run_and_assert_ok(code, env)
        assert "svc_is_none: True" in stdout, (
            "BUG #1686: after a bare import, the module-level "
            f"diagnostics_service global must be None, not constructed. Got: {stdout!r}"
        )


class TestExplicitAccessStillConstructsRealServiceCorrectly:
    """Real usage -- a route handler genuinely needing the service -- must
    still construct a fully-functional DiagnosticsService (regression guard
    against an over-broad fix that breaks the feature)."""

    def test_get_diagnostics_service_constructs_real_instance_on_first_call(
        self, tmp_path
    ) -> None:
        fake_server_dir = tmp_path / "cidx-server-fake-explicit"
        env = {**os.environ, "CIDX_SERVER_DATA_DIR": str(fake_server_dir)}
        expected_db_path = str(fake_server_dir / "data" / "cidx_server.db")
        code = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "import code_indexer.server.routers.diagnostics as m; "
            "svc = m._get_diagnostics_service(); "
            "print('type:', type(svc).__name__); "
            "print('same_instance:', svc is m._get_diagnostics_service()); "
            "print('db_path:', svc._db_path); "
            "status = svc.get_status(); "
            "print('status_type:', type(status).__name__); "
            "print('status_categories:', len(status))"
        )
        stdout = _run_and_assert_ok(code, env)
        assert "type: DiagnosticsService" in stdout, stdout
        assert "same_instance: True" in stdout, (
            "_get_diagnostics_service() must return the SAME cached "
            f"singleton on repeated calls, not construct a new one each time. Got: {stdout!r}"
        )
        assert f"db_path: {expected_db_path}" in stdout, (
            "Explicit access must construct a real DiagnosticsService "
            f"honoring CIDX_SERVER_DATA_DIR ({expected_db_path}). Got: {stdout!r}"
        )
        assert "status_type: dict" in stdout, (
            f"get_status() must return a real dict. Got: {stdout!r}"
        )
        assert "status_categories: 0" not in stdout, (
            "Explicit access must construct a real, working "
            f"DiagnosticsService that returns real category statuses. Got: {stdout!r}"
        )


class TestConcurrentAccessReturnsSameSingleton:
    """Bug #1650-class re-entrancy/thread-safety guard: concurrent first
    callers must never construct two distinct instances (double-checked
    locking correctness)."""

    def test_concurrent_get_diagnostics_service_calls_share_one_instance(
        self, tmp_path, monkeypatch
    ) -> None:
        fake_dir = tmp_path / "cidx-server-fake-concurrent"
        monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(fake_dir))

        from code_indexer.server.routers import diagnostics as diag_module

        # Reset module-level singleton to simulate fresh (unconstructed)
        # state for this test; monkeypatch restores the original value
        # (whatever it was) automatically at teardown.
        monkeypatch.setattr(diag_module, "diagnostics_service", None)

        results = []
        errors = []

        def worker() -> None:
            try:
                results.append(diag_module._get_diagnostics_service())
            except Exception as e:  # pragma: no cover - failure diagnostic
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not any(t.is_alive() for t in threads), (
            "A worker thread failed to join within the bounded timeout "
            "(possible deadlock in _get_diagnostics_service())"
        )
        assert not errors, f"Worker threads raised: {errors}"
        assert len(results) == 8
        first = results[0]
        assert all(r is first for r in results), (
            "Concurrent first-access calls to _get_diagnostics_service() "
            "must all observe the SAME singleton instance, never construct "
            "distinct ones."
        )
