"""
LLM Credentials Provider REST Router (Story #367).

Endpoints:
  POST /api/llm-creds/test-connection  — connectivity probe against provider
  GET  /api/llm-creds/lease-status     — current lease lifecycle status
  POST /api/llm-creds/save-config      — persist subscription config, start/stop lifecycle
"""

from __future__ import annotations

import logging
import traceback
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.dependencies import get_current_admin_user_hybrid
from code_indexer.server.auth.user_manager import User
from code_indexer.server.services.llm_creds_client import (
    LlmCredsClient,
    LlmCredsProviderError,
)
from code_indexer.utils.credential_redaction import is_display_mask

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/llm-creds", tags=["LLM Credentials"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class TestConnectionRequest(BaseModel):
    provider_url: str = Field(..., description="Base URL of the llm-creds-provider")
    api_key: str = Field(..., description="API key for the provider")


class TestConnectionResponse(BaseModel):
    success: bool
    error: Optional[str] = None


class LeaseStatusResponse(BaseModel):
    status: str  # "inactive" | "active" | "degraded" | "shutting_down"
    lease_id: Optional[str] = None
    credential_id: Optional[str] = None  # masked
    error: Optional[str] = None


class SaveConfigRequest(BaseModel):
    claude_auth_mode: str = Field(..., description="'api_key' or 'subscription'")
    llm_creds_provider_url: str = Field(default="")
    llm_creds_provider_api_key: str = Field(default="")
    llm_creds_provider_consumer_id: str = Field(default="cidx-server")


class SaveConfigResponse(BaseModel):
    success: bool
    mode: str
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Helper: build lifecycle service (extracted for test patching)
# ---------------------------------------------------------------------------


