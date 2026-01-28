"""
API Keys REST API Router.

Provides endpoints for API key management (Anthropic and VoyageAI).

Story #20: API Key Management for Claude CLI and VoyageAI
"""

import logging
import os
import re
import json
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.requests import Request
from ..auth.dependencies import get_current_admin_user_hybrid
from ..auth.user_manager import User
from ..services.api_key_management import (
    ApiKeyValidator,
    ApiKeySyncService,
    ApiKeyConnectivityTester,
)
from ..services.config_service import ConfigService

logger = logging.getLogger(__name__)


def trigger_catchup_on_api_key_save(api_key: Optional[str]) -> bool:
    """
    Trigger immediate catch-up processing when API key is saved.

    Updates the global ClaudeCliManager with the new API key and triggers
    catch-up processing in a background thread to process any repos that
    were registered before the API key was configured.

    Args:
        api_key: The new API key (must be non-empty to trigger catch-up)

    Returns:
        True if catch-up was triggered, False if skipped (no manager or invalid key)

    Story #23 AC3: Immediate Catch-Up Trigger on API Key Save
    """
    import threading
    from ..services.claude_cli_manager import get_claude_cli_manager
    from ..middleware.correlation import get_correlation_id

    # Validate key
    if not api_key:
        logger.debug("Skipping catch-up trigger: no API key provided")
        return False

    # Get global manager
    manager = get_claude_cli_manager()
    if manager is None:
        logger.warning(
            "Cannot trigger catch-up: ClaudeCliManager not initialized",
            extra={"correlation_id": get_correlation_id()},
        )
        return False

    # Update the manager's API key
    manager.update_api_key(api_key)

    # Trigger catch-up in background thread
    def run_catchup():
        try:
            logger.info(
                "Starting immediate catch-up processing after API key save",
                extra={"correlation_id": get_correlation_id()},
            )
            result = manager.process_all_fallbacks()
            if result.processed:
                logger.info(
                    f"Catch-up completed: processed {len(result.processed)} repos",
                    extra={"correlation_id": get_correlation_id()},
                )
            elif result.error:
                logger.warning(
                    f"Catch-up partially completed: {result.error}",
                    extra={"correlation_id": get_correlation_id()},
                )
            else:
                logger.info(
                    "Catch-up completed: no repos needed processing",
                    extra={"correlation_id": get_correlation_id()},
                )
        except Exception as e:
            logger.error(
                f"Catch-up processing failed: {e}",
                exc_info=True,
                extra={"correlation_id": get_correlation_id()},
            )

    catchup_thread = threading.Thread(
        target=run_catchup,
        name="ImmediateCatchupProcessor",
        daemon=True,
    )
    catchup_thread.start()

    logger.info(
        "API key updated, immediate catch-up triggered in background",
        extra={"correlation_id": get_correlation_id()},
    )
    return True


# Request/Response Models
class SaveApiKeyRequest(BaseModel):
    """Request to save an API key."""

    api_key: str = Field(..., description="The API key to save")


class SaveApiKeyResponse(BaseModel):
    """Response after saving an API key."""

    success: bool
    provider: str
    already_synced: bool = False
    error: Optional[str] = None


class TestApiKeyRequest(BaseModel):
    """Request to test an API key connectivity."""

    api_key: str = Field(..., description="The API key to test")


class TestApiKeyResponse(BaseModel):
    """Response from API key connectivity test."""

    success: bool
    provider: str
    error: Optional[str] = None
    response_time_ms: Optional[int] = None


class ApiKeysStatusResponse(BaseModel):
    """Response with API key configuration status."""

    anthropic_configured: bool
    voyageai_configured: bool


# Singleton service instances
_api_key_sync_service: Optional[ApiKeySyncService] = None
_api_key_connectivity_tester: Optional[ApiKeyConnectivityTester] = None


def get_api_key_sync_service() -> ApiKeySyncService:
    """Get or create the API key sync service instance."""
    global _api_key_sync_service
    if _api_key_sync_service is None:
        _api_key_sync_service = ApiKeySyncService()
    return _api_key_sync_service


def get_api_key_connectivity_tester() -> ApiKeyConnectivityTester:
    """Get or create the API key connectivity tester instance."""
    global _api_key_connectivity_tester
    if _api_key_connectivity_tester is None:
        _api_key_connectivity_tester = ApiKeyConnectivityTester()
    return _api_key_connectivity_tester


def get_config_service() -> ConfigService:
    """Get the config service instance."""
    from ..services.config_service import get_config_service as _get_config_service

    return _get_config_service()


# Helper functions for key clearing
def _clear_from_claude_config(key_to_clear: str) -> Optional[str]:
    """Clear apiKey from ~/.claude.json if it matches. Returns location name if cleared."""
    try:
        claude_config_path = Path.home() / ".claude.json"
        if claude_config_path.exists():
            with open(claude_config_path, "r") as f:
                claude_config = json.load(f)
            if claude_config.get("apiKey") == key_to_clear:
                del claude_config["apiKey"]
                with open(claude_config_path, "w") as f:
                    json.dump(claude_config, f, indent=2)
                return "~/.claude.json"
    except Exception as e:
        logger.warning(f"Could not check/clear apiKey from ~/.claude.json: {e}")
    return None


