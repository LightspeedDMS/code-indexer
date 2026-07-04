"""Story #1293: MCP correlation_id WRONG-IMPORT bug fix.

mcp/handlers/search.py imported get_correlation_id from
code_indexer.server.middleware.correlation, whose CorrelationContextMiddleware
is NEVER registered in startup/app_wiring.py (only
telemetry.correlation_bridge.CorrelationBridgeMiddleware is). That means the
unwired reader's ContextVar is NEVER set in production -- get_correlation_id()
always returns None there, so every search_event_log / search_embed_event row
written from mcp/handlers/search.py silently carried correlation_id=None.

The fix: mcp/handlers/search.py must use the WIRED reader,
telemetry.correlation_bridge.get_current_correlation_id.
"""


class TestMcpSearchHandlerUsesWiredCorrelationIdReader:
    def test_get_correlation_id_is_the_wired_telemetry_reader(self):
        from code_indexer.server.mcp.handlers import search as search_handlers
        from code_indexer.server.telemetry.correlation_bridge import (
            get_current_correlation_id,
        )

        assert search_handlers.get_correlation_id is get_current_correlation_id, (
            "mcp/handlers/search.py must bind get_correlation_id to the WIRED "
            "telemetry.correlation_bridge reader (whose middleware IS "
            "registered in app_wiring.py), not the unwired "
            "middleware.correlation reader whose middleware is never added."
        )

    def test_get_correlation_id_returns_set_value_via_wired_contextvar(self):
        """End-to-end proof: setting the WIRED context var makes
        search.get_correlation_id() see it (confirming it reads the SAME
        ContextVar the real CorrelationBridgeMiddleware populates)."""
        from code_indexer.server.mcp.handlers import search as search_handlers
        from code_indexer.server.telemetry.correlation_bridge import (
            set_current_correlation_id,
            _correlation_id_var,
        )

        token = _correlation_id_var.set(None)
        try:
            set_current_correlation_id("wired-test-corr-id")
            assert search_handlers.get_correlation_id() == "wired-test-corr-id"
        finally:
            _correlation_id_var.reset(token)


class TestHandlersPackageReExportAlsoWired:
    """The handlers package __init__.py also imports get_correlation_id (for
    _ForwardingModule mock-patch compatibility -- see its docstring). It must
    ALSO bind to the wired reader.

    Root cause of a real cross-test pollution bug found during full-suite
    verification: handlers/__init__.py installs a _ForwardingModule whose
    __setattr__ mirrors every attribute write into ANY submodule (including
    search.py) that also defines a same-named attribute. Any test that does
    ``patch("code_indexer.server.mcp.handlers.get_correlation_id", ...)``
    forwards the MOCK into search.py's binding during the patch, and on
    __exit__ restores it to handlers/__init__.py's ORIGINAL value -- which,
    before this fix, was the WRONG middleware.correlation function. That
    silently re-corrupted search.py's (already fixed) binding for the rest of
    the pytest process. Fixing handlers/__init__.py's own import closes the
    loop: the "original" value the forwarder restores is now the wired one.
    """

    def test_handlers_package_get_correlation_id_is_wired(self):
        import code_indexer.server.mcp.handlers as handlers_pkg
        from code_indexer.server.telemetry.correlation_bridge import (
            get_current_correlation_id,
        )

        assert handlers_pkg.get_correlation_id is get_current_correlation_id, (
            "handlers/__init__.py must also bind get_correlation_id to the "
            "wired telemetry.correlation_bridge reader -- otherwise the "
            "_ForwardingModule shim re-corrupts search.py's binding back to "
            "the wrong function whenever any test patches "
            "'handlers.get_correlation_id' and lets it restore."
        )

    def test_patch_restore_cycle_on_handlers_pkg_does_not_corrupt_search(self):
        """Regression guard for the exact pollution scenario discovered: a
        patch()/restore cycle on handlers.get_correlation_id (as done by
        unrelated tests elsewhere, e.g. audit-log tests in cicd.py) must
        leave search.py's get_correlation_id correctly wired afterward."""
        from unittest.mock import patch

        import code_indexer.server.mcp.handlers.search as search_handlers
        from code_indexer.server.telemetry.correlation_bridge import (
            get_current_correlation_id,
        )

        assert search_handlers.get_correlation_id is get_current_correlation_id

        with patch(
            "code_indexer.server.mcp.handlers.get_correlation_id",
            return_value="mocked-during-patch",
        ):
            # During the patch, forwarding propagates the mock -- expected.
            assert search_handlers.get_correlation_id() == "mocked-during-patch"

        # After restore, search.py's binding must be the WIRED reader again,
        # not the stale wrong function the forwarder used to restore.
        assert search_handlers.get_correlation_id is get_current_correlation_id, (
            "patch()/restore on handlers.get_correlation_id corrupted "
            "search.py's binding -- handlers/__init__.py's own import must "
            "also be the wired reader so the _ForwardingModule restores the "
            "CORRECT function, not the wrong middleware.correlation one."
        )