def _build_lifecycle_service(provider_url: str, api_key: str):
    """Construct a fresh LlmLeaseLifecycleService from config values."""
    if not api_key.strip():
        raise ValueError(
            "refusing to build the LLM lease lifecycle without a provider API key"
        )
    from code_indexer.server.services.llm_creds_client import LlmCredsClient
    from code_indexer.server.config.llm_lease_state import LlmLeaseStateManager
    from code_indexer.server.services.claude_credentials_file_manager import (
        ClaudeCredentialsFileManager,
    )
    from code_indexer.server.services.llm_lease_lifecycle import (
        LlmLeaseLifecycleService,
    )

    client = LlmCredsClient(provider_url=provider_url, api_key=api_key)
    return LlmLeaseLifecycleService(
        client=client,
        state_manager=LlmLeaseStateManager(),
        credentials_manager=ClaudeCredentialsFileManager(),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_config_service():
    from code_indexer.server.services.config_service import (
        get_config_service as _get,
    )

    return _get()


def _mask_credential_id(cred_id: Optional[str]) -> Optional[str]:
    """Return first 8 chars + '...' for display."""
    if not cred_id:
        return None
    prefix = cred_id[:8]
    return f"{prefix}..."


_NO_KEY_FOR_URL = (
    "No provider API key is stored for this provider URL; enter the key to test it"
)


def _same_provider_url(first: str, second: str) -> bool:
    """The stored provider key is bound to the stored provider URL: ANY
    change of URL (scheme, host, port or path) is a different provider."""
    return first.strip() == second.strip()


def _stored_key_for(provider_url: str) -> str:
    """The COMMITTED stored provider key, only when *provider_url* is the
    stored provider URL: a test never sends the stored key to another host."""
    _version, section = get_config_service().read_committed_section(
        "claude_integration_config"
    )
    stored_url = str(section.get("llm_creds_provider_url") or "")
    if not stored_url.strip() or not _same_provider_url(provider_url, stored_url):
        return ""
    return str(section.get("llm_creds_provider_api_key") or "").strip()


def _redact(text: str, key: str) -> str:
    """*text* with every spelling of *key* masked: raw, repr-escaped (an
    HTTP layer quotes a rejected header value escaped) and bytes-escaped."""
    if not key:
        return text
    escaped = repr(key)[1:-1]
    byte_escaped = repr(key.encode("utf-8", "backslashreplace"))[2:-1]
    for form in sorted({key, escaped, byte_escaped}, key=len, reverse=True):
        text = text.replace(form, "***")
    return text


def _failure_text(what: str, exc: Exception, key: str) -> str:
    """The error text safe to return for *exc*: a provider error's own
    message with *key* redacted; any other exception (whose text may quote a
    request header) only as ``"<what> (<ExceptionClass>)"``, its traceback
    logged with *key* redacted."""
    if isinstance(exc, LlmCredsProviderError):
        return _redact(str(exc), key)
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logger.error("%s (%s):\n%s", what, type(exc).__name__, _redact(trace, key))
    return f"{what} ({type(exc).__name__})"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/test-connection", response_model=TestConnectionResponse)
def test_connection(
    request: TestConnectionRequest,
    http_request: Request,
    _current_user: User = Depends(get_current_admin_user_hybrid),
) -> TestConnectionResponse:
    """Probe the LLM credentials provider for reachability.

    A blank (or display-mask) key tests with the stored key, as the
    configuration page's blank-means-keep rule implies -- and so requires
    the same TOTP elevation as saving it; a typed key needs none.  The
    stored key is never returned, not even inside an error message.
    """
    api_key = request.api_key.strip()
    use_stored_key = not api_key or is_display_mask(api_key)
    if use_stored_key:
        dependencies.require_elevation()(http_request, _current_user)
        api_key = ""
    try:
        if use_stored_key:
            api_key = _stored_key_for(request.provider_url)
            if not api_key:
                return TestConnectionResponse(success=False, error=_NO_KEY_FOR_URL)
        client = LlmCredsClient(provider_url=request.provider_url, api_key=api_key)
        healthy = client.health()
        if healthy:
            return TestConnectionResponse(success=True)
        return TestConnectionResponse(
            success=False, error="Provider responded but reported unhealthy status"
        )
    except Exception as exc:
        return TestConnectionResponse(
            success=False,
            error=_failure_text("Connection test failed", exc, api_key),
        )


@router.get("/lease-status", response_model=LeaseStatusResponse)
def lease_status(
    http_request: Request,
    _current_user: User = Depends(get_current_admin_user_hybrid),
) -> LeaseStatusResponse:
    """Return the current LLM lease lifecycle status."""
    service = getattr(http_request.app.state, "llm_lifecycle_service", None)
    if service is None:
        return LeaseStatusResponse(status="inactive")

    info = service.get_status()
    return LeaseStatusResponse(
        status=info.status.value,
        lease_id=info.lease_id,
        credential_id=_mask_credential_id(info.credential_id),
        error=info.error,
    )


_KEY_REQUIRED = "llm_creds_provider_api_key is required for subscription mode"
_KEY_REQUIRED_FOR_NEW_URL = "Enter the provider API key when changing the provider URL"
_KEY_NOT_PRINTABLE = (
    "The provider API key must be printable ASCII with no spaces or control characters"
)


def _is_printable_key(key: str) -> bool:
    """True when every character is printable ASCII other than space: a key
    is sent in an HTTP header, where anything else is rejected (and quoted
    back in the error)."""
    return all("!" <= char <= "~" for char in key)


class _LlmCredsChangeRefused(Exception):
    """The LLM-creds change was refused on the committed configuration;
    the message is the error shown to the admin."""


@router.post(
    "/save-config",
    response_model=SaveConfigResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def save_config(
    request: SaveConfigRequest,
    http_request: Request,
    _current_user: User = Depends(get_current_admin_user_hybrid),
) -> SaveConfigResponse:
    """Persist subscription configuration and start/stop lifecycle accordingly."""
    mode = request.claude_auth_mode
    if mode not in ("api_key", "subscription"):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid claude_auth_mode '{mode}'. Must be 'api_key' or 'subscription'.",
        )

    # A blank (or re-submitted display-mask) key means "keep the stored key",
    # the configuration page's rule for every stored secret.
    submitted_key = request.llm_creds_provider_api_key.strip()
    replaces_key = bool(submitted_key) and not is_display_mask(submitted_key)
    if replaces_key and not _is_printable_key(submitted_key):
        return SaveConfigResponse(success=False, mode=mode, error=_KEY_NOT_PRINTABLE)

    config_svc = get_config_service()
    current = config_svc.get_config().claude_integration_config
    prev_mode = current.claude_auth_mode

    # Validate subscription fields when switching to subscription mode (an
    # early refusal on this process's view; the committed row is re-checked
    # inside the change below).
    if mode == "subscription":
        if not request.llm_creds_provider_url:
            return SaveConfigResponse(
                success=False,
                mode=mode,
                error="llm_creds_provider_url is required for subscription mode",
            )
        if not replaces_key and not current.llm_creds_provider_api_key:
            return SaveConfigResponse(success=False, mode=mode, error=_KEY_REQUIRED)

    # Update config: one audited change on a candidate copy (the provider API
    # key is recorded by key name only).  The candidate is the COMMITTED row,
    # so both key rules are judged there: another writer may have changed the
    # key or URL after this process cached them.
    committed_key: list[str] = []

    def _set_llm_creds(candidate) -> None:
        integration = candidate.claude_integration_config
        # A kept key stays bound to its provider URL: it is never kept for
        # (and so never sent to) another host.
        if (
            not replaces_key
            and integration.llm_creds_provider_api_key
            and not _same_provider_url(
                integration.llm_creds_provider_url, request.llm_creds_provider_url
            )
        ):
            raise _LlmCredsChangeRefused(_KEY_REQUIRED_FOR_NEW_URL)
        integration.claude_auth_mode = mode
        integration.llm_creds_provider_url = request.llm_creds_provider_url
        if replaces_key:
            integration.llm_creds_provider_api_key = submitted_key
        integration.llm_creds_provider_consumer_id = (
            request.llm_creds_provider_consumer_id
        )
        if mode == "subscription" and not integration.llm_creds_provider_api_key:
            raise _LlmCredsChangeRefused(_KEY_REQUIRED)
        committed_key[:] = [integration.llm_creds_provider_api_key]

    try:
        config_svc.apply_audited_change(
            _set_llm_creds,
            actor=_current_user.username,
            target_id="claude_integration",
        )
    except _LlmCredsChangeRefused as refused:
        return SaveConfigResponse(success=False, mode=mode, error=str(refused))
    # The key the lifecycle authenticates with: the new one, else the one the
    # change was committed over.
    effective_key = submitted_key if replaces_key else committed_key[-1]

    # Lifecycle transitions.  No error text returned or logged below may carry
    # a provider key: the new one, or the one the old lifecycle used.
    def _log_stop_failure(what: str, exc: Exception) -> None:
        message = _redact(str(exc), effective_key)
        message = _redact(message, current.llm_creds_provider_api_key)
        logger.warning("%s (%s): %s", what, type(exc).__name__, message)

    existing_service = getattr(http_request.app.state, "llm_lifecycle_service", None)

    if mode == "subscription" and prev_mode != "subscription":
        # Switching TO subscription — build and start
        try:
            svc = _build_lifecycle_service(
                provider_url=request.llm_creds_provider_url,
                api_key=effective_key,
            )
            svc.start(
                consumer_id=request.llm_creds_provider_consumer_id or "cidx-server"
            )
            http_request.app.state.llm_lifecycle_service = svc
            logger.info(
                "LLM lease lifecycle started via config save: %s",
                svc.get_status().status.value,
            )
        except Exception as exc:
            error = _failure_text(
                "LLM lease lifecycle start failed", exc, effective_key
            )
            logger.error("Failed to start LLM lease lifecycle: %s", error)
            return SaveConfigResponse(success=False, mode=mode, error=error)

    elif mode == "api_key" and prev_mode == "subscription":
        # Switching FROM subscription — stop existing lifecycle if present
        if existing_service is not None:
            try:
                existing_service.stop()
                http_request.app.state.llm_lifecycle_service = None
                logger.info("LLM lease lifecycle stopped via config save")
            except Exception as exc:
                _log_stop_failure("Error stopping LLM lease lifecycle", exc)

    elif mode == "subscription" and prev_mode == "subscription":
        # Same-mode re-save: restart lifecycle with new credentials
        old_svc = getattr(http_request.app.state, "llm_lifecycle_service", None)
        if old_svc is not None:
            try:
                old_svc.stop()
            except Exception as exc:
                _log_stop_failure(
                    "Error stopping old LLM lease lifecycle during re-save", exc
                )
        try:
            new_svc = _build_lifecycle_service(
                provider_url=request.llm_creds_provider_url,
                api_key=effective_key,
            )
            new_svc.start(
                consumer_id=request.llm_creds_provider_consumer_id or "cidx-server"
            )
            http_request.app.state.llm_lifecycle_service = new_svc
            logger.info(
                "LLM lease lifecycle restarted via same-mode config save: %s",
                new_svc.get_status().status.value,
            )
        except Exception as exc:
            error = _failure_text(
                "LLM lease lifecycle restart failed", exc, effective_key
            )
            logger.error("Failed to restart LLM lease lifecycle: %s", error)
            return SaveConfigResponse(success=False, mode=mode, error=error)

    return SaveConfigResponse(success=True, mode=mode)
