"""Destination-enforcing transport for every SIEM outbound request.

Both token and Chronicle requests pass through :func:`guarded_send`, which
compares the request with the destination's allowlist BEFORE connecting:

* token:  ``POST <dest.token_uri>`` exactly (Google's token URI deployed;
  ``<harness_endpoint>/token`` in harness mode);
* import: ``POST <dest.origin><dest.import_path>`` exactly.

Anything else raises :class:`SiemDestinationNotAllowed` (the URL is never
put in the message or a log line).  Redirects are never followed.  The
client comes from ``HttpClientFactory`` (non-pooled), so fault injection
applies as for every other outbound call.  ``google-auth`` is imported
lazily, only when the adapter is first built.
"""

from __future__ import annotations

import warnings
from typing import Any, Dict, Mapping, Optional

import httpx

from code_indexer.server.services.siem_delivery.destination import Destination


class SiemDestinationNotAllowed(Exception):
    """A request outside the destination allowlist was refused before connecting."""

    def __init__(self) -> None:
        super().__init__("SIEM request refused: destination not allowed")


def request_allowed(method: str, url: str, dest: Destination) -> bool:
    if method.upper() != "POST":
        return False
    return url in (dest.token_uri, dest.origin + dest.import_path)


def guarded_send(
    http_factory: Any,
    dest: Destination,
    method: str,
    url: str,
    body: Optional[bytes],
    headers: Mapping[str, str],
    timeout: float,
) -> httpx.Response:
    """Send one allowlisted request; raises before connecting otherwise."""
    if not request_allowed(method, url, dest):
        raise SiemDestinationNotAllowed()
    with http_factory.create_sync_client(
        timeout=timeout, follow_redirects=False
    ) as client:
        response: httpx.Response = client.request(
            method.upper(), url, content=body, headers=dict(headers)
        )
        return response


def import_google_auth() -> Any:
    """Import google-auth lazily, silencing only its interpreter-EOL notice."""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", category=FutureWarning, module=r"google\.(auth|oauth2)"
        )
        import google.auth.exceptions
        import google.auth.transport
        import google.oauth2.service_account

    return google


def make_auth_request(http_factory: Any, dest: Destination, timeout: float) -> Any:
    """A google-auth transport ``Request`` bound to *dest*'s allowlist."""
    google = import_google_auth()
    transport = google.auth.transport
    exceptions = google.auth.exceptions

    class _Response(transport.Response):  # type: ignore[misc,name-defined]
        def __init__(self, response: httpx.Response) -> None:
            self._response = response

        @property
        def status(self) -> int:
            return int(self._response.status_code)

        @property
        def headers(self) -> Dict[str, str]:
            return dict(self._response.headers)

        @property
        def data(self) -> bytes:
            return bytes(self._response.content)

    class _GuardedRequest(transport.Request):  # type: ignore[misc,name-defined]
        def __call__(
            self,
            url: str,
            method: str = "GET",
            body: Optional[bytes] = None,
            headers: Optional[Mapping[str, str]] = None,
            timeout: Optional[float] = None,
            **kwargs: Any,
        ) -> Any:
            try:
                response = guarded_send(
                    http_factory,
                    dest,
                    method,
                    url,
                    body,
                    headers or {},
                    timeout if timeout is not None else _default_timeout,
                )
            except httpx.HTTPError as exc:
                raise exceptions.TransportError(type(exc).__name__) from None
            return _Response(response)

    _default_timeout = timeout
    return _GuardedRequest()