def _clear_from_rc_files(key_to_clear: str, env_var_name: str) -> List[str]:
    """Clear matching export from ~/.bashrc and ~/.profile. Returns list of cleared locations."""
    cleared = []
    for rc_file in [".bashrc", ".profile"]:
        try:
            rc_path = Path.home() / rc_file
            if rc_path.exists():
                content = rc_path.read_text()
                # Match export VAR="key" or export VAR='key' or export VAR=key
                pattern = rf'^export\s+{env_var_name}=["\']?{re.escape(key_to_clear)}["\']?\s*$'
                new_content, count = re.subn(pattern, '', content, flags=re.MULTILINE)
                if count > 0:
                    new_content = re.sub(r'\n{3,}', '\n\n', new_content)
                    rc_path.write_text(new_content)
                    cleared.append(f"~/{rc_file}")
        except Exception as e:
            logger.warning(f"Could not check/clear from ~/{rc_file}: {e}")
    return cleared


# Router
router = APIRouter(prefix="/api/api-keys", tags=["API Keys"])


@router.post("/anthropic", response_model=SaveApiKeyResponse)
def save_anthropic_key(
    request: SaveApiKeyRequest,
    http_request: Request,
    _current_user: User = Depends(get_current_admin_user_hybrid),
) -> SaveApiKeyResponse:
    """
    Save and sync Anthropic API key.

    Validates format, then syncs to:
    - ~/.claude.json
    - os.environ["ANTHROPIC_API_KEY"]
    - systemd environment file
    """
    # Validate format
    validation = ApiKeyValidator.validate_anthropic_format(request.api_key)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)

    # Sync key
    sync_service = get_api_key_sync_service()
    result = sync_service.sync_anthropic_key(request.api_key)

    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)

    # Persist to server config
    config_service = get_config_service()
    config = config_service.load_config()
    config.claude_integration_config.anthropic_api_key = request.api_key
    config_service.config_manager.save_config(config)

    # Trigger immediate catch-up processing (Story #23, AC3)
    # This replaces the old flag-based deferred reconciliation (Story #20)
    try:
        trigger_catchup_on_api_key_save(request.api_key)
    except Exception as e:
        logger.debug(f"Could not trigger immediate catch-up: {e}")

    return SaveApiKeyResponse(
        success=True,
        provider="anthropic",
        already_synced=result.already_synced,
    )


@router.post("/voyageai", response_model=SaveApiKeyResponse)
def save_voyageai_key(
    request: SaveApiKeyRequest,
    http_request: Request,
    _current_user: User = Depends(get_current_admin_user_hybrid),
) -> SaveApiKeyResponse:
    """
    Save and sync VoyageAI API key.

    Validates format, then syncs to:
    - os.environ["VOYAGE_API_KEY"]
    - systemd environment file
    """
    # Validate format
    validation = ApiKeyValidator.validate_voyageai_format(request.api_key)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)

    # Sync key
    sync_service = get_api_key_sync_service()
    result = sync_service.sync_voyageai_key(request.api_key)

    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)

    # Persist to server config
    config_service = get_config_service()
    config = config_service.load_config()
    config.claude_integration_config.voyageai_api_key = request.api_key
    config_service.config_manager.save_config(config)

    return SaveApiKeyResponse(
        success=True,
        provider="voyageai",
        already_synced=result.already_synced,
    )


@router.post("/anthropic/test", response_model=TestApiKeyResponse)
async def test_anthropic_key(
    request: TestApiKeyRequest,
    http_request: Request,
    _current_user: User = Depends(get_current_admin_user_hybrid),
) -> TestApiKeyResponse:
    """
    Test Anthropic API key connectivity.

    Makes a test call via Claude CLI to verify the key works.
    """
    # Validate format first
    validation = ApiKeyValidator.validate_anthropic_format(request.api_key)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)

    # Test connectivity
    tester = get_api_key_connectivity_tester()
    result = await tester.test_anthropic_connectivity(request.api_key)

    return TestApiKeyResponse(
        success=result.success,
        provider=result.provider,
        error=result.error,
        response_time_ms=result.response_time_ms,
    )


@router.post("/voyageai/test", response_model=TestApiKeyResponse)
async def test_voyageai_key(
    request: TestApiKeyRequest,
    http_request: Request,
    _current_user: User = Depends(get_current_admin_user_hybrid),
) -> TestApiKeyResponse:
    """
    Test VoyageAI API key connectivity.

    Makes a test embedding API call to verify the key works.
    """
    # Validate format first
    validation = ApiKeyValidator.validate_voyageai_format(request.api_key)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)

    # Test connectivity
    tester = get_api_key_connectivity_tester()
    result = await tester.test_voyageai_connectivity(request.api_key)

    return TestApiKeyResponse(
        success=result.success,
        provider=result.provider,
        error=result.error,
        response_time_ms=result.response_time_ms,
    )


