"""
LLM Lease Lifecycle Service (Story #366).

Orchestrates the full credential lease lifecycle:
- Startup: crash recovery + fresh checkout
- Shutdown: checkin with token writeback + file cleanup
- Config change: enter/exit subscription mode
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from code_indexer.server.config.llm_lease_state import LlmLeaseState, LlmLeaseStateManager
from code_indexer.server.services.claude_credentials_file_manager import (
    ClaudeCredentialsFileManager,
)
from code_indexer.server.services.llm_creds_client import LlmCredsClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status types
# ---------------------------------------------------------------------------


class LeaseLifecycleStatus(str, Enum):
    INACTIVE = "inactive"          # Subscription mode disabled or not started
    ACTIVE = "active"              # Credential checked out and files written
    DEGRADED = "degraded"          # Provider unreachable, no credential available
    SHUTTING_DOWN = "shutting_down"  # Shutdown in progress


@dataclass
class LeaseStatusInfo:
    """Current status snapshot returned by get_status()."""

    status: LeaseLifecycleStatus
    lease_id: Optional[str] = None
    credential_id: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class LlmLeaseLifecycleService:
    """
    Orchestrates the LLM credential lease lifecycle.

    Thread-safe: all state mutations are protected by a single lock.
    Non-blocking: provider failures during start() result in DEGRADED state,
    never raise exceptions to callers.
    """

    def __init__(
        self,
        client: LlmCredsClient,
        state_manager: LlmLeaseStateManager,
        credentials_manager: ClaudeCredentialsFileManager,
        claude_json_path: Optional[Path] = None,
    ) -> None:
        self._client = client
        self._state_mgr = state_manager
        self._creds_mgr = credentials_manager
        self._claude_json_path = claude_json_path or (Path.home() / ".claude.json")
        self._lock = threading.Lock()
        self._status = LeaseStatusInfo(status=LeaseLifecycleStatus.INACTIVE)
        self._credential_type: Optional[str] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, consumer_id: str = "cidx-server") -> None:
        """
        Called on startup when subscription mode is enabled.

        Steps:
        1. Crash recovery: if residual state exists, checkin old lease with
           token writeback and clear state.
        2. Remove ANTHROPIC_API_KEY from os.environ to prevent interference.
        3. Fresh checkout from provider.
        4. Write .credentials.json and encrypted state file.

        Non-blocking: if the provider is unreachable, enters DEGRADED state
        and returns without raising.
        """
        with self._lock:
            # Step 1: Crash recovery — clean up any residual state
            residual = self._state_mgr.load_state()
            if residual:
                logger.info(
                    "Found residual lease state on startup: lease_id=%s — performing crash recovery",
                    residual.lease_id,
                )
                if getattr(residual, "credential_type", "oauth") == "api_key":
                    self._do_plain_checkin(residual.lease_id, residual.credential_id)
                    self._clear_claude_api_key()
                else:
                    self._do_checkin_with_writeback(residual.lease_id, residual.credential_id)
                self._state_mgr.clear_state()

            # Step 2: Remove ANTHROPIC_API_KEY from env to prevent interference
            os.environ.pop("ANTHROPIC_API_KEY", None)

            # Step 3: Fresh checkout
            try:
                response = self._client.checkout(vendor="anthropic", consumer_id=consumer_id)

                # Step 4: Store credentials based on type returned by provider
                if response.api_key:
                    self._credential_type = "api_key"
                    os.environ["ANTHROPIC_API_KEY"] = response.api_key
                    self._write_claude_api_key(response.api_key)
                else:
                    self._credential_type = "oauth"
                    self._creds_mgr.write_credentials(
                        access_token=response.access_token or "",
                        refresh_token=response.refresh_token or "",
                    )
                self._state_mgr.save_state(
                    LlmLeaseState(
                        lease_id=response.lease_id,
                        credential_id=response.credential_id,
                        credential_type=self._credential_type or "oauth",
                    )
                )

                self._status = LeaseStatusInfo(
                    status=LeaseLifecycleStatus.ACTIVE,
                    lease_id=response.lease_id,
                    credential_id=response.credential_id,
                )
                logger.info(
                    "Credential checkout successful: lease_id=%s",
                    response.lease_id,
                )

            except Exception as exc:
                # Non-blocking: server continues in degraded state
                logger.error(
                    "Failed to checkout credential from LLM provider: %s", exc
                )
                self._credential_type = None
                self._status = LeaseStatusInfo(
                    status=LeaseLifecycleStatus.DEGRADED,
                    error=str(exc),
                )

    def stop(self) -> None:
        """
        Called on graceful shutdown.

        Steps:
        1. Read current tokens from .credentials.json (for writeback).
        2. Checkin with token writeback.
        3. Delete .credentials.json.
        4. Clear state file.
        5. Set status to INACTIVE.

        No-op if status is already INACTIVE or DEGRADED.
        """
        with self._lock:
            if self._status.status != LeaseLifecycleStatus.ACTIVE:
                self._status = LeaseStatusInfo(status=LeaseLifecycleStatus.INACTIVE)
                return

            lease_id = self._status.lease_id
            credential_id = self._status.credential_id

            self._status = LeaseStatusInfo(
                status=LeaseLifecycleStatus.SHUTTING_DOWN,
                lease_id=lease_id,
                credential_id=credential_id,
            )

            if lease_id:
                if self._credential_type == "api_key":
                    # Plain checkin — no token writeback for api_key credentials
                    self._do_plain_checkin(lease_id, credential_id)
                    os.environ.pop("ANTHROPIC_API_KEY", None)
                    self._clear_claude_api_key()
                else:
                    # OAuth: read current tokens from file and write them back
                    self._do_checkin_with_writeback(lease_id, credential_id)
                    self._creds_mgr.delete_credentials()
                    self._clear_claude_api_key()

            self._state_mgr.clear_state()
            self._credential_type = None

            self._status = LeaseStatusInfo(status=LeaseLifecycleStatus.INACTIVE)
            logger.info("Lease lifecycle stopped, credentials cleaned up")

    def on_mode_enter_subscription(self, consumer_id: str = "cidx-server") -> None:
        """
        Called when config changes TO subscription mode.

        Performs the same sequence as start().
        """
        self.start(consumer_id=consumer_id)

    def on_mode_exit_subscription(self) -> None:
        """
        Called when config changes FROM subscription mode.

        Performs the same cleanup as stop().
        """
        self.stop()

    def get_status(self) -> LeaseStatusInfo:
        """Return a snapshot of the current lifecycle status (thread-safe read)."""
        with self._lock:
            return self._status

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _do_plain_checkin(
        self, lease_id: str, credential_id: Optional[str]
    ) -> None:
        """
        Checkin without token writeback — used for api_key credential type.

        Failures are logged as warnings and swallowed — checkin failure must
        not prevent the rest of shutdown from completing.
        """
        try:
            self._client.checkin(
                lease_id=lease_id,
                credential_id=credential_id,
            )
            logger.info("Plain checkin successful: lease_id=%s", lease_id)
        except Exception as exc:
            logger.warning(
                "Checkin failed for lease_id=%s (non-fatal): %s", lease_id, exc
            )

    def _do_checkin_with_writeback(
        self, lease_id: str, credential_id: Optional[str]
    ) -> None:
        """
        Read current tokens from .credentials.json and checkin with writeback.

        Failures are logged as warnings and swallowed — checkin failure must
        not prevent the rest of shutdown/recovery from completing.
        """
        try:
            tokens = self._creds_mgr.read_credentials()
            access_token = tokens.get("access_token") if tokens else None
            refresh_token = tokens.get("refresh_token") if tokens else None

            self._client.checkin(
                lease_id=lease_id,
                credential_id=credential_id,
                access_token=access_token,
                refresh_token=refresh_token,
            )
            logger.info(
                "Checkin with writeback successful: lease_id=%s", lease_id
            )
        except Exception as exc:
            logger.warning(
                "Checkin failed for lease_id=%s (non-fatal): %s", lease_id, exc
            )

    def _write_claude_api_key(self, api_key: str) -> None:
        """Write apiKey to ~/.claude.json, preserving existing fields."""
        config: dict = {}
        if self._claude_json_path.exists():
            try:
                config = json.loads(self._claude_json_path.read_text())
            except json.JSONDecodeError:
                logger.warning(
                    "Invalid JSON in %s, overwriting with fresh config",
                    self._claude_json_path,
                )
                config = {}
        config["apiKey"] = api_key
        self._claude_json_path.parent.mkdir(parents=True, exist_ok=True)
        self._claude_json_path.write_text(json.dumps(config, indent=2))
        os.chmod(self._claude_json_path, 0o600)

    def _clear_claude_api_key(self) -> None:
        """Remove apiKey from ~/.claude.json, preserving other fields.

        If the file does not exist or has no apiKey, this is a no-op.
        If apiKey was the only field, the file is left as an empty object {}.
        """
        if not self._claude_json_path.exists():
            return
        try:
            config = json.loads(self._claude_json_path.read_text())
        except json.JSONDecodeError:
            logger.warning(
                "Invalid JSON in %s when clearing apiKey — skipping",
                self._claude_json_path,
            )
            return
        if "apiKey" not in config:
            return
        del config["apiKey"]
        self._claude_json_path.write_text(json.dumps(config, indent=2))
