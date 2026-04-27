"""
Phase 4 E2E test: cidx-meta backup rebase conflict resolution (Story #926 AC8).

PREREQUISITES (Phase 4 live environment required):
  - Live uvicorn CIDX server at E2E_SERVER_HOST:E2E_SERVER_PORT
  - Anthropic API / Claude CLI (managed by ApiKeySyncService into ~/.claude.json)
  - Admin credentials (E2E_ADMIN_USER / E2E_ADMIN_PASS in .e2e-automation)
  - file:// git transport (no SSH key required)

Exercises the full cidx-meta backup flow end-to-end with ZERO mocking:
  1. Configure backup with file:// bare remote; trigger refresh (bootstrap)
  2. Inject divergent commit on bare remote AND conflicting local write
  3. Trigger refresh -- rebase conflict -- real Claude CLI resolves it
  4. Verify merged _domains.json shows conflict was actually resolved

Run as part of e2e-automation.sh --phase 4 (cli_remote tests).
"""

from __future__ import annotations

import json
from typing import Any
import warnings
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import E2EConfig
from tests.e2e.helpers import (
    CONFLICT_RESOLUTION_TIMEOUT,
    MCP_CALL_TIMEOUT,
    login,
    patch_json_field,
    run_git,
    toggle_cidx_meta_backup,
)

_META_ALIAS = "cidx-meta-global"  # RefreshScheduler._execute_refresh contract
_META_SUBDIR = "cidx-meta"  # mutable base folder under data/golden-repos/
_DOMAINS_FILE = "_domains.json"

# Distinct sentinel values injected on each side of the conflict.
# Claude's merged output must not be identical to either of these raw values,
# proving neither side silently won uncontested.
_REMOTE_VALUE = "REMOTE-AC8: remote side injected by E2E harness"
_LOCAL_VALUE = "LOCAL-AC8: local side injected by E2E harness"


