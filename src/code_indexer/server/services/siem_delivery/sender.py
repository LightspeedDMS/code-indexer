"""Service-account credentials (the stored key) and the Chronicle sender.

The key is the one configured in the Web UI and stored encrypted in the
database (``credential.py``); the provider receives it through a loader and
builds google-auth credentials from the decrypted info dict.  Its
``token_uri`` is checked against the destination BEFORE any request is
made.  Key contents, tokens, headers and google-auth exception text never
reach a log line, column, audit row or API response: failures are reported
as a :class:`ProbeResult` enum and exception class names.

The credential cache is per-process wiring (one entry per destination and
stored credential id), not cross-request application state.
"""

from __future__ import annotations

import enum
import logging
import threading
from typing import Any, Callable, Dict, Optional, Tuple

import httpx

from code_indexer.server.services.siem_delivery.classifier import (
    Classification,
    HttpOutcome,
    classify,
)
from code_indexer.server.services.siem_delivery.destination import (
    SECOPS_SCOPE,
    Destination,
)
from code_indexer.server.services.siem_delivery.transport import (
    SiemDestinationNotAllowed,
    guarded_send,
    import_google_auth,
    make_auth_request,
)

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_TIMEOUT = 20.0


class ProbeResult(str, enum.Enum):
    PENDING = "pending"
    OK = "ok"
    CREDENTIAL_MISSING = "credential_missing"
    CREDENTIAL_INVALID = "credential_invalid"
    # stored under a different encryption key than this process derives
    CREDENTIAL_KEY_MISMATCH = "credential_key_mismatch"
    TOKEN_URI_NOT_ALLOWED = "token_uri_not_allowed"
    TOKEN_REJECTED = "token_rejected"
    TOKEN_ENDPOINT_UNREACHABLE = "token_endpoint_unreachable"


class CredentialError(Exception):
    """No token could be minted; carries the probe result enum only."""

    def __init__(self, result: ProbeResult) -> None:
        super().__init__(result.value)
        self.result = result


def _checked_info(stored: Any, dest: Destination) -> Dict[str, Any]:
    """The stored key's info dict, checked against *dest* before any use."""
    if stored is None:
        raise CredentialError(ProbeResult.CREDENTIAL_MISSING)
    info = stored.info
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise CredentialError(ProbeResult.CREDENTIAL_INVALID)
    if info.get("token_uri") != dest.token_uri:
        raise CredentialError(ProbeResult.TOKEN_URI_NOT_ALLOWED)
    return info


# Returns the stored credential (an object with ``credential_id`` and
# ``info``) or None; raises CredentialError when it cannot be decrypted.
CredentialLoader = Callable[[], Any]


class CredentialProvider:
    """Mints and caches access tokens for a destination (one per process)."""

    def __init__(
        self,
        http_factory: Any,
        loader: CredentialLoader,
        token_timeout: float = DEFAULT_TOKEN_TIMEOUT,
    ) -> None:
        self._http_factory = http_factory
        self._loader = loader
        self._token_timeout = token_timeout
        self._lock = threading.Lock()
        self._cache: Dict[Tuple[str, str, str], Any] = {}

    @staticmethod
    def _credentials(info: Dict[str, Any]) -> Any:
        google = import_google_auth()
        try:
            return google.oauth2.service_account.Credentials.from_service_account_info(
                info, scopes=[SECOPS_SCOPE]
            )
        except (ValueError, KeyError, TypeError):
            raise CredentialError(ProbeResult.CREDENTIAL_INVALID) from None

    def token(self, dest: Destination) -> str:
        """A valid access token; raises :class:`CredentialError`.

        The stored credential is read on every call (one row), so a
        Replace or Remove made through any process takes effect on the next
        request here."""
        stored = self._loader()
        info = _checked_info(stored, dest)
        key = (dest.key, dest.token_uri, str(stored.credential_id))
        with self._lock:
            creds = self._cache.get(key)
        if creds is None or not creds.valid:
            creds = self._credentials(info)
            self._refresh(creds, dest)
            with self._lock:
                self._cache = {key: creds}  # one live credential per process
        return str(creds.token)

    def _refresh(self, creds: Any, dest: Destination) -> None:
        google = import_google_auth()
        request = make_auth_request(self._http_factory, dest, self._token_timeout)
        try:
            creds.refresh(request)
        except SiemDestinationNotAllowed:
            raise CredentialError(ProbeResult.TOKEN_URI_NOT_ALLOWED) from None
        except google.auth.exceptions.RefreshError as exc:
            result = (
                ProbeResult.TOKEN_ENDPOINT_UNREACHABLE
                if getattr(exc, "retryable", False)
                else ProbeResult.TOKEN_REJECTED
            )
            raise CredentialError(result) from None
        except google.auth.exceptions.TransportError:
            raise CredentialError(ProbeResult.TOKEN_ENDPOINT_UNREACHABLE) from None
        except (ValueError, TypeError, AttributeError, KeyError):
            # the signer could not use the stored key (a probe result, never
            # an exception escaping the loop)
            raise CredentialError(ProbeResult.CREDENTIAL_INVALID) from None

    def invalidate(self, dest: Destination) -> None:
        with self._lock:
            self._cache = {k: v for k, v in self._cache.items() if k[0] != dest.key}

    def invalidate_all(self) -> None:
        """Forget every cached token (the stored key was set or removed)."""
        with self._lock:
            self._cache = {}

    def probe(self, dest: Destination) -> ProbeResult:
        """Mint a fresh token (no Chronicle call); the result enum only."""
        self.invalidate(dest)
        try:
            self.token(dest)
        except CredentialError as exc:
            logger.debug("SIEM credential probe: %s", exc.result.value)
            return exc.result
        return ProbeResult.OK


def _transport_error_kind(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.ConnectError):
        return "connect"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    return "reset"


def send_batch(
    http_factory: Any,
    dest: Destination,
    token: str,
    body: bytes,
    *,
    event_count: int,
    timeout: float,
) -> Classification:
    """POST the persisted bytes exactly; classify the outcome."""
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    try:
        response = guarded_send(
            http_factory,
            dest,
            "POST",
            dest.origin + dest.import_path,
            body,
            headers,
            timeout,
        )
    except httpx.HTTPError as exc:
        return classify(
            None, event_count=event_count, transport_error=_transport_error_kind(exc)
        )
    outcome = HttpOutcome(
        status=int(response.status_code),
        body=bytes(response.content),
        headers=dict(response.headers),
    )
    return classify(outcome, event_count=event_count)


def probe_label(result: Optional[ProbeResult]) -> str:
    return result.value if result is not None else ProbeResult.PENDING.value
