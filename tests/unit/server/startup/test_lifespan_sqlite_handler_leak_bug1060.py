"""Bug #1060 regression guard: leaked SQLiteLogHandler on root logger after lifespan shutdown.

Root cause: SQLiteLogHandler is installed on the root logger during lifespan startup but
was never removed on shutdown. After pytest deletes the tmp logs.db dir, subsequent
logger.warning() calls in other tests fail with 'unable to open database file' and drop
silently, masking test failures.

Fix: lifespan shutdown must call logging.getLogger().removeHandler(sqlite_handler) and
sqlite_handler.close(), symmetric with the install in startup. The removal is placed
FIRST in the shutdown section (immediately after yield) so it runs robustly even if
later shutdown steps raise exceptions.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock


_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


class TestLifespanSQLiteHandlerLeakSourceGuard:
    """Source-text guard: lifespan.py must contain the handler-removal on shutdown."""

    def test_handler_removal_present_in_lifespan_source(self):
        """lifespan.py must remove the SQLiteLogHandler from the root logger on shutdown.

        Bug #1060: the handler was installed at startup but never removed on shutdown.
        This caused the handler to remain on the root logger across pytest test sessions.
        When the tmp DB directory was deleted after a test, subsequent log calls in other
        tests failed with 'unable to open database file'.

        The fix must appear AFTER the 'yield' statement (shutdown section), and must
        call removeHandler on the root logger with the sqlite_handler instance.
        """
        source = _LIFESPAN_PATH.read_text()

        # Locate the yield statement -- everything after is shutdown
        yield_pos = source.find("yield  # Server is now running")
        assert yield_pos != -1, (
            "Could not find 'yield  # Server is now running' in lifespan.py"
        )

        shutdown_section = source[yield_pos:]

        # The shutdown must remove the handler from the root logger
        has_remove_handler = (
            "removeHandler" in shutdown_section and "sqlite" in shutdown_section.lower()
        )
        assert has_remove_handler, (
            "Bug #1060: lifespan.py shutdown section does not call removeHandler on the "
            "SQLiteLogHandler. The handler installed at startup is leaked onto the root "
            "logger and will try to write to a deleted DB after the test. "
            "Add: logging.getLogger().removeHandler(sqlite_handler) in the shutdown section."
        )

    def test_handler_close_present_in_lifespan_source(self):
        """lifespan.py must close the SQLiteLogHandler on shutdown.

        A removeHandler call without close() leaves the handler in a half-dismounted
        state. close() must also be called to release any internal resources.
        """
        source = _LIFESPAN_PATH.read_text()

        yield_pos = source.find("yield  # Server is now running")
        assert yield_pos != -1, (
            "Could not find 'yield  # Server is now running' in lifespan.py"
        )

        shutdown_section = source[yield_pos:]

        # Check that .close() is called on the sqlite handler after shutdown
        has_close = "_sqlite_handler.close()" in shutdown_section or (
            "_sqlite_handler" in shutdown_section and ".close()" in shutdown_section
        )
        assert has_close, (
            "Bug #1060: lifespan.py shutdown section does not close the SQLiteLogHandler. "
            "Add: _sqlite_handler.close() after removeHandler() in the shutdown section."
        )

    def test_handler_removal_is_first_action_after_yield(self):
        """The SQLiteLogHandler removal must be the FIRST shutdown action after yield.

        Bug #1060 robust placement: if the removal is at the end of the shutdown
        sequence, an earlier shutdown step that raises unhandled will skip the removal,
        leaking the handler. Placing it immediately after yield (in its own try/except)
        guarantees it runs even on error teardown.
        """
        source = _LIFESPAN_PATH.read_text()

        yield_pos = source.find("yield  # Server is now running")
        assert yield_pos != -1, "Could not find 'yield  # Server is now running'"

        # The section immediately after yield (before any other shutdown comment/code)
        post_yield = source[yield_pos:]

        # Find the first substantive shutdown action (non-yield, non-blank, non-comment lines)
        # The removal of SQLiteLogHandler must appear before other service stops
        remove_pos = post_yield.find("removeHandler")
        mcp_executor_pos = post_yield.find("_mcp_executor.shutdown")
        cluster_stop_pos = post_yield.find("_cluster_services")

        assert remove_pos != -1, (
            "Bug #1060: removeHandler not found in shutdown section of lifespan.py"
        )
        assert remove_pos < mcp_executor_pos or mcp_executor_pos == -1, (
            "Bug #1060: SQLiteLogHandler removal must come BEFORE _mcp_executor.shutdown "
            "in the shutdown section. If _mcp_executor.shutdown raises, the handler leaks."
        )
        # removeHandler must come before cluster service stops
        if cluster_stop_pos != -1:
            assert remove_pos < cluster_stop_pos, (
                "Bug #1060: SQLiteLogHandler removal must come BEFORE cluster service stops."
            )


def _write_minimal_config(server_data_dir: str) -> None:
    """Write a minimal config.json so ServerConfigManager.load_config() returns non-None.

    Without a config.json, load_config() returns None and startup_config.log_level
    raises AttributeError, causing the SQLiteLogHandler install to be skipped entirely
    (caught by the except block). Writing the minimal JSON satisfies the config check
    and allows the handler install branch to execute.
    """
    config_path = Path(server_data_dir) / "config.json"
    config_path.write_text(
        json.dumps({"server_dir": server_data_dir, "log_level": "INFO"})
    )


def _make_minimal_lifespan_deps():
    """Build the minimal set of mock dependencies needed to run make_lifespan.

    Most startup steps are wrapped in try/except and are non-fatal. The handler
    install happens very early (lines ~150-183), and the removal happens
    immediately after yield. We just need enough to get past import-time checks.
    """
    dep = MagicMock()
    dep.user_manager = MagicMock()
    return dict(
        background_job_manager=MagicMock(),
        job_tracker=MagicMock(),
        golden_repo_manager=MagicMock(),
        mcp_registration_service=MagicMock(),
        user_manager=MagicMock(),
        jwt_manager=MagicMock(),
        dependencies=dep,
        register_langfuse_golden_repos=MagicMock(),
        storage_mode="sqlite",
        backend_registry=None,
        latency_tracker=None,
    )


class TestLifespanRealHandlerRemoval:
    """Runtime guard: the real make_lifespan context manager removes SQLiteLogHandler.

    This test drives the ACTUAL lifespan function -- not a simulation. It must FAIL
    if the removeHandler line in lifespan.py is deleted. (Verified: see docstring.)
    """

    def test_sqlite_handler_removed_from_root_logger_after_lifespan_exit(self):
        """After entering and exiting make_lifespan(...), no SQLiteLogHandler remains.

        This drives the real make_lifespan async context manager via asyncio.run().
        The lifespan startup will partially succeed (SQLiteLogHandler installed early)
        and partially fail (many services need real infra -- all wrapped in try/except).
        What matters: after the context exits, the handler must be gone from root logger.

        Fails-against-pre-fix: if the removeHandler call in lifespan.py shutdown is
        deleted, handler_installed will be True (install succeeded) but
        handler_removed will be False (removal skipped), causing the assertion to fail.

        Uses try/finally to guarantee the test never leaks a handler itself.
        """
        import asyncio
        from fastapi import FastAPI
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler
        from code_indexer.server.startup.lifespan import make_lifespan

        root_logger = logging.getLogger()
        handler_installed = False

        with tempfile.TemporaryDirectory() as server_data_dir:
            # Point the lifespan at our tmp dir so logs.db goes there
            os.environ["CIDX_SERVER_DATA_DIR"] = server_data_dir
            # Write a minimal config.json so load_config() returns non-None.
            # Without this, startup_config is None and log_level access raises
            # AttributeError, which is caught and skips the handler install.
            _write_minimal_config(server_data_dir)

            app = FastAPI()
            lifespan_fn = make_lifespan(**_make_minimal_lifespan_deps())

            async def _run():
                nonlocal handler_installed
                async with lifespan_fn(app):
                    # Inside lifespan: check that the handler was actually installed
                    sqlite_handlers = [
                        h
                        for h in root_logger.handlers
                        if isinstance(h, SQLiteLogHandler)
                    ]
                    handler_installed = len(sqlite_handlers) > 0
                # After exit: collect any remaining SQLiteLogHandlers

            try:
                asyncio.run(_run())
            except Exception:
                # Startup failures are expected (many services need real infra).
                # What we care about is the handler removal at the top of shutdown,
                # which runs before any other step that might raise.
                pass
            finally:
                # Always clean up CIDX_SERVER_DATA_DIR env var
                os.environ.pop("CIDX_SERVER_DATA_DIR", None)

            # After the lifespan exits, the root logger must be clean
            remaining_sqlite_handlers = [
                h for h in root_logger.handlers if isinstance(h, SQLiteLogHandler)
            ]

            # Guarantee we clean up even on test failure (never leak a handler)
            for h in remaining_sqlite_handlers:
                root_logger.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass

        assert handler_installed, (
            "SQLiteLogHandler was never installed during lifespan startup. "
            "Check that startup config is readable in the tmp server_data_dir."
        )
        assert len(remaining_sqlite_handlers) == 0, (
            f"Bug #1060: {len(remaining_sqlite_handlers)} SQLiteLogHandler(s) remained on "
            f"root logger after lifespan exit. The lifespan shutdown must call "
            f"logging.getLogger().removeHandler(sqlite_handler) immediately after yield."
        )

    def test_three_lifespan_cycles_do_not_accumulate_handlers(self):
        """3 startup/shutdown cycles must not grow the root logger's handler list.

        If removeHandler is omitted, each cycle accumulates a stale handler pointing
        to a deleted DB. This test catches that accumulation by running 3 cycles.
        """
        import asyncio
        from fastapi import FastAPI
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler
        from code_indexer.server.startup.lifespan import make_lifespan

        root_logger = logging.getLogger()
        handlers_before = set(root_logger.handlers)
        leaked: list = []

        for cycle in range(3):
            with tempfile.TemporaryDirectory() as server_data_dir:
                os.environ["CIDX_SERVER_DATA_DIR"] = server_data_dir
                # Write minimal config so the handler install branch is reached
                _write_minimal_config(server_data_dir)

                app = FastAPI()
                lifespan_fn = make_lifespan(**_make_minimal_lifespan_deps())

                async def _run():
                    async with lifespan_fn(app):
                        pass

                try:
                    asyncio.run(_run())
                except Exception:
                    pass
                finally:
                    os.environ.pop("CIDX_SERVER_DATA_DIR", None)

            # After each cycle, collect any new SQLiteLogHandlers not present before
            new_handlers = [
                h
                for h in root_logger.handlers
                if isinstance(h, SQLiteLogHandler) and h not in handlers_before
            ]
            leaked.extend(new_handlers)

        # Clean up any leaked handlers before asserting
        for h in leaked:
            if h in root_logger.handlers:
                root_logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass

        assert len(leaked) == 0, (
            f"Bug #1060: {len(leaked)} SQLiteLogHandler(s) leaked across 3 lifespan cycles. "
            f"Each lifespan shutdown must removeHandler from the root logger."
        )