def _call_mcp_tool(
    client: httpx.Client,
    token: str,
    tool_name: str,
    arguments: dict,
    *,
    timeout: float = MCP_CALL_TIMEOUT,
) -> dict:
    """POST a tools/call MCP JSON-RPC request and return the inner result dict.

    The MCP endpoint wraps handler responses in:
      {"content": [{"type": "text", "text": "<JSON>"}]}

    This helper unwraps that envelope and returns the parsed inner dict.

    Args:
        client: httpx.Client bound to the server base URL.
        token: JWT access token.
        tool_name: MCP tool name (e.g. "enter_write_mode").
        arguments: Tool argument dict.
        timeout: HTTP request timeout in seconds (use CONFLICT_RESOLUTION_TIMEOUT
                 for exit_write_mode calls that invoke Claude CLI conflict resolution).

    Raises:
        httpx.HTTPStatusError: On HTTP-level errors.
        AssertionError: If the JSON-RPC response contains an error field.
        KeyError: If the MCP content envelope is missing expected fields.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    resp = client.post(
        "/mcp",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    resp.raise_for_status()
    body = resp.json()
    assert "error" not in body, (
        f"MCP JSON-RPC error calling {tool_name}: {body['error']}"
    )
    result = body.get("result", {})
    content = result.get("content", [])
    assert content, f"MCP result for {tool_name} has empty content: {result}"
    parsed: dict[str, Any] = json.loads(content[0]["text"])
    return parsed


def _run_refresh_via_write_mode(
    client: httpx.Client, token: str, *, label: str
) -> None:
    """Trigger cidx-meta-global refresh via the write-mode lifecycle.

    cidx-meta-global is a write-exception repo whose refresh is NOT triggerable
    via the standard /api/admin/golden-repos/{alias}/refresh REST endpoint.
    The canonical external trigger is the MCP write-mode lifecycle:
      enter_write_mode -> exit_write_mode
    exit_write_mode calls _execute_refresh() synchronously and blocks until
    complete, so no job polling is required.

    Uses CONFLICT_RESOLUTION_TIMEOUT for the exit_write_mode call because
    conflict resolution invokes the real Claude CLI subprocess, which can
    take several minutes.

    Args:
        client: httpx.Client bound to the server base URL.
        token: JWT access token.
        label: Human-readable label for assertion error messages.

    Raises:
        AssertionError: If enter_write_mode or exit_write_mode reports failure.
    """
    enter_result = _call_mcp_tool(
        client,
        token,
        "enter_write_mode",
        {"repo_alias": _META_ALIAS},
    )
    assert enter_result.get("success"), (
        f"{label} enter_write_mode failed: {enter_result.get('error') or enter_result}"
    )

    exit_result = _call_mcp_tool(
        client,
        token,
        "exit_write_mode",
        {"repo_alias": _META_ALIAS},
        timeout=CONFLICT_RESOLUTION_TIMEOUT,
    )
    assert exit_result.get("success"), (
        f"{label} exit_write_mode (refresh) failed: "
        f"{exit_result.get('error') or exit_result.get('message') or exit_result}"
    )


def _assert_billing_conflict_resolved(remote_url: str, tmp_path: Path) -> None:
    """Clone bare remote and verify neither side's raw value won uncontested.

    Asserts:
      - billing.description is non-empty (merge produced output)
      - description != _REMOTE_VALUE  (remote did not simply overwrite local)
      - description != _LOCAL_VALUE   (local did not simply overwrite remote)

    These three conditions together prove Claude resolved the conflict rather
    than one side silently winning.
    """
    verify = tmp_path / "verify"
    run_git(["clone", remote_url, str(verify)], cwd=tmp_path)
    domains_file = verify / _DOMAINS_FILE
    assert domains_file.exists(), (
        f"{_DOMAINS_FILE} missing from bare remote after merge"
    )
    merged = json.loads(domains_file.read_text())
    assert "billing" in merged, (
        f"'billing' missing from merged {_DOMAINS_FILE}: {merged}"
    )
    description = merged["billing"].get("description", "")
    assert description, (
        f"billing.description is empty after conflict resolution: {merged}"
    )
    assert description != _REMOTE_VALUE, (
        f"billing.description equals the unmodified remote value — remote silently won: {description!r}"
    )
    assert description != _LOCAL_VALUE, (
        f"billing.description equals the unmodified local value — local silently won: {description!r}"
    )


@pytest.mark.e2e
def test_cidx_meta_backup_ac8_rebase_conflict_resolved_by_claude(
    e2e_config: E2EConfig,
    tmp_path: Path,
) -> None:
    """Story #926 AC8: rebase conflict in _domains.json resolved by real Claude CLI.

    PREREQUISITE: Phase 4 live uvicorn server with cidx-meta-global populated.
    ZERO mocking: real Claude CLI subprocess, real file:// git transport.
    """
    bare_remote = tmp_path / "cidx-meta-backup-remote.git"
    bare_remote.mkdir()
    run_git(["init", "--bare", str(bare_remote)], cwd=tmp_path)
    remote_url = f"file://{bare_remote}"
    base_meta = e2e_config.server_data_dir / "data" / "golden-repos" / _META_SUBDIR
    token = login(e2e_config.server_url, e2e_config.admin_user, e2e_config.admin_pass)
    original_exc: BaseException | None = None

    with httpx.Client(base_url=e2e_config.server_url) as client:
        try:
            toggle_cidx_meta_backup(
                client,
                admin_user=e2e_config.admin_user,
                admin_pass=e2e_config.admin_pass,
                enabled=True,
                remote_url=remote_url,
            )
            _run_refresh_via_write_mode(client, token, label="bootstrap")

            divergent = tmp_path / "divergent"
            run_git(["clone", remote_url, str(divergent)], cwd=tmp_path)
            run_git(["config", "user.email", "e2e@test.local"], cwd=divergent)
            run_git(["config", "user.name", "E2E Harness"], cwd=divergent)
            patch_json_field(
                divergent,
                divergent / _DOMAINS_FILE,
                "billing",
                "description",
                _REMOTE_VALUE,
            )
            run_git(["add", _DOMAINS_FILE], cwd=divergent)
            run_git(
                ["commit", "-m", "test: inject remote divergent commit (AC8)"],
                cwd=divergent,
            )
            run_git(["push", "origin", "HEAD"], cwd=divergent)

            # Per feedback_versioned_path_trap.md: only mutable base clone, never .versioned/
            patch_json_field(
                base_meta,
                base_meta / _DOMAINS_FILE,
                "billing",
                "description",
                _LOCAL_VALUE,
            )

            _run_refresh_via_write_mode(client, token, label="conflict-resolve")
            _assert_billing_conflict_resolved(remote_url, tmp_path)

        except BaseException as exc:
            original_exc = exc
            raise

        finally:
            try:
                toggle_cidx_meta_backup(
                    client,
                    admin_user=e2e_config.admin_user,
                    admin_pass=e2e_config.admin_pass,
                    enabled=False,
                    remote_url="",
                )
            except Exception as cleanup_exc:
                msg = f"AC8 cleanup: failed to disable backup ({cleanup_exc})"
                if original_exc is None:
                    raise RuntimeError(msg) from cleanup_exc
                warnings.warn(msg, stacklevel=2)
