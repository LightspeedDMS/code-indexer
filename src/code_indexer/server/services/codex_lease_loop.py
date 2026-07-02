"""
Codex Lease Loop (Story #846).

Provides vendor-scoped credential lease management for the Codex CLI.
Uses vendor="openai" with llm-creds-provider and a dedicated state file
(codex_lease_state.json) that does not collide with Claude's state file.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from code_indexer.server.config.llm_lease_state import (
    LlmLeaseState,
    LlmLeaseStateManager,
)
from code_indexer.server.services.codex_credentials_file_manager import (
    CodexCredentialsFileManager,
    _provider_response_to_auth_json,
)
from code_indexer.server.services.llm_creds_client import (
    CheckoutResponse,
    LlmCredsClient,
)

logger = logging.getLogger(__name__)

# Vendor-scoped state filename — distinct from Claude's llm_lease_state.json.
_CODEX_STATE_FILENAME = "codex_lease_state.json"


class CodexLeaseLoop:
    """
    Manages the Codex CLI credential lease lifecycle.

    Scopes its lease state file to codex_lease_state.json so Claude and
    Codex instances running in the same server data directory do not share
    state. Provides a thread-safe checkin helper and exposes the state file
    path for inspection and testing.
    """

    def __init__(
        self,
        client: LlmCredsClient,
        state_manager: LlmLeaseStateManager,
        credentials_manager: CodexCredentialsFileManager,
    ) -> None:
        if client is None:
            raise ValueError("client must not be None")
        if state_manager is None:
            raise ValueError("state_manager must not be None")
        if credentials_manager is None:
            raise ValueError("credentials_manager must not be None")

        self._client = client
        self._state_mgr = state_manager
        self._creds_mgr = credentials_manager
        self._lock = threading.Lock()
        self._active_lease_id: Optional[str] = None
        self._active_credential_id: Optional[str] = None
        # State filename isolation is achieved by passing state_filename to
        # LlmLeaseStateManager at construction time (CRIT-3 fix, Story #846).
        # Post-construction mutation of _state_file has been removed.

    @property
    def state_file_path(self) -> Path:
        """Return the path to the codex-scoped lease state file."""
        return Path(self._state_mgr._state_file)

    def _do_checkin(self, lease_id: str, credential_id: Optional[str]) -> bool:
        """
        Check in a lease to the provider.

        Args:
            lease_id: Non-empty lease identifier returned by checkout.
            credential_id: Optional credential identifier for writeback.

        Returns:
            True if checkin succeeded, False on any failure.

        Raises:
            ValueError: If lease_id is empty or None.

        Failures are logged as warnings — callers decide whether to preserve
        or clear state based on the return value.
        """
        if not lease_id:
            raise ValueError("lease_id must not be empty")

        try:
            self._client.checkin(lease_id=lease_id, credential_id=credential_id)
            logger.info("Codex checkin successful: lease_id=%s", lease_id)
            return True
        except Exception as exc:
            logger.warning(
                "Codex checkin failed for lease_id=%s (non-fatal): %s", lease_id, exc
            )
            return False

    def _recover_residual_state(self) -> None:
        """Check in any residual lease from a prior crash. Clears state only on success."""
        residual = self._state_mgr.load_state()
        if not residual:
            return
        logger.info(
            "Codex: found residual lease state: lease_id=%s — crash recovery",
            residual.lease_id,
        )
        checkin_ok = self._do_checkin(residual.lease_id, residual.credential_id)
        if checkin_ok:
            self._state_mgr.clear_state()
        else:
            logger.warning(
                "Codex: crash-recovery checkin failed for lease_id=%s — "
                "state file preserved for retry on next startup",
                residual.lease_id,
            )

    def _write_auth_and_persist(self, response: CheckoutResponse) -> None:
        """Transform the checkout response, write auth.json, and persist lease state."""
        if response is None:
            raise ValueError("response must not be None")
        auth_data = _provider_response_to_auth_json(
            {
                "access_token": response.access_token or "",
                "refresh_token": response.refresh_token or "",
                "custom_fields": {},
            }
        )
        self._creds_mgr.write_credentials(
            auth_mode=auth_data["auth_mode"],
            access_token=auth_data["tokens"]["access_token"],
            refresh_token=auth_data["tokens"]["refresh_token"],
            account_id=auth_data["tokens"]["account_id"],
            id_token=auth_data["tokens"]["id_token"],
            openai_api_key=auth_data["OPENAI_API_KEY"],
        )
        self._state_mgr.save_state(
            LlmLeaseState(
                lease_id=response.lease_id,
                credential_id=response.credential_id,
                credential_type="oauth",
            )
        )

    def _compensate(self, lease_id: str, credential_id: Optional[str]) -> None:
        """Return lease and unconditionally delete auth.json after post-checkout failure."""
        _ = self._do_checkin(lease_id, credential_id)  # best-effort; warns on failure
        self._creds_mgr.delete_credentials()  # idempotent if never written

    def start(self, consumer_id: str = "cidx-server") -> bool:
        """Acquire a lease (vendor='openai'). Returns True on success, False on failure."""
        with self._lock:
            self._recover_residual_state()
            lease_id: Optional[str] = None
            cred_id: Optional[str] = None
            try:
                resp = self._client.checkout(vendor="openai", consumer_id=consumer_id)
                lease_id, cred_id = resp.lease_id, resp.credential_id
                self._write_auth_and_persist(resp)
                self._active_lease_id, self._active_credential_id = lease_id, cred_id
                logger.info(
                    "Codex credential checkout successful: lease_id=%s", lease_id
                )
                return True
            except Exception as exc:
                logger.warning(
                    "Codex lease acquisition failed (job will be skipped): %s", exc
                )
                if lease_id is not None:
                    self._compensate(lease_id, cred_id)
                self._active_lease_id = self._active_credential_id = None
                return False

    def stop(self) -> None:
        """Return the active lease and clean up auth.json. No-op if no lease is active."""
        with self._lock:
            if self._active_lease_id:
                _ = self._do_checkin(self._active_lease_id, self._active_credential_id)
                # best-effort: _do_checkin already logs WARNING on failure
            self._creds_mgr.delete_credentials()
            self._state_mgr.clear_state()
            self._active_lease_id = self._active_credential_id = None
            logger.info("Codex lease lifecycle stopped, auth.json cleaned up")
