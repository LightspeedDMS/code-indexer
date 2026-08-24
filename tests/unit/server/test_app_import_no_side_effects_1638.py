"""Bug #1638: importing code_indexer.server.app must not run full service init.

Prior to the fix, `server/app.py` had a module-level statement
`app = create_app()` that ran unconditionally at import time. Any code that
merely imports `code_indexer.server.app` -- or a module that transitively
imports it, e.g. `code_indexer.server.mcp.handlers._utils` (imported by
`code_indexer.server.mcp.handlers.search`) via
`from code_indexer.server import app as app_module` -- triggered a real
`ConfigService` load, SQLite golden-repo enumeration,
`DependencyLatencyTracker` startup, `MCPSelfRegistrationService` singleton
registration, and contention for the live server's `primary_instance.lock`
file, with no explicit opt-in.

These tests use a fresh subprocess (mirroring the established pattern in
tests/unit/xray/test_lazy_load.py) so the Python interpreter is clean --
no contamination from other test files having already imported
code_indexer.server.app in-process.
"""

import subprocess
import sys
from pathlib import Path

SRC_ROOT = str(Path(__file__).parent.parent.parent.parent / "src")
SUBPROCESS_TIMEOUT_SECONDS = 60


def _run_and_assert_ok(code: str) -> str:
    """Run `code` in a fresh subprocess, assert clean exit, return stdout."""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, (
        f"Subprocess failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result.stdout


class TestBareImportDoesNotConstructApp:
    """A bare import of app.py (directly or transitively) must be inert."""

    def test_direct_module_import_does_not_construct_app(self) -> None:
        """`from code_indexer.server import app as app_module` must not
        construct the FastAPI app as an import-time side effect.

        This mirrors mcp/handlers/_utils.py's real import statement.
        """
        code = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "from code_indexer.server import app as app_module; "
            "print('app_in_dict:', 'app' in vars(app_module))"
        )
        stdout = _run_and_assert_ok(code)
        assert "app_in_dict: False" in stdout, (
            "BUG #1638: importing code_indexer.server.app as a module "
            "(without touching the `app` attribute) constructed the FastAPI "
            f"app eagerly. Subprocess output: {stdout!r}"
        )

    def test_transitive_import_via_search_handler_does_not_construct_app(
        self,
    ) -> None:
        """Importing mcp.handlers.search (which imports app.py transitively
        via mcp.handlers._utils) must not construct the FastAPI app.

        This is the exact reproduction reported in the bug: a bare
        `import code_indexer.server.mcp.handlers.search` in a clean
        interpreter used to run full service initialization.
        """
        code = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "import code_indexer.server.mcp.handlers.search; "
            "import code_indexer.server.app as app_module; "
            "print('app_in_dict:', 'app' in vars(app_module))"
        )
        stdout = _run_and_assert_ok(code)
        assert "app_in_dict: False" in stdout, (
            "BUG #1638: a bare `import code_indexer.server.mcp.handlers.search` "
            "transitively constructed the FastAPI app (ran create_app()/"
            f"initialize_services()) as an import-time side effect. "
            f"Subprocess output: {stdout!r}"
        )

    def test_transitive_import_does_not_register_mcp_self_registration_singleton(
        self,
    ) -> None:
        """The empirically-observed symptom from the bug report: a bare
        import must not set MCPSelfRegistrationService's singleton instance.
        """
        code = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "import code_indexer.server.mcp.handlers.search; "
            "from code_indexer.server.services.mcp_self_registration_service import "
            "MCPSelfRegistrationService; "
            "print('instance_is_none:', MCPSelfRegistrationService.get_instance() is None)"
        )
        stdout = _run_and_assert_ok(code)
        assert "instance_is_none: True" in stdout, (
            "BUG #1638: a bare import registered a real "
            "MCPSelfRegistrationService singleton instance as a side effect "
            f"of merely importing search handlers. Output: {stdout!r}"
        )


class TestExplicitAccessStillInitializesCorrectly:
    """Real usage -- explicit access to `app` or a `create_app()` call --
    must still fully and correctly initialize every service (regression
    guard for the production uvicorn entrypoint)."""

    def test_explicit_app_attribute_access_constructs_full_fastapi_app(
        self,
    ) -> None:
        """Accessing `app_module.app` (what `uvicorn code_indexer.server.app:app`
        effectively does) must still construct a fully-wired FastAPI app.
        """
        code = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "from code_indexer.server import app as app_module; "
            "real_app = app_module.app; "
            "from fastapi import FastAPI; "
            "print('is_fastapi:', isinstance(real_app, FastAPI)); "
            "print('has_golden_repo_manager:', "
            "getattr(real_app.state, 'golden_repo_manager', None) is not None); "
            "print('module_golden_repo_manager_set:', "
            "app_module.golden_repo_manager is not None)"
        )
        stdout = _run_and_assert_ok(code)
        assert "is_fastapi: True" in stdout
        assert "has_golden_repo_manager: True" in stdout
        assert "module_golden_repo_manager_set: True" in stdout

    def test_create_app_called_directly_still_fully_initializes(self) -> None:
        """Tests/tooling that call create_app() explicitly (the established
        pattern used by 100+ existing tests) must be completely unaffected.
        """
        code = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "from code_indexer.server.app import create_app; "
            "from fastapi import FastAPI; "
            "app = create_app(); "
            "print('is_fastapi:', isinstance(app, FastAPI)); "
            "print('has_jwt_manager:', "
            "getattr(app.state, 'jwt_manager', None) is not None)"
        )
        stdout = _run_and_assert_ok(code)
        assert "is_fastapi: True" in stdout
        assert "has_jwt_manager: True" in stdout

    def test_from_import_of_manager_global_still_lazily_initializes(self) -> None:
        """`from code_indexer.server.app import golden_repo_manager` (the
        pattern used internally e.g. by services/dashboard_service.py) must
        still resolve to a real, fully-initialized manager instance -- it
        is an explicit reference to the service, not a bare import.
        """
        code = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "from code_indexer.server.app import golden_repo_manager; "
            "print('manager_is_none:', golden_repo_manager is None)"
        )
        stdout = _run_and_assert_ok(code)
        assert "manager_is_none: False" in stdout
