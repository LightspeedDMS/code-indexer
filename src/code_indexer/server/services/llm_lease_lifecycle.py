"""
LLM Lease Lifecycle Service (Story #366).

Orchestrates the full credential lease lifecycle:
- Startup: crash recovery + fresh checkout
- Shutdown: checkin with token writeback + file cleanup
- Config change: enter/exit subscription mode
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from enum import Enum
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
    ) -> None:
        self._client = client
        self._state_mgr = state_manager
        self._creds_mgr = credentials_manager
        self._lock = threading.Lock()
        self._status = LeaseStatusInfo(status=LeaseLifecycleStatus.INACTIVE)

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
                self._do_checkin_with_writeback(residual.lease_id, residual.credential_id)
                self._state_mgr.clear_state()

            # Step 2: Remove ANTHROPIC_API_KEY from env to prevent interference
            os.environ.pop("ANTHROPIC_API_KEY", None)

            # Step 3: Fresh checkout
            try:
                response = self._client.checkout(vendor="anthropic", consumer_id=consumer_id)

                # Step 4: Write credentials file and state
                self._creds_mgr.write_credentials(
                    access_token=response.access_token or "",
                    refresh_token=response.refresh_token or "",
                )
                self._state_mgr.save_state(
                    LlmLeaseState(
                        lease_id=response.lease_id,
                        credential_id=response.credential_id,
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
            if self._status.status not in (LeaseLifecycleStatus.ACTIVE,):
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
                self._do_checkin_with_writeback(lease_id, credential_id)

            # Cleanup files
            self._creds_mgr.delete_credentials()
            self._state_mgr.clear_state()

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
