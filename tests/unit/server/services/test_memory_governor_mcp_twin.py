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
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from code_indexer.server.auth.user_manager import User, UserRole
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
# The permission declared in the tool doc's required_permission field must be
# a real permission that admin users actually hold (not the literal string "admin").
REQUIRED_PERMISSION = "manage_users"


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

    def test_real_admin_user_passes_permission_gate(self):
        """required_permission in tool doc must be a real permission admin holds.

        Checks: (1) real admin User has REQUIRED_PERMISSION, (2) normal user does
        not (confirming the gate is admin-restricted), (3) tool doc frontmatter
        declares required_permission == REQUIRED_PERMISSION.
        Catches the defect where the doc declared "admin" (not a real permission).
        """
        import yaml

        _dummy_ts = datetime(2024, 1, 1, tzinfo=timezone.utc)

        # (1) A real admin user must hold REQUIRED_PERMISSION.
        admin_user = User(
            username="admin",
            role=UserRole.ADMIN,
            password_hash="x",
            created_at=_dummy_ts,
        )
        assert admin_user.has_permission(REQUIRED_PERMISSION), (
            f"Admin user does not have permission '{REQUIRED_PERMISSION}' — "
            "check the permission model in auth/user_manager.py"
        )

        # (2) A normal user must NOT hold REQUIRED_PERMISSION.
        normal_user = User(
            username="alice",
            role=UserRole.NORMAL_USER,
            password_hash="x",
            created_at=_dummy_ts,
        )
        assert not normal_user.has_permission(REQUIRED_PERMISSION), (
            f"Normal user unexpectedly has permission '{REQUIRED_PERMISSION}'"
        )

        # (3) The tool doc must declare the same real permission.
        md_file = _project_root() / TOOL_DOC_RELATIVE
        content = md_file.read_text()
        parts = content.split("---")
        assert len(parts) >= 3, "tool doc has no YAML frontmatter"
        frontmatter = yaml.safe_load(parts[1])
        doc_permission = frontmatter.get("required_permission", "")
        assert doc_permission == REQUIRED_PERMISSION, (
            f"Tool doc required_permission is '{doc_permission}', "
            f"expected '{REQUIRED_PERMISSION}'. "
            "No user role holds a permission literally named 'admin'."
        )
