"""JSON-RPC client for sandbox-mcp's fixed platform tools.

cidx hands executions it must not run in its own pod to sandbox-mcp: the SCIP
compile step today (``sandbox_scip``, see ``scip/indexers/delegated.py``), and
X-Ray's evaluator compile + scan once ``sandbox_xray`` exists. Every such call
has the same transport -- one JSON-RPC ``tools/call`` POST carrying a bearer
``api_key`` against the sandbox-mcp base ``endpoint`` -- and the same reply, a
``SandboxRunResult`` envelope (``exit_code``, ``output``, ``duration_ms``,
``errors``). This module owns that transport so each consumer only builds its
tool's ``arguments`` and interprets the envelope.

Only a fixed, named tool is ever called (never an agent-chosen exec string).
The endpoint comes from ``utils.remote_endpoint``; which config key supplies it
is the startup wiring's business, not this client's. No API-GW authorizer
secret is distributed to cidx; the route is constrained by K8s NetworkPolicy +
service account.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Optional

from code_indexer.utils.remote_endpoint import RemoteServiceEndpoint

logger = logging.getLogger(__name__)

# Path appended to the sandbox-mcp base endpoint for the JSON-RPC front door.
# ``SandboxMcpController`` mounts the MCP handler at ``POST /v1/sandbox/mcp``
# (URI versioning prefix 'v', v1).
DEFAULT_JSONRPC_PATH = "/v1/sandbox/mcp"

# Health is un-versioned and un-authed (``GET /health``, no ``/v1`` prefix).
DEFAULT_HEALTH_PATH = "/health"

# Timeout for the health probe; the tool call's own timeout is per client.
_HEALTH_TIMEOUT_SECONDS = 10

# How much of an HTTP error body / an envelope-less payload an error carries.
_ERROR_BODY_CHARS = 500


class SandboxMcpError(Exception):
    """A ``tools/call`` that produced no ``SandboxRunResult`` envelope.

    Raised for a transport error, an HTTP status >= 300, a non-JSON body, or a
    JSON-RPC error / envelope-less payload. The message is the operator-facing
    reason; callers turn it into their own failure result, never a fallback.
    """


def extract_run_result(payload: Any) -> Optional[Dict[str, Any]]:
    """Pull the ``SandboxRunResult`` envelope out of a JSON-RPC response.

    Handles the three shapes the tool-exec path may return: the envelope inline
    under ``result``, MCP ``structuredContent``, or a JSON string in
    ``result.content[0].text``. Returns ``None`` when the payload is a JSON-RPC
    error or the envelope cannot be located.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("error") is not None:
        return None

    result = payload.get("result", payload)
    if isinstance(result, dict):
        if "exit_code" in result:
            return result
        structured = result.get("structuredContent")
        if isinstance(structured, dict) and "exit_code" in structured:
            return structured
        content = result.get("content")
        if isinstance(content, list) and content:
            first = content[0]
            text = first.get("text") if isinstance(first, dict) else None
            if isinstance(text, str):
                try:
                    parsed = json.loads(text)
                except (ValueError, TypeError):
                    return None
                if isinstance(parsed, dict) and "exit_code" in parsed:
                    return parsed
    return None


def _import_requests() -> Any:
    """Return the lazily-imported ``requests`` module (keeps startup fast)."""
    import requests  # noqa: PLC0415

    return requests


class SandboxMcpClient:
    """Calls one sandbox-mcp deployment's fixed tools over JSON-RPC."""

    def __init__(
        self,
        config: RemoteServiceEndpoint,
        *,
        timeout_seconds: int,
        jsonrpc_path: str = DEFAULT_JSONRPC_PATH,
        health_path: str = DEFAULT_HEALTH_PATH,
        requests_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        """
        Args:
            config: Anything exposing ``endpoint`` and ``api_key``.
                ``endpoint`` is the sandbox-mcp *base* URL (e.g.
                ``http://sandbox-mcp:8200``); the paths below are appended.
            timeout_seconds: HTTP timeout for a ``tools/call`` POST.
            jsonrpc_path: Path of the JSON-RPC POST under ``endpoint``.
            health_path: Path of the health probe under ``endpoint``.
            requests_provider: Returns the ``requests``-compatible module to
                use; called per request. Defaults to a lazy ``import requests``.
        """
        self.endpoint = (config.endpoint or "").rstrip("/")
        self.api_key = config.api_key or ""
        self.timeout_seconds = timeout_seconds
        self._jsonrpc_path = jsonrpc_path
        self._health_path = health_path
        self._requests_provider = requests_provider or _import_requests
        self._request_id = 0

    def headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def rpc_url(self) -> str:
        return f"{self.endpoint}{self._jsonrpc_path}"

    def build_tool_call(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        """The JSON-RPC ``tools/call`` request for *tool_name*, with a fresh id."""
        self._request_id += 1
        return {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }

    def post(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """POST a built ``tools/call`` *request*; return its run-result envelope.

        Raises:
            SandboxMcpError: no envelope came back (see the class docstring).
                The tool's own failure (a non-zero ``exit_code``) is NOT an
                error here -- it is in the returned envelope.
        """
        tool_name = request["params"]["name"]
        requests = self._requests_provider()
        try:
            response = requests.post(
                self.rpc_url(),
                json=request,
                headers=self.headers(),
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise SandboxMcpError(
                f"{tool_name} request to '{self.rpc_url()}' failed: {exc}"
            ) from exc

        if response.status_code >= 300:
            body = ""
            try:
                body = response.text[:_ERROR_BODY_CHARS]
            except Exception:  # pragma: no cover - defensive
                pass
            raise SandboxMcpError(
                f"{tool_name} failed (HTTP {response.status_code}): {body}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise SandboxMcpError(
                f"{tool_name} returned non-JSON response: {exc}"
            ) from exc

        run_result = extract_run_result(payload)
        if run_result is None:
            raise SandboxMcpError(
                f"{tool_name} response contained no SandboxRunResult envelope: "
                f"{json.dumps(payload)[:_ERROR_BODY_CHARS]}"
            )
        return run_result

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Build and POST one ``tools/call``; see :meth:`post`."""
        return self.post(self.build_tool_call(tool_name, arguments))

    def health_url(self) -> str:
        return f"{self.endpoint}{self._health_path}"

    def is_healthy(self) -> bool:
        """True iff the health endpoint answers 2xx.

        False without an endpoint, on any transport error (logged), or on
        non-2xx. Callers decide whether an unhealthy sandbox fails startup.
        """
        if not self.endpoint:
            return False
        requests = self._requests_provider()
        try:
            resp = requests.get(
                self.health_url(),
                headers=self.headers(),
                timeout=_HEALTH_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            logger.warning(
                "SandboxMcpClient: health probe to '%s' failed: %s",
                self.health_url(),
                exc,
            )
            return False
        return bool(resp.status_code < 300)