@router.post("/anthropic/test-configured", response_model=TestApiKeyResponse)
async def test_configured_anthropic_key(
    http_request: Request,
    _current_user: User = Depends(get_current_admin_user_hybrid),
) -> TestApiKeyResponse:
    """
    Test the currently configured Anthropic API key connectivity.

    Tests the key stored in server config without requiring a new key input.
    """
    config_service = get_config_service()
    config = config_service.load_config()

    api_key = config.claude_integration_config.anthropic_api_key
    if not api_key:
        return TestApiKeyResponse(
            success=False,
            provider="anthropic",
            error="No Anthropic API key configured",
        )

    # Test connectivity
    tester = get_api_key_connectivity_tester()
    result = await tester.test_anthropic_connectivity(api_key)

    return TestApiKeyResponse(
        success=result.success,
        provider=result.provider,
        error=result.error,
        response_time_ms=result.response_time_ms,
    )


@router.post("/voyageai/test-configured", response_model=TestApiKeyResponse)
async def test_configured_voyageai_key(
    http_request: Request,
    _current_user: User = Depends(get_current_admin_user_hybrid),
) -> TestApiKeyResponse:
    """
    Test the currently configured VoyageAI API key connectivity.

    Tests the key stored in server config without requiring a new key input.
    """
    config_service = get_config_service()
    config = config_service.load_config()

    api_key = config.claude_integration_config.voyageai_api_key
    if not api_key:
        return TestApiKeyResponse(
            success=False,
            provider="voyageai",
            error="No VoyageAI API key configured",
        )

    # Test connectivity
    tester = get_api_key_connectivity_tester()
    result = await tester.test_voyageai_connectivity(api_key)

    return TestApiKeyResponse(
        success=result.success,
        provider=result.provider,
        error=result.error,
        response_time_ms=result.response_time_ms,
    )


@router.get("/status", response_model=ApiKeysStatusResponse)
def get_api_keys_status(
    http_request: Request,
    _current_user: User = Depends(get_current_admin_user_hybrid),
) -> ApiKeysStatusResponse:
    """
    Get API key configuration status.

    Returns whether each provider has a key configured.
    """
    config_service = get_config_service()
    config = config_service.load_config()

    return ApiKeysStatusResponse(
        anthropic_configured=bool(
            config.claude_integration_config.anthropic_api_key
        ),
        voyageai_configured=bool(
            config.claude_integration_config.voyageai_api_key
        ),
    )


class ClearApiKeyResponse(BaseModel):
    """Response after clearing an API key."""

    success: bool
    provider: str
    message: str


@router.delete("/anthropic", response_model=ClearApiKeyResponse)
def clear_anthropic_key(
    http_request: Request,
    _current_user: User = Depends(get_current_admin_user_hybrid),
) -> ClearApiKeyResponse:
    """Clear the Anthropic API key from config and matching synced locations."""
    config_service = get_config_service()
    config = config_service.load_config()
    key_to_clear = config.claude_integration_config.anthropic_api_key

    if not key_to_clear:
        return ClearApiKeyResponse(
            success=True, provider="anthropic", message="No key was configured"
        )

    cleared = ["server config"]

    # Clear from config
    config.claude_integration_config.anthropic_api_key = ""
    config_service.config_manager.save_config(config)

    # Clear from environment only if it matches
    if os.environ.get("ANTHROPIC_API_KEY") == key_to_clear:
        del os.environ["ANTHROPIC_API_KEY"]
        cleared.append("environment")

    # Clear from ~/.claude.json only if it matches
    if loc := _clear_from_claude_config(key_to_clear):
        cleared.append(loc)

    # Clear from ~/.bashrc and ~/.profile only if matching
    cleared.extend(_clear_from_rc_files(key_to_clear, "ANTHROPIC_API_KEY"))

    logger.info(f"Cleared Anthropic API key from: {', '.join(cleared)}")
    return ClearApiKeyResponse(
        success=True, provider="anthropic", message=f"Cleared from: {', '.join(cleared)}"
    )


@router.delete("/voyageai", response_model=ClearApiKeyResponse)
def clear_voyageai_key(
    http_request: Request,
    _current_user: User = Depends(get_current_admin_user_hybrid),
) -> ClearApiKeyResponse:
    """Clear the VoyageAI API key from config and matching synced locations."""
    config_service = get_config_service()
    config = config_service.load_config()
    key_to_clear = config.claude_integration_config.voyageai_api_key

    if not key_to_clear:
        return ClearApiKeyResponse(
            success=True, provider="voyageai", message="No key was configured"
        )

    cleared = ["server config"]

    # Clear from config
    config.claude_integration_config.voyageai_api_key = ""
    config_service.config_manager.save_config(config)

    # Clear from environment only if it matches
    if os.environ.get("VOYAGE_API_KEY") == key_to_clear:
        del os.environ["VOYAGE_API_KEY"]
        cleared.append("environment")

    # Clear from ~/.bashrc and ~/.profile only if matching
    cleared.extend(_clear_from_rc_files(key_to_clear, "VOYAGE_API_KEY"))

    logger.info(f"Cleared VoyageAI API key from: {', '.join(cleared)}")
    return ClearApiKeyResponse(
        success=True, provider="voyageai", message=f"Cleared from: {', '.join(cleared)}"
    )
