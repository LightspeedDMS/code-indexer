"""Shared MCP JSON-RPC helpers and constants for Phase 3 server tests.

All protocol-level string and numeric literals live here so test files
contain no inline magic values for endpoints, field names, status codes,
or pytest parametrize infrastructure.
"""

from __future__ import annotations

from typing import Union

import httpx
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# JSON type alias
# JsonValue is a recursive JSON-compatible type.  'Any' is avoided; the
# recursive structure accurately models the MCP arguments schema.
# ---------------------------------------------------------------------------
JsonValue = Union[
    str, int, float, bool, None, "list[JsonValue]", "dict[str, JsonValue]"
]
JsonArgs = dict[str, JsonValue]

# ---------------------------------------------------------------------------
# Endpoint and protocol constants
# ---------------------------------------------------------------------------
MCP_ENDPOINT: str = "/mcp"
JSONRPC_VERSION: str = "2.0"
MCP_METHOD: str = "tools/call"

# JSON-RPC request field names
FIELD_JSONRPC: str = "jsonrpc"
FIELD_ID: str = "id"
FIELD_METHOD: str = "method"
FIELD_PARAMS: str = "params"
FIELD_NAME: str = "name"
FIELD_ARGUMENTS: str = "arguments"
FIELD_RESULT: str = "result"
FIELD_ERROR: str = "error"

# HTTP status boundary constants
HTTP_OK: int = 200
HTTP_SERVER_ERROR: int = 500

# Request / pagination defaults
JSONRPC_REQUEST_ID: int = 1
DEFAULT_LIMIT: int = 3
MAX_ERROR_SNIPPET: int = 400

# ---------------------------------------------------------------------------
# Shared pytest parametrize constants
# Centralised here so test files do not embed inline index literals or
# parametrize field-name strings.
# ---------------------------------------------------------------------------
TOOL_LABEL_INDEX: int = 0
PARAMETRIZE_FIELDS: str = "label,tool,params"

# ---------------------------------------------------------------------------
# Tool call helper
# ---------------------------------------------------------------------------


def call_mcp_tool(
    client: TestClient,
    tool_name: str,
    arguments: JsonArgs,
    headers: dict,
) -> httpx.Response:
    """POST a tools/call JSON-RPC request and return the raw Response.

    All protocol literals are resolved from module constants so callers
    never assemble JSON-RPC payloads by hand.

    Args:
        client: Session-scoped FastAPI TestClient (must be a non-None TestClient).
        tool_name: MCP tool name (must be a non-empty string after strip).
        arguments: Tool-specific argument dict (must be a dict).
        headers: Authorization header dict (must be a dict).

    Returns:
        The raw httpx.Response object from TestClient.

    Raises:
        ValueError: If any argument fails its type or content validation.
    """
    if client is None or not isinstance(client, TestClient):
        raise ValueError(
            f"client must be a TestClient instance, got {type(client).__name__}"
        )
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise ValueError(f"tool_name must be a non-empty string, got {tool_name!r}")
    if not isinstance(arguments, dict):
        raise ValueError(f"arguments must be a dict, got {type(arguments).__name__}")
    if not isinstance(headers, dict):
        raise ValueError(f"headers must be a dict, got {type(headers).__name__}")

    return client.post(
        MCP_ENDPOINT,
        json={
            FIELD_JSONRPC: JSONRPC_VERSION,
            FIELD_ID: JSONRPC_REQUEST_ID,
            FIELD_METHOD: MCP_METHOD,
            FIELD_PARAMS: {FIELD_NAME: tool_name, FIELD_ARGUMENTS: arguments},
        },
        headers=headers,
    )
