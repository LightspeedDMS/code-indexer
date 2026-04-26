"""
Codex Credentials File Manager (Story #846).

Manages the $CODEX_HOME/auth.json file consumed by the Codex CLI for
OAuth / API-key authentication.  The file format is dictated by Codex CLI's
expected JSON structure.

Mirrors ClaudeCredentialsFileManager (Story #365) for structural consistency.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Default CODEX_HOME sub-path inside the CIDX server data directory.
# Resolved dynamically at runtime — never via Path.home() — to honour
# CIDX_SERVER_DATA_DIR (Bug #879 pattern, same variable used in lifespan.py).
_DEFAULT_CODEX_HOME_SUBDIR = "codex-home"
_AUTH_JSON_FILENAME = "auth.json"


def _default_auth_json_path() -> Path:
    """
    Derive the default auth.json path from CIDX_SERVER_DATA_DIR / codex-home.

    Honours the ``CIDX_SERVER_DATA_DIR`` environment variable so that the
    server and auto-updater processes resolve identical paths even when run
    as different OS users (Bug #879 pattern, same contract as lifespan.py).
    """
    server_data_dir = os.environ.get(
        "CIDX_SERVER_DATA_DIR", str(Path.home() / ".cidx-server")
    )
    return Path(server_data_dir) / _DEFAULT_CODEX_HOME_SUBDIR / _AUTH_JSON_FILENAME


def _provider_response_to_auth_json(response: dict) -> dict:
    """
    Transform an llm-creds-provider OpenAI vendor checkout response into the
    Codex auth.json schema.

    # SPIKE (Story #846): llm-creds-provider OpenAI vendor returns
    # {lease_id, credential_id, access_token, refresh_token, custom_fields: {}}
    # but Codex auth.json expects tokens.account_id and tokens.id_token.
    # Workaround: pull from custom_fields if the provider populates them
    # (vendor-side enhancement), otherwise default to empty string. Empty
    # account_id/id_token may cause Codex to require explicit OPENAI_API_KEY
    # fallback. This is acceptable per the user-confirmed degradation in #843.
    # Open follow-up: extend llm-creds-provider OpenAI vendor adapter to surface
    # account_id and id_token in custom_fields.

    Args:
        response: Raw dict from the checkout API response.

    Returns:
        A dict matching the Codex auth.json schema.

    Raises:
        ValueError: If a required field (access_token, refresh_token) is absent.
    """
    if "access_token" not in response:
        raise ValueError("Provider response missing required field: access_token")
    if "refresh_token" not in response:
        raise ValueError("Provider response missing required field: refresh_token")

    custom_fields: dict = response.get("custom_fields") or {}
    last_refresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    return {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": response["access_token"],
            "refresh_token": response["refresh_token"],
            # SPIKE fallback — see docstring above
            "account_id": custom_fields.get("account_id", ""),
            "id_token": custom_fields.get("id_token", ""),
        },
        "OPENAI_API_KEY": "",
        "last_refresh": last_refresh,
    }


class CodexCredentialsFileManager:
    """
    Manages the Codex CLI auth.json file ($CODEX_HOME/auth.json).

    The file is written atomically (tempfile + os.replace) with ``0o600``
    permissions, matching the Codex auth.json schema:

    .. code-block:: json

        {
            "auth_mode": "chatgpt",
            "tokens": {
                "access_token": "...",
                "refresh_token": "...",
                "account_id": "...",
                "id_token": "..."
            },
            "OPENAI_API_KEY": "...",
            "last_refresh": "ISO8601"
        }
    """

    def __init__(self, auth_json_path: Optional[Path] = None) -> None:
        """
        Initialise the manager.

        Args:
            auth_json_path: Override the default auth.json path.
                            Useful for testing with ``tmp_path``.
        """
        if auth_json_path is not None:
            self.auth_json_path = Path(auth_json_path)
        else:
            self.auth_json_path = _default_auth_json_path()

    def write_credentials(
        self,
        auth_mode: str,
        access_token: str,
        refresh_token: str,
        account_id: str,
        id_token: str,
        openai_api_key: str,
    ) -> None:
        """
        Write auth.json atomically with strict 0o600 permissions.

        Creates the parent directory if it does not exist.

        Args:
            auth_mode: Codex auth mode (e.g. ``"chatgpt"``).
            access_token: OAuth access token.
            refresh_token: OAuth refresh token.
            account_id: OpenAI account ID (may be empty for SPIKE fallback).
            id_token: OpenAI ID token (may be empty for SPIKE fallback).
            openai_api_key: Direct OPENAI_API_KEY value (empty in OAuth mode).

        Raises:
            ValueError: If ``auth_mode``, ``access_token``, or ``refresh_token``
                        are empty strings.
        """
        if not auth_mode:
            raise ValueError("auth_mode must not be empty")
        if not access_token:
            raise ValueError("access_token must not be empty")
        if not refresh_token:
            raise ValueError("refresh_token must not be empty")

        last_refresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

        payload = {
            "auth_mode": auth_mode,
            "tokens": {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "account_id": account_id,
                "id_token": id_token,
            },
            "OPENAI_API_KEY": openai_api_key,
            "last_refresh": last_refresh,
        }

        self.auth_json_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write: write to a sibling temp file, then os.replace.
        # This prevents partial-write corruption if the process is interrupted.
        fd, tmp_path = tempfile.mkstemp(
            dir=self.auth_json_path.parent,
            prefix=".auth_json_tmp_",
            suffix=".json",
        )
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, indent=2)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, str(self.auth_json_path))
        except Exception:
            # Clean up temp file on failure to avoid orphaned files.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        logger.debug("Wrote Codex credentials to %s", self.auth_json_path)

    def read_credentials(self) -> Optional[dict]:
        """
        Read the current auth.json content.

        Returns:
            The parsed dict, or ``None`` if the file does not exist or
            contains invalid JSON.

        Note:
            ``FileNotFoundError`` (missing file, including races between
            ``exists()`` and ``open()``) and ``json.JSONDecodeError`` (corrupt
            content) are silently downgraded to ``None``.  Other I/O failures
            (permissions, directory errors, etc.) propagate as-is.
        """
        if not self.auth_json_path.exists():
            return None

        try:
            with open(self.auth_json_path, "r") as fh:
                loaded: Dict[Any, Any] = json.load(fh)
                return loaded
        except FileNotFoundError:
            # Race: file removed between exists() and open()
            logger.debug(
                "Codex auth.json disappeared before read at %s", self.auth_json_path
            )
            return None
        except json.JSONDecodeError as exc:
            logger.warning(
                "Corrupt Codex auth.json at %s: %s",
                self.auth_json_path,
                exc,
            )
            return None

    def delete_credentials(self) -> None:
        """
        Delete auth.json if it exists.

        Idempotent — does nothing if the file is already absent or is removed
        concurrently before the unlink completes.
        """
        try:
            self.auth_json_path.unlink()
            logger.debug("Deleted Codex auth.json at %s", self.auth_json_path)
        except FileNotFoundError:
            # Already absent — idempotent, no action needed.
            pass

    def exists(self) -> bool:
        """Return True if the auth.json file currently exists."""
        return self.auth_json_path.exists()
