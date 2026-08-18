"""
Phase 4 E2E test: cidx-meta backup mirror sync under remote/local divergence
(Bug #1555 mirror semantics; supersedes the original Story #926 AC8 design).

PREREQUISITES (Phase 4 live environment required):
  - Live uvicorn CIDX server at E2E_SERVER_HOST:E2E_SERVER_PORT
  - Admin credentials (E2E_ADMIN_USER / E2E_ADMIN_PASS in .e2e-automation)
  - file:// git transport (no SSH key required)

HISTORY: this test originally exercised Story #926 AC8's rebase +
Claude-CLI auto-conflict-resolution design: a divergent remote commit and a
conflicting local write on the same field were expected to be reconciled by
invoking the real Claude CLI, producing a merged value equal to neither raw
side. Bug #1555 (commit 6a46c996) removed that entire mechanism as a
root-cause fix: cidx-meta's git remote is a passive BACKUP MIRROR, never a
peer whose independent history must be preserved, so ``sync()`` no longer
fetches-then-rebases. It commits local changes and publishes local HEAD
directly via ``git push --force-with-lease``, unconditionally overwriting
whatever the remote holds. There is no more conflict class to resolve, and
therefore nothing left for Claude CLI to reconcile -- see
``src/code_indexer/server/services/cidx_meta_backup/sync.py``'s module
docstring for the full rationale. This test now proves that CURRENT,
intentional contract end-to-end through the real MCP write-mode lifecycle,
instead of asserting the removed behavior.

Exercises the full cidx-meta backup flow end-to-end with ZERO mocking:
  1. Configure backup with file:// bare remote; trigger refresh (bootstrap)
  2. Inject divergent commit on bare remote AND conflicting local write
  3. Trigger refresh -- local is unconditionally published, remote is
     overwritten via force-with-lease -- no conflict step, no Claude CLI
  4. Verify merged _domains.json holds exactly the local value on both
     the local clone and the remote

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

# Distinct sentinel values injected on each side of the divergence.
# Bug #1555's mirror-semantics contract requires the merged output to equal
# the LOCAL value exactly (local is unconditionally authoritative, published
# via git push --force-with-lease) and to never equal the raw remote value.
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
                 for exit_write_mode calls, a generous safety ceiling for the
                 mirror sync/push cycle -- no Claude CLI involved).

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

    Uses CONFLICT_RESOLUTION_TIMEOUT for the exit_write_mode call as a
    generous safety ceiling for the sync/push cycle (this call runs the real
    cidx-meta backup sync -- git commit/fetch/push -- no Claude CLI involved).

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


def _assert_local_value_won_the_mirror_push(remote_url: str, tmp_path: Path) -> None:
    """Clone bare remote and verify local unconditionally won (Bug #1555).

    Asserts:
      - billing.description equals the LOCAL value exactly (local is the
        sole source of truth for the mirror push -- force-with-lease
        overwrites the remote unconditionally, with no merge/rebase step)
      - description != _REMOTE_VALUE (the remote's divergent commit was
        overwritten, not preserved or merged)

    These conditions together prove the CURRENT mirror-semantics contract:
    the remote is a passive backup, never a peer, so divergence is resolved
    by unconditional local authority rather than any conflict-resolution
    step (Claude CLI or otherwise).
    """
    verify = tmp_path / "verify"
    run_git(["clone", remote_url, str(verify)], cwd=tmp_path)
    domains_file = verify / _DOMAINS_FILE
    assert domains_file.exists(), f"{_DOMAINS_FILE} missing from bare remote after sync"
    merged = json.loads(domains_file.read_text())
    assert "billing" in merged, (
        f"'billing' missing from merged {_DOMAINS_FILE}: {merged}"
    )
    description = merged["billing"].get("description", "")
    assert description != _REMOTE_VALUE, (
        f"billing.description equals the remote value -- remote was not "
        f"overwritten by the local-authoritative mirror push: {description!r}"
    )
    assert description == _LOCAL_VALUE, (
        f"billing.description does not equal the local value -- local did "
        f"not unconditionally win the mirror push as Bug #1555 requires: "
        f"{description!r}"
    )


def _inject_remote_divergent_commit(tmp_path: Path, remote_url: str) -> None:
    """Clone the bare remote and push a commit that diverges from local.

    Mirrors the shape of the original AC8 conflict scenario: a commit on the
    SAME field (billing.description) that local will also modify, so the
    mirror push has genuinely conflicting content to overwrite.
    """
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
        ["commit", "-m", "test: inject remote divergent commit (Bug #1555)"],
        cwd=divergent,
    )
    run_git(["push", "origin", "HEAD"], cwd=divergent)


@pytest.mark.e2e
def test_cidx_meta_backup_diverged_remote_local_wins_bug1555(
    e2e_config: E2EConfig,
    tmp_path: Path,
) -> None:
    """Bug #1555: a diverged remote never blocks/conflicts a mirror sync --
    local is unconditionally published via force-with-lease.

    PREREQUISITE: Phase 4 live uvicorn server with cidx-meta-global populated.
    ZERO mocking: real file:// git transport, real MCP write-mode lifecycle.
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

            _inject_remote_divergent_commit(tmp_path, remote_url)

            # Per feedback_versioned_path_trap.md: only mutable base clone, never .versioned/
            patch_json_field(
                base_meta,
                base_meta / _DOMAINS_FILE,
                "billing",
                "description",
                _LOCAL_VALUE,
            )

            _run_refresh_via_write_mode(client, token, label="mirror-sync")
            _assert_local_value_won_the_mirror_push(remote_url, tmp_path)

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
