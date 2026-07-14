"""
Bug #1399 CRITICAL item 3: mcp_session.session_ttl_seconds /
cleanup_interval_seconds must actually reach the running MCP session
registry.

Root cause: lifespan.py's startup block called
    session_registry.start_background_cleanup(
        ttl_seconds=3600,             # hardcoded literal
        cleanup_interval_seconds=900, # hardcoded literal
    )

session_registry.py's start_background_cleanup() already has a correct
config-driven fallback (reads config.mcp_session_config.session_ttl_seconds
/ cleanup_interval_seconds from ConfigService) -- but that fallback ONLY
activates when the corresponding argument is None. Passing explicit literals
at the call site always bypasses it, so a Web UI change to
mcp_session.session_ttl_seconds / cleanup_interval_seconds could never reach
the running registry (not even on the next restart, since lifespan.py itself
overrides the DB value with the literal on every startup).

Fix: remove the hardcoded literals from the lifespan.py call site so
start_background_cleanup() is called with ttl_seconds=None,
cleanup_interval_seconds=None (or simply no arguments), letting the existing
config-driven fallback run.

Test suite:
1. Source-text guard: lifespan.py's MCP Session cleanup init block must not
   pass ttl_seconds=3600 / cleanup_interval_seconds=900 literals.
2. Runtime guard: exercises the ACTUAL session_registry.py fallback with
   ttl_seconds=None / cleanup_interval_seconds=None and a real ConfigService
   backed by a real (tmp_path) config, proving the fallback truly reads the
   configured DB values end-to-end.
"""

from __future__ import annotations

from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)

# Distinctive values used by the runtime test -- chosen to be nothing like
# the module-level defaults (3600s / 900s) so a false-pass via defaults is
# impossible.
_DISTINCTIVE_TTL_SECONDS = 4321
_DISTINCTIVE_CLEANUP_INTERVAL_SECONDS = 654


class TestLifespanMcpSessionCleanupSourceGuard:
    """Source-text guard: lifespan.py must not hardcode TTL/cleanup literals."""

    def test_start_background_cleanup_call_has_no_hardcoded_literals(self):
        """
        Bug #1399: lifespan.py must not call
        session_registry.start_background_cleanup(ttl_seconds=3600,
        cleanup_interval_seconds=900) -- those literals always bypass the
        existing config-driven fallback in session_registry.py, which only
        activates when the argument is None.
        """
        source = _LIFESPAN_PATH.read_text()

        block_start = source.find("Initializing MCP Session cleanup")
        assert block_start != -1, (
            "'Initializing MCP Session cleanup' log block not found in lifespan.py"
        )

        call_start = source.find("start_background_cleanup(", block_start)
        assert call_start != -1, (
            "session_registry.start_background_cleanup( call not found after "
            "the MCP Session cleanup init block in lifespan.py"
        )

        # Grab a small window after the call site (the call's argument list)
        # to check for the hardcoded literals without parsing the whole file.
        call_window = source[call_start : call_start + 200]

        assert "ttl_seconds=3600" not in call_window, (
            "Bug #1399: lifespan.py still hardcodes ttl_seconds=3600 at the "
            "start_background_cleanup() call site -- this bypasses the "
            "config-driven fallback in session_registry.py. Remove the "
            "literal so ttl_seconds defaults to None."
        )
        assert "cleanup_interval_seconds=900" not in call_window, (
            "Bug #1399: lifespan.py still hardcodes cleanup_interval_seconds=900 "
            "at the start_background_cleanup() call site -- this bypasses the "
            "config-driven fallback in session_registry.py. Remove the "
            "literal so cleanup_interval_seconds defaults to None."
        )


class TestSessionRegistryConfigDrivenFallbackRuntime:
    """Runtime guard: the None-triggered config-driven fallback actually works.

    This proves the fix at the lifespan.py call site is sufficient -- the
    downstream fallback it unblocks is real and reads real configured
    values, not just module defaults.
    """

    @pytest.fixture(autouse=True)
    def _reset_registry(self):
        from code_indexer.server.mcp.session_registry import get_session_registry

        registry = get_session_registry()
        registry.clear_all()
        registry.stop_background_cleanup()
        yield registry
        registry.stop_background_cleanup()

    @pytest.mark.asyncio
    async def test_none_args_read_configured_ttl_and_interval_from_config_service(
        self, tmp_path: Path, _reset_registry
    ):
        from code_indexer.server.services.config_service import (
            ConfigService,
            set_config_service,
            reset_config_service,
        )

        service = ConfigService(str(tmp_path))
        service.update_setting(
            "mcp_session", "session_ttl_seconds", _DISTINCTIVE_TTL_SECONDS
        )
        service.update_setting(
            "mcp_session",
            "cleanup_interval_seconds",
            _DISTINCTIVE_CLEANUP_INTERVAL_SECONDS,
        )

        set_config_service(service)
        try:
            registry = _reset_registry
            registry.start_background_cleanup(
                ttl_seconds=None, cleanup_interval_seconds=None
            )

            assert registry._ttl_seconds == _DISTINCTIVE_TTL_SECONDS, (
                "session_registry's config-driven fallback must read "
                "session_ttl_seconds from ConfigService when ttl_seconds=None; "
                f"got {registry._ttl_seconds!r}."
            )
            assert (
                registry._cleanup_interval_seconds
                == _DISTINCTIVE_CLEANUP_INTERVAL_SECONDS
            ), (
                "session_registry's config-driven fallback must read "
                "cleanup_interval_seconds from ConfigService when "
                f"cleanup_interval_seconds=None; got "
                f"{registry._cleanup_interval_seconds!r}."
            )
        finally:
            reset_config_service()
