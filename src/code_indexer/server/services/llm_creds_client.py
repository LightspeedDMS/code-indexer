"""
LLM Credentials Provider HTTP Client (Story #365).

Communicates with an llm-creds-provider service to checkout and checkin
OAuth credentials for Claude CLI subscription access.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LlmCredsProviderError(Exception):
    """Base error for LLM credentials provider client."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class LlmCredsConnectionError(LlmCredsProviderError):
    """Provider unreachable (connection refused, timeout, DNS failure)."""

    pass


class LlmCredsAuthError(LlmCredsProviderError):
    """API key rejected (401/403)."""

    pass


# ---------------------------------------------------------------------------
# Response dataclass
# ---------------------------------------------------------------------------


@dataclass
class CheckoutResponse:
    """Response from the /checkout endpoint."""

    lease_id: str
    credential_id: str
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    api_key: Optional[str] = None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LlmCredsClient:
    """
    Synchronous HTTP client for the llm-creds-provider service.

    All requests include an ``x-api-key`` header for authentication.
    Raises typed exceptions on errors — never swallows failures.
    """

    def __init__(
        self,
        provider_url: str,
        api_key: str,
        timeout: float = 10.0,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        """
        Initialise the client.

        Args:
            provider_url: Base URL of the llm-creds-provider (e.g. ``http://host:8080``).
            api_key: API key sent as ``x-api-key`` header on every request.
            timeout: Request timeout in seconds (default 10.0).
            transport: Optional httpx transport — used for testing with
                       ``httpx.MockTransport``.  If omitted, a real HTTP
                       transport is used.
        """
        self._base_url = provider_url.rstrip("/")
        self._headers = {"x-api-key": api_key}
        self._timeout = httpx.Timeout(timeout)
        self._transport = transport

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _client(self) -> httpx.Client:
        """Build an httpx.Client with the configured headers and transport."""
        kwargs: dict = {
            "headers": self._headers,
            "timeout": self._timeout,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.Client(**kwargs)

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Raise a typed exception for non-2xx responses."""
        if response.status_code in (401, 403):
            raise LlmCredsAuthError(
                f"Authentication failed: HTTP {response.status_code}",
                status_code=response.status_code,
            )
        if not response.is_success:
            raise LlmCredsProviderError(
                f"Provider returned HTTP {response.status_code}",
                status_code=response.status_code,
            )

    def _handle_transport_errors(self, exc: Exception) -> None:
        """Convert httpx transport-level errors into typed exceptions."""
        if isinstance(exc, httpx.ConnectError):
            raise LlmCredsConnectionError(
                f"Cannot connect to LLM credentials provider: {exc}"
            ) from exc
        if isinstance(exc, httpx.TimeoutException):
            raise LlmCredsConnectionError(
                f"Request to LLM credentials provider timed out: {exc}"
            ) from exc
        raise exc

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def health(self) -> bool:
        """
        Check whether the provider is reachable and healthy.

        Returns:
            ``True`` if the provider responds with HTTP 2xx, ``False``
            for any non-success status.

        Raises:
            LlmCredsConnectionError: If the provider is unreachable or times out.
        """
        url = f"{self._base_url}/health"
        try:
            with self._client() as client:
                response = client.get(url)
                return response.is_success
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            self._handle_transport_errors(exc)
            return False  # unreachable; _handle_transport_errors always raises

    def checkout(self, vendor: str, consumer_id: str) -> CheckoutResponse:
        """
        Checkout a credential lease from the provider.

        Args:
            vendor: Credential vendor (e.g. ``"anthropic"``).
            consumer_id: Unique identifier for this consumer (e.g. server hostname).

        Returns:
            A :class:`CheckoutResponse` with lease and token details.

        Raises:
            LlmCredsConnectionError: Provider unreachable or timed out.
            LlmCredsAuthError: API key rejected (HTTP 401/403).
            LlmCredsProviderError: Any other non-2xx response.
        """
        url = f"{self._base_url}/checkout"
        payload = {"vendor": vendor, "consumer_id": consumer_id}
        try:
            with self._client() as client:
                response = client.post(url, json=payload)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            self._handle_transport_errors(exc)
            raise  # unreachable; _handle_transport_errors always raises

        self._raise_for_status(response)

        try:
            data = response.json()
        except Exception as exc:
            raise LlmCredsProviderError(
                f"Provider returned invalid JSON: {exc}",
                status_code=response.status_code,
            ) from exc

        try:
            return CheckoutResponse(
                lease_id=data["lease_id"],
                credential_id=data["credential_id"],
                access_token=data.get("access_token"),
                refresh_token=data.get("refresh_token"),
                api_key=data.get("api_key"),
            )
        except KeyError as exc:
            raise LlmCredsProviderError(
                f"Provider response missing required field: {exc}",
                status_code=response.status_code,
            ) from exc

    def checkin(
        self,
        lease_id: str,
        credential_id: Optional[str] = None,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
    ) -> None:
        """
        Return a credential lease to the provider, optionally writing back
        refreshed tokens.

        Args:
            lease_id: The lease ID obtained from :meth:`checkout`.
            credential_id: Credential ID for token writeback (optional).
            access_token: Updated access token (optional, for writeback).
            refresh_token: Updated refresh token (optional, for writeback).

        Raises:
            LlmCredsConnectionError: Provider unreachable or timed out.
            LlmCredsAuthError: API key rejected (HTTP 401/403).
            LlmCredsProviderError: Any other non-2xx response.
        """
        url = f"{self._base_url}/checkin"
        payload: dict = {"lease_id": lease_id}
        if credential_id is not None:
            payload["credential_id"] = credential_id
        if access_token is not None:
            payload["access_token"] = access_token
        if refresh_token is not None:
            payload["refresh_token"] = refresh_token

        try:
            with self._client() as client:
                response = client.post(url, json=payload)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            self._handle_transport_errors(exc)
            raise  # unreachable; _handle_transport_errors always raises

        self._raise_for_status(response)
