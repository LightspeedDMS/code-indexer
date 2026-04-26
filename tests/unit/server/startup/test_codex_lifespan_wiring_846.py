"""
Unit tests asserting that lifespan.py wires codex_cli_startup (Story #846).

These are structural tests: they verify that the startup module is imported
from lifespan.py and that initialize_codex_manager_on_startup is called,
mirroring the claude_cli_startup precedent at line ~656.
No server startup is required — grep-style source inspection only.
"""

from __future__ import annotations

from pathlib import Path


LIFESPAN_PATH = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "code_indexer"
    / "server"
    / "startup"
    / "lifespan.py"
)


def _split_at_yield(source: str):
    """Split lifespan source into startup (before yield) and shutdown (after yield) halves."""
    # The asynccontextmanager uses a bare `yield` to separate startup from shutdown.
    # We split on the first occurrence of "\n        yield" (indented inside make_lifespan).
    marker = "\n        yield"
    idx = source.find(marker)
    assert idx != -1, "Could not find 'yield' marker in lifespan.py"
    return source[:idx], source[idx + len(marker) :]


class TestLifespanCodexWiring:
    def test_lifespan_imports_codex_cli_startup(self):
        """lifespan.py must import from code_indexer.server.startup.codex_cli_startup."""
        source = LIFESPAN_PATH.read_text()
        assert "from code_indexer.server.startup.codex_cli_startup import" in source, (
            "lifespan.py must wire codex_cli_startup (Story #846 Gap 1)"
        )

    def test_lifespan_calls_initialize_codex_manager_on_startup(self):
        """lifespan.py must contain a call to initialize_codex_manager_on_startup(...)."""
        source = LIFESPAN_PATH.read_text()
        # Check for call site (with opening paren) to confirm invocation, not just import.
        assert "initialize_codex_manager_on_startup(" in source, (
            "lifespan.py must call initialize_codex_manager_on_startup() (Story #846 Gap 1)"
        )

    def test_lifespan_hoists_codex_shutdown_hook_to_app_state(self):
        """lifespan.py startup block must store the codex hook on app.state (CRIT-1, Story #846).

        Without this hoist the hook is only held in a local variable and is
        unreachable by the shutdown block which reads from app.state.
        """
        source = LIFESPAN_PATH.read_text()
        startup, _ = _split_at_yield(source)
        assert "app.state.codex_shutdown_hook" in startup, (
            "lifespan.py startup region must assign app.state.codex_shutdown_hook after "
            "calling initialize_codex_manager_on_startup() (Story #846 CRIT-1)"
        )

    def test_lifespan_invokes_codex_shutdown_hook_on_teardown(self):
        """lifespan.py shutdown block must call the codex hook (CRIT-1, Story #846).

        Without the teardown invocation the Codex lease is never returned to
        the provider on server shutdown.
        """
        source = LIFESPAN_PATH.read_text()
        _, shutdown = _split_at_yield(source)
        assert "_codex_hook()" in shutdown, (
            "lifespan.py shutdown region (after yield) must invoke _codex_hook() to "
            "return the Codex lease on server shutdown (Story #846 CRIT-1)"
        )


class TestErrorCodeUniqueness:
    """CRIT-2 regression: APP-GENERAL-046 (api_key_management) and the Codex
    startup code (APP-GENERAL-050) must be distinct entries with distinct
    descriptions so monitoring alerts are unambiguous (Story #846)."""

    def test_codex_startup_code_is_distinct_from_api_key_code(self):
        from code_indexer.server.error_codes import ERROR_REGISTRY

        assert "APP-GENERAL-046" in ERROR_REGISTRY, (
            "APP-GENERAL-046 must be registered in ERROR_REGISTRY"
        )
        assert "APP-GENERAL-050" in ERROR_REGISTRY, (
            "APP-GENERAL-050 must be registered in ERROR_REGISTRY (Codex startup code)"
        )
        desc_046 = ERROR_REGISTRY["APP-GENERAL-046"].description
        desc_050 = ERROR_REGISTRY["APP-GENERAL-050"].description
        assert desc_046 != desc_050, (
            f"APP-GENERAL-046 and APP-GENERAL-050 must have distinct descriptions "
            f"to avoid monitoring ambiguity; both have: {desc_046!r}"
        )
