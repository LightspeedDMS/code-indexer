"""
Story #919 AC5: MCP tool trigger_dependency_analysis dry_run_graph_only wiring tests.

Verifies:
  AC5: handle_trigger_dependency_analysis accepts dry_run_graph_only: bool = False
  AC5: when dry_run_graph_only=True response includes graph_repair_dry_run_report key
  AC5: omitting dry_run_graph_only (default False) excludes graph_repair_dry_run_report

Note: stubs are at the service boundary only
(Messi Rule 1: mock only the external-service boundary, never internal logic).

Tests (exhaustive list):
  test_dry_run_graph_only_param_accepted_without_error
  test_dry_run_graph_only_true_response_includes_report_key
  test_omitting_dry_run_graph_only_excludes_report_key

Module-level helpers (exhaustive list):
  _StubDepMapService        -- minimal service satisfying is_available() check
  _make_user()              -- minimal User stub
  _build_server_config()    -- config stub with dependency_map_enabled=True
  _call_handler(args)       -- invoke handler with stubbed app state
  _unwrap_payload(result)   -- decode MCP content envelope; raises AssertionError if malformed
  _assert_success_payload(payload) -- assert success=True and no error key
"""

import json
from typing import Any, Dict
from unittest.mock import MagicMock, patch

from code_indexer.server.mcp.handlers.admin import handle_trigger_dependency_analysis


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubDepMapService:
    """Minimal dependency map service stub that reports availability."""

    def is_available(self) -> bool:
        return True

    def run_delta_analysis(self, job_id: str, **kwargs: Any) -> None:
        pass

    def run_full_analysis(self, job_id: str, **kwargs: Any) -> None:
        pass

    def run_graph_repair_dry_run(self) -> Dict[str, Any]:
        """Return a minimal dry_run report dict."""
        return {
            "mode": "dry_run",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "total_anomalies": 0,
            "per_type_counts": {},
            "per_verdict_counts": {},
            "per_action_counts": {},
            "would_be_writes": [],
            "skipped": [],
        }


def _make_user() -> Any:
    """Return a minimal user stub with admin role."""
    user = MagicMock()
    user.username = "admin"
    return user


def _build_server_config() -> Any:
    """Return a minimal server config stub with dependency_map_enabled=True."""
    config = MagicMock()
    config.claude_integration_config.dependency_map_enabled = True
    return config


def _unwrap_payload(result: Any) -> Dict[str, Any]:
    """Decode the MCP content envelope and return the inner payload dict.

    All handler responses use _mcp_response which always produces:
      {"content": [{"type": "text", "text": "<json>"}]}
    Raises AssertionError with a descriptive message if the shape is unexpected.
    """
    assert isinstance(result, dict), f"Expected dict from handler, got {type(result)}"
    assert "content" in result, (
        f"Expected 'content' key in MCP response, got: {list(result.keys())}"
    )
    items = result["content"]
    assert items, "MCP response 'content' list is empty"
    first = items[0]
    assert isinstance(first, dict) and "text" in first, (
        f"MCP content item missing 'text' key: {first!r}"
    )
    return json.loads(first["text"])


def _assert_success_payload(payload: Dict[str, Any]) -> None:
    """Assert that payload represents a successful MCP response."""
    assert payload.get("success") is True, f"Expected success=True, got: {payload}"
    assert "error" not in payload, f"Unexpected 'error' key in payload: {payload}"


def _call_handler(args: Dict[str, Any]) -> Dict[str, Any]:
    """Invoke handle_trigger_dependency_analysis with stubbed app state."""
    stub_service = _StubDepMapService()

    with (
        patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=MagicMock(get_config=lambda: _build_server_config()),
        ),
        patch(
            "code_indexer.server.mcp.handlers._utils.app_module.app.state",
            dependency_map_service=stub_service,
        ),
    ):
        return handle_trigger_dependency_analysis(args, _make_user())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_dry_run_graph_only_param_accepted_without_error() -> None:
    """AC5: passing dry_run_graph_only=True succeeds — success=True and no error key."""
    result = _call_handler({"mode": "delta", "dry_run_graph_only": True})
    payload = _unwrap_payload(result)
    _assert_success_payload(payload)


def test_dry_run_graph_only_true_response_includes_report_key() -> None:
    """AC5: dry_run_graph_only=True response includes graph_repair_dry_run_report key."""
    result = _call_handler({"mode": "delta", "dry_run_graph_only": True})
    payload = _unwrap_payload(result)
    _assert_success_payload(payload)
    assert "graph_repair_dry_run_report" in payload, (
        f"Expected 'graph_repair_dry_run_report' in response, got: {list(payload.keys())}"
    )


def test_omitting_dry_run_graph_only_excludes_report_key() -> None:
    """AC5: omitting dry_run_graph_only (default False) succeeds without report key."""
    result = _call_handler({"mode": "delta"})
    payload = _unwrap_payload(result)
    _assert_success_payload(payload)
    assert "graph_repair_dry_run_report" not in payload, (
        "Unexpected 'graph_repair_dry_run_report' in non-dry-run response"
    )
