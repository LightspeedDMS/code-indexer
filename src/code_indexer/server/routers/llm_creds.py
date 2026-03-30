"""
LLM Credentials Provider REST Router (Story #367).

Endpoints:
  POST /api/llm-creds/test-connection  — connectivity probe against provider
  GET  /api/llm-creds/lease-status     — current lease lifecycle status
  POST /api/llm-creds/save-config      — persist subscription config, start/stop lifecycle
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from code_indexer.server.auth.dependencies import get_current_admin_user_hybrid
from code_indexer.server.auth.user_manager import User
from code_indexer.server.services.llm_creds_client import LlmCredsClient

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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/test-connection", response_model=TestConnectionResponse)
def test_connection(
    request: TestConnectionRequest,
    _current_user: User = Depends(get_current_admin_user_hybrid),
) -> TestConnectionResponse:
    """Probe the LLM credentials provider for reachability."""
    try:
        client = LlmCredsClient(
            provider_url=request.provider_url,
            api_key=request.api_key,
        )
        healthy = client.health()
        if healthy:
            return TestConnectionResponse(success=True)
        return TestConnectionResponse(
            success=False, error="Provider responded but reported unhealthy status"
        )
    except Exception as exc:
        return TestConnectionResponse(success=False, error=str(exc))


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


@router.post("/save-config", response_model=SaveConfigResponse)
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

    # Validate subscription fields when switching to subscription mode
    if mode == "subscription":
        if not request.llm_creds_provider_url:
            return SaveConfigResponse(
                success=False,
                mode=mode,
                error="llm_creds_provider_url is required for subscription mode",
            )
        if not request.llm_creds_provider_api_key:
            return SaveConfigResponse(
                success=False,
                mode=mode,
                error="llm_creds_provider_api_key is required for subscription mode",
            )

    # Load and update config
    config_svc = get_config_service()
    config = config_svc.load_config()
    prev_mode = config.claude_integration_config.claude_auth_mode

    config.claude_integration_config.claude_auth_mode = mode
    config.claude_integration_config.llm_creds_provider_url = (
        request.llm_creds_provider_url
    )
    config.claude_integration_config.llm_creds_provider_api_key = (
        request.llm_creds_provider_api_key
    )
    config.claude_integration_config.llm_creds_provider_consumer_id = (
        request.llm_creds_provider_consumer_id
    )
    config_svc.save_config(config)

    # Lifecycle transitions
    existing_service = getattr(http_request.app.state, "llm_lifecycle_service", None)

    if mode == "subscription" and prev_mode != "subscription":
        # Switching TO subscription — build and start
        try:
            svc = _build_lifecycle_service(
                provider_url=request.llm_creds_provider_url,
                api_key=request.llm_creds_provider_api_key,
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
            logger.error("Failed to start LLM lease lifecycle: %s", exc)
            return SaveConfigResponse(success=False, mode=mode, error=str(exc))

    elif mode == "api_key" and prev_mode == "subscription":
        # Switching FROM subscription — stop existing lifecycle if present
        if existing_service is not None:
            try:
                existing_service.stop()
                http_request.app.state.llm_lifecycle_service = None
                logger.info("LLM lease lifecycle stopped via config save")
            except Exception as exc:
                logger.warning("Error stopping LLM lease lifecycle: %s", exc)

    elif mode == "subscription" and prev_mode == "subscription":
        # Same-mode re-save: restart lifecycle with new credentials
        old_svc = getattr(http_request.app.state, "llm_lifecycle_service", None)
        if old_svc is not None:
            try:
                old_svc.stop()
            except Exception as exc:
                logger.warning(
                    "Error stopping old LLM lease lifecycle during re-save: %s", exc
                )
        try:
            new_svc = _build_lifecycle_service(
                provider_url=request.llm_creds_provider_url,
                api_key=request.llm_creds_provider_api_key,
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
            logger.error("Failed to restart LLM lease lifecycle: %s", exc)
            return SaveConfigResponse(success=False, mode=mode, error=str(exc))

    return SaveConfigResponse(success=True, mode=mode)
