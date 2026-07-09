"""Wiring guard: lifespan installs async QueueHandler/QueueListener logging.

Performance follow-up to Bug #1078 (py-spy: the per-Handler lock was the query
throughput ceiling). The server root logger must be routed through a single
QueueHandler whose QueueListener owns the real handlers (SQLiteLogHandler,
console StreamHandler). The listener must be started at startup and STOPPED
(drained + flushed) on shutdown so no logs are lost on a clean shutdown.

These are source-text + source-order guards mirroring the existing lifespan
wiring-test pattern (e.g. test_lifespan_clone_backend_wiring_bug1044.py).
"""

from __future__ import annotations

from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


class TestAsyncLoggingStartupWiring:
    """Startup must install the QueueHandler/QueueListener."""

    def test_install_queue_logging_called_in_startup(self) -> None:
        source = _LIFESPAN_PATH.read_text()
        yield_pos = source.find("yield  # Server is now running")
        assert yield_pos != -1
        startup_section = source[:yield_pos]
        assert "install_queue_logging" in startup_section, (
            "lifespan startup must call install_queue_logging(...) to route the "
            "root logger through a single QueueHandler (py-spy logging-lock fix)."
        )

    def test_listener_stored_on_app_state(self) -> None:
        source = _LIFESPAN_PATH.read_text()
        assert "app.state.log_queue_listener" in source, (
            "lifespan must store the QueueListener on app.state.log_queue_listener "
            "so shutdown (and tests) can stop/flush it."
        )

    def test_install_after_sqlite_handler_attached(self) -> None:
        """install_queue_logging must run AFTER the SQLiteLogHandler is created.

        The listener must own the SQLiteLogHandler, so the handler has to exist
        before the install call moves it behind the listener.
        """
        source = _LIFESPAN_PATH.read_text()
        sqlite_pos = source.find("SQLiteLogHandler(log_db_path)")
        install_pos = source.find("install_queue_logging")
        assert sqlite_pos != -1
        assert install_pos != -1
        assert sqlite_pos < install_pos, (
            "install_queue_logging must be called AFTER SQLiteLogHandler is "
            "constructed so the listener owns it."
        )


class TestAsyncLoggingShutdownWiring:
    """Shutdown must stop/flush the listener so queued logs are not lost."""

    def test_listener_stopped_in_shutdown(self) -> None:
        source = _LIFESPAN_PATH.read_text()
        yield_pos = source.find("yield  # Server is now running")
        assert yield_pos != -1
        shutdown_section = source[yield_pos:]
        assert "log_queue_listener" in shutdown_section and (
            ".stop()" in shutdown_section
            or "shutdown_queue_logging" in shutdown_section
        ), (
            "lifespan shutdown must stop the QueueListener (drains + flushes "
            "queued records) so no logs are lost on clean shutdown."
        )

    def test_listener_stop_is_among_first_shutdown_actions(self) -> None:
        """Stopping the listener must precede MCP executor shutdown.

        The listener owns the SQLiteLogHandler; draining it early (before steps
        that might raise) preserves the Bug #1060 robustness placement.
        """
        source = _LIFESPAN_PATH.read_text()
        yield_pos = source.find("yield  # Server is now running")
        assert yield_pos != -1
        post_yield = source[yield_pos:]
        listener_stop_pos = post_yield.find("log_queue_listener")
        mcp_executor_pos = post_yield.find("_mcp_executor.shutdown")
        assert listener_stop_pos != -1
        assert listener_stop_pos < mcp_executor_pos or mcp_executor_pos == -1, (
            "QueueListener stop must come BEFORE _mcp_executor.shutdown so a "
            "later raising step cannot skip the log drain."
        )
