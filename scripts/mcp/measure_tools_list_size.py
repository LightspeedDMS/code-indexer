"""
Baseline measurement harness for MCP tools/list payload size.

Story #986: Measures byte size and tiktoken token count of the tools/list
payload for each user role, so subsequent changes can be quantitatively
validated.

Usage:
    PYTHONPATH=./src python3 scripts/mcp/measure_tools_list_size.py

Output:
    reports/mcp/baseline_<timestamp>.json

No infrastructure required: no database, no server, no network calls.
Imports the ToolDocLoader directly to build the tool registry offline.
"""

import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import tiktoken

# Ensure src/ is on the path when invoked directly
_REPO_ROOT = Path(__file__).parents[2]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader  # noqa: E402

logger = logging.getLogger(__name__)

# Timeout for the git rev-parse subprocess used to capture HEAD SHA.
GIT_COMMAND_TIMEOUT_SECONDS = 5

# ---------------------------------------------------------------------------
# Role -> permission set mapping
# Mirrors the inheritance in user_manager.py User.has_permission()
# without requiring the User model or any database.
# ---------------------------------------------------------------------------

_NORMAL_USER_BASE = frozenset(
    {
        "public",
        "query_repos",
        "repository:read",
        "activate_repos",
    }
)

_POWER_USER_BASE = frozenset(
    {
        "repository:write",
        "delegate_open",
    }
)

_ADMIN_BASE = frozenset(
    {
        "manage_users",
        "manage_golden_repos",
        "repository:admin",
    }
)

ROLE_PERMISSIONS: Dict[str, frozenset] = {
    "anonymous": frozenset({"public"}),
    "normal_user": _NORMAL_USER_BASE,
    "power_user": _NORMAL_USER_BASE | _POWER_USER_BASE,
    "admin": _NORMAL_USER_BASE | _POWER_USER_BASE | _ADMIN_BASE,
}

# ---------------------------------------------------------------------------
# Tool registry (loaded once at import time, just like the server does)
# ---------------------------------------------------------------------------

_TOOL_DOCS_DIR = _REPO_ROOT / "src" / "code_indexer" / "server" / "mcp" / "tool_docs"


def _load_registry() -> Dict[str, Dict[str, Any]]:
    """Load the full tool registry from tool_docs/ markdown files."""
    loader = ToolDocLoader(_TOOL_DOCS_DIR)
    result: Dict[str, Dict[str, Any]] = loader.build_tool_registry()
    return result


_REGISTRY: Dict[str, Dict[str, Any]] = _load_registry()

# tiktoken encoder (cl100k_base as required by AC1)
_ENCODER = tiktoken.get_encoding("cl100k_base")


# ---------------------------------------------------------------------------
# Core measurement functions
# ---------------------------------------------------------------------------


def _filter_tools_for_role(role: str) -> list:
    """
    Filter the tool registry to only tools visible to the given role.

    Checks required_permission only, not requires_config. The server's
    filter_tools_by_role() additionally hides tools whose requires_config
    condition is unmet (e.g., tracing tools when Langfuse is disabled).
    We intentionally measure the permission-only ceiling so the baseline
    is config-independent and stable across environments.

    Returns a list of MCP-compliant tool dicts (name, description, inputSchema)
    matching the tools/list response shape.
    """
    permissions = ROLE_PERMISSIONS[role]
    visible = []
    for tool_def in _REGISTRY.values():
        required = tool_def.get("required_permission", "public")
        if required in permissions:
            # Emit only MCP-valid fields (same as filter_tools_by_role in tools.py)
            mcp_tool = {
                "name": tool_def["name"],
                "description": tool_def["description"],
                "inputSchema": tool_def["inputSchema"],
            }
            visible.append(mcp_tool)
    return visible


def measure_role(role: str) -> Dict[str, int]:
    """
    Measure the tools/list payload for a given role.

    Args:
        role: One of 'anonymous', 'normal_user', 'power_user', 'admin'

    Returns:
        Dict with keys:
        - byte_size: UTF-8 encoded byte length of the JSON serialized tool list
        - tiktoken_count: Token count using cl100k_base encoding
        - tool_count: Number of tools visible to this role
    """
    if role not in ROLE_PERMISSIONS:
        raise ValueError(
            f"Unknown role '{role}'. Valid roles: {sorted(ROLE_PERMISSIONS.keys())}"
        )

    tools = _filter_tools_for_role(role)
    payload = json.dumps(tools, ensure_ascii=False)
    encoded = payload.encode("utf-8")

    return {
        "byte_size": len(encoded),
        "tiktoken_count": len(_ENCODER.encode(payload)),
        "tool_count": len(tools),
    }


def collect_measurements() -> Dict[str, Dict[str, int]]:
    """
    Measure tools/list payload for all four roles.

    Returns:
        Dict mapping role name -> measurement dict (byte_size, tiktoken_count, tool_count)
    """
    return {role: measure_role(role) for role in ROLE_PERMISSIONS}


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def _get_git_sha() -> str:
    """Return the current git HEAD SHA (short), or 'unknown' on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Failed to retrieve git SHA: %s", exc)
    return "unknown"


def build_report() -> Dict[str, Any]:
    """
    Build the full measurement report.

    Returns:
        Dict with keys:
        - generated_at: ISO8601 UTC timestamp string
        - git_sha: short git SHA of HEAD (or 'unknown')
        - roles: dict mapping role -> {byte_size, tiktoken_count, tool_count}
    """
    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "git_sha": _get_git_sha(),
        "roles": collect_measurements(),
    }


# ---------------------------------------------------------------------------
# File output
# ---------------------------------------------------------------------------


def write_report(
    report: Dict[str, Any],
    output_dir: Path = _REPO_ROOT / "reports" / "mcp",
) -> Path:
    """
    Write the report to a timestamped JSON file in output_dir.

    Creates output_dir if it does not exist.

    Args:
        report: Report dict from build_report()
        output_dir: Directory to write the JSON file into

    Returns:
        Path to the written file
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Use the timestamp from generated_at to build a filesystem-safe filename
    ts = report["generated_at"].replace(":", "-").replace("+", "").replace(" ", "T")
    # Strip sub-second precision and timezone offset for brevity
    ts_clean = ts.split(".")[0].replace("T", "_")
    filename = f"baseline_{ts_clean}.json"

    out_path = output_dir / filename
    out_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return out_path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the measurement and write the baseline report."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print("Building tool registry...")
    report = build_report()

    print(f"Git SHA: {report['git_sha']}")
    print(f"Generated at: {report['generated_at']}")
    print()

    roles_data = report["roles"]
    print(f"{'Role':<15} {'Tools':>6} {'Bytes':>10} {'Tokens':>10}")
    print("-" * 45)
    for role in ("anonymous", "normal_user", "power_user", "admin"):
        entry = roles_data[role]
        print(
            f"{role:<15} {entry['tool_count']:>6} "
            f"{entry['byte_size']:>10,} {entry['tiktoken_count']:>10,}"
        )

    out_path = write_report(report)
    print()
    print(f"Written to: {out_path}")


if __name__ == "__main__":
    main()
