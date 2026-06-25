"""Story 4 — Part 3: MCP twin handler get_memory_governor_stats.

Tests:
- Handler callable in handlers/admin/__init__.py.
- Tool doc file exists at tool_docs/admin/get_memory_governor_stats.md.
- Registered in TOOL_REGISTRY (loaded from .md file).
- Registered in _register() (handler wiring).
- Returns snapshot envelope when governor active.
- No exception (graceful response) when governor absent.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from code_indexer.server.services.memory_governor import (
    MemoryBand,
    MemoryGovernor,
    clear_memory_governor,
    set_memory_governor,
)

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

BYTES_PER_GIB = 1024 * 1024 * 1024
HOST_100_GIB = 100 * BYTES_PER_GIB
GREEN_USAGE_PCT = 30.0
YELLOW_PCT_DEFAULT = 70.0
RED_PCT_DEFAULT = 85.0
HYSTERESIS_PCT_DEFAULT = 10.0
NO_SWAP_PAGES_IN = 0
NO_RED_DWELL_SECONDS = 0.0

TOOL_NAME = "get_memory_governor_stats"
TOOL_DOC_RELATIVE = (
    "src/code_indexer/server/mcp/tool_docs/admin/get_memory_governor_stats.md"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_readers(used_pct: float) -> MagicMock:
    readers = MagicMock()
    vm = MagicMock()
    vm.total = HOST_100_GIB
    vm.used = int(HOST_100_GIB * used_pct / 100)
    readers.read_host_memory.return_value = vm
    readers.read_cgroup_v2_max.side_effect = FileNotFoundError
    readers.read_cgroup_v1_limit.side_effect = FileNotFoundError
    readers.read_pswpin.return_value = NO_SWAP_PAGES_IN
    return readers


def _green_gov() -> MemoryGovernor:
    gov = MemoryGovernor(
        readers=_make_readers(GREEN_USAGE_PCT),
        enabled=True,
        start_sampler=False,
        yellow_pct=YELLOW_PCT_DEFAULT,
        red_pct=RED_PCT_DEFAULT,
        hysteresis_pct=HYSTERESIS_PCT_DEFAULT,
        red_min_dwell_seconds=NO_RED_DWELL_SECONDS,
    )
    gov._tick()
    assert gov.band == MemoryBand.GREEN
    return gov


def _admin_user() -> MagicMock:
    user = MagicMock()
    user.role = "admin"
    return user


def _project_root() -> Path:
    """Return the code-indexer project root (4 parents up from this test file)."""
    return Path(__file__).parent.parent.parent.parent.parent


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMCPTwinHandler:
    """MCP tool get_memory_governor_stats."""

    def test_handler_callable_in_admin_module(self):
        """handle_get_memory_governor_stats must exist and be callable."""
        from code_indexer.server.mcp.handlers.admin import (
            handle_get_memory_governor_stats,
        )

        assert callable(handle_get_memory_governor_stats)

    def test_tool_doc_file_exists(self):
        """tool_docs/admin/get_memory_governor_stats.md must exist."""
        md_file = _project_root() / TOOL_DOC_RELATIVE
        assert md_file.exists(), f"Missing tool doc: {md_file}"

    def test_registered_in_tool_registry(self):
        """TOOL_REGISTRY must contain get_memory_governor_stats (loaded from .md)."""
        from code_indexer.server.mcp.tools import TOOL_REGISTRY

        assert TOOL_NAME in TOOL_REGISTRY

    def test_registered_in_admin_handler_registry(self):
        """_register() must wire get_memory_governor_stats into the handler registry."""
        from code_indexer.server.mcp.handlers.admin import _register

        registry: dict = {}
        _register(registry)
        assert TOOL_NAME in registry

    def test_returns_snapshot_envelope_when_active(self):
        """Handler returns a snapshot with band=GREEN when governor is active."""
        from code_indexer.server.mcp.handlers.admin import (
            handle_get_memory_governor_stats,
        )

        gov = _green_gov()
        set_memory_governor(gov)
        try:
            result = handle_get_memory_governor_stats({}, _admin_user())
            assert result is not None
            # Unwrap MCP response envelope if present
            if isinstance(result, dict) and "content" in result:
                data = json.loads(result["content"][0]["text"])
                assert data.get("band") == "GREEN"
            else:
                assert result.get("band") == "GREEN" or result.get("success") is True
        finally:
            clear_memory_governor()

    def test_no_exception_when_governor_absent(self):
        """Handler must not raise when governor is None — returns a graceful response."""
        from code_indexer.server.mcp.handlers.admin import (
            handle_get_memory_governor_stats,
        )

        clear_memory_governor()
        result = handle_get_memory_governor_stats({}, _admin_user())
        assert result is not None
