"""
HTTP client for the CIDX performance test harness.

Story #333: Performance Test Harness with Single-User Baselines
AC2: HTTP Client with MCP JSON-RPC and REST Support
AC3: JWT Authentication with Proactive Refresh

Uses httpx.AsyncClient for all HTTP communication.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from metrics import RequestResult

# Token refresh threshold: 8 minutes (480 seconds) before 10-minute expiry
TOKEN_REFRESH_SECONDS = 480


@dataclass
class TokenTracker:
    """Tracks a JWT token and its acquisition timestamp for proactive refresh."""

    token: str
    acquired_at: float  # Unix timestamp when token was acquired

    def needs_refresh(self) -> bool:
        """Return True if the token is at or past the 8-minute mark."""
        elapsed = time.time() - self.acquired_at
        return elapsed >= TOKEN_REFRESH_SECONDS


def build_mcp_envelope(tool_name: str, arguments: dict[str, Any], request_id: int) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 envelope for an MCP tool/call request."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }


def build_rest_payload(endpoint: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """Build a REST API request body (parameters sent directly as JSON body)."""
    return dict(parameters)


def build_auth_headers(token: str) -> dict[str, str]:
    """Build HTTP headers with JWT Bearer authorization and Content-Type."""
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


class PerfClient:
    """
    Async HTTP client with JWT authentication and proactive token refresh.

    Handles MCP JSON-RPC and REST requests. Token is refreshed transparently
    at the 8-minute mark (before the 10-minute server expiry).
    """

    def __init__(self, server_url: str, username: str, password: str) -> None:
        self.server_url = server_url.rstrip("/")
        self.username = username
        self.password = password
        self._token_tracker: Optional[TokenTracker] = None
        self._request_counter = 0

    @property
    def mcp_url(self) -> str:
        return f"{self.server_url}/mcp"

    @property
    def auth_url(self) -> str:
        return f"{self.server_url}/auth/login"

    async def authenticate(self, client: httpx.AsyncClient) -> None:
        """Authenticate and store the JWT token. Raises RuntimeError on failure."""
        response = await client.post(
            self.auth_url,
            json={"username": self.username, "password": self.password},
        )
        if response.status_code != 200:
            raise RuntimeError(f"Authentication failed: HTTP {response.status_code} - {response.text}")
        data = response.json()
        token = data.get("access_token")
        if not token:
            raise RuntimeError(f"Authentication response missing 'access_token': {data}")
        self._token_tracker = TokenTracker(token=token, acquired_at=time.time())

    async def _ensure_valid_token(self, client: httpx.AsyncClient) -> str:
        """Return a valid token, refreshing if at the 8-minute threshold."""
        if self._token_tracker is None or self._token_tracker.needs_refresh():
            await self.authenticate(client)
        return self._token_tracker.token  # type: ignore[union-attr]

    def _next_request_id(self) -> int:
        self._request_counter += 1
        return self._request_counter

    async def execute_mcp(
        self, client: httpx.AsyncClient, tool_name: str, arguments: dict[str, Any]
    ) -> RequestResult:
        """Execute an MCP JSON-RPC tool call. HTTP/JSON-RPC errors are captured, not raised."""
        token = await self._ensure_valid_token(client)
        headers = build_auth_headers(token)
        envelope = build_mcp_envelope(tool_name, arguments, self._next_request_id())

        start_time = time.monotonic()
        try:
            response = await client.post(self.mcp_url, json=envelope, headers=headers)
            elapsed_ms = (time.monotonic() - start_time) * 1000.0
            response_bytes = len(response.content)

            if response.status_code >= 400:
                return RequestResult(
                    response_time_ms=elapsed_ms,
                    status_code=response.status_code,
                    success=False,
                    response_size_bytes=response_bytes,
                    error_message=f"HTTP {response.status_code}: {response.text[:200]}",
                )

            try:
                body = response.json()
                if "error" in body:
                    return RequestResult(
                        response_time_ms=elapsed_ms,
                        status_code=response.status_code,
                        success=False,
                        response_size_bytes=response_bytes,
                        error_message=f"JSON-RPC error: {body['error']}",
                    )
            except (ValueError, json.JSONDecodeError):
                # Response body is not valid JSON - HTTP status was OK (2xx), treat as success
                pass

            return RequestResult(
                response_time_ms=elapsed_ms,
                status_code=response.status_code,
                success=True,
                response_size_bytes=response_bytes,
            )

        except httpx.RequestError as exc:
            elapsed_ms = (time.monotonic() - start_time) * 1000.0
            return RequestResult(
                response_time_ms=elapsed_ms,
                status_code=0,
                success=False,
                response_size_bytes=0,
                error_message=f"Request error: {exc}",
            )

    async def execute_rest(
        self, client: httpx.AsyncClient, endpoint: str, parameters: dict[str, Any]
    ) -> RequestResult:
        """Execute a REST API request. HTTP errors (4xx/5xx) are captured, not raised."""
        token = await self._ensure_valid_token(client)
        headers = build_auth_headers(token)
        url = f"{self.server_url}{endpoint}"
        payload = build_rest_payload(endpoint, parameters)

        start_time = time.monotonic()
        try:
            response = await client.post(url, json=payload, headers=headers)
            elapsed_ms = (time.monotonic() - start_time) * 1000.0
            response_bytes = len(response.content)
            success = response.status_code < 400
            error_message = None if success else f"HTTP {response.status_code}: {response.text[:200]}"

            return RequestResult(
                response_time_ms=elapsed_ms,
                status_code=response.status_code,
                success=success,
                response_size_bytes=response_bytes,
                error_message=error_message,
            )

        except httpx.RequestError as exc:
            elapsed_ms = (time.monotonic() - start_time) * 1000.0
            return RequestResult(
                response_time_ms=elapsed_ms,
                status_code=0,
                success=False,
                response_size_bytes=0,
                error_message=f"Request error: {exc}",
            )
