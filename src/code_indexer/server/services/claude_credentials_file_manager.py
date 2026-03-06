"""
Claude Credentials File Manager (Story #365).

Manages the ~/.claude/.credentials.json file consumed by Claude CLI for
OAuth authentication.  The file format is dictated by Claude CLI's
expected ``claudeAiOauth`` JSON structure.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Hardcoded values required by Claude CLI (as specified in Story #365)
_SCOPES = [
    "user:inference",
    "user:mcp_servers",
    "user:profile",
    "user:sessions:claude_code",
]
_SUBSCRIPTION_TYPE = "enterprise"
_RATE_LIMIT_TIER = "default_claude_max_5x"

# Default expires-at offset: 1 hour in milliseconds
_DEFAULT_EXPIRES_OFFSET_MS = 3_600_000


class ClaudeCredentialsFileManager:
    """
    Manages the Claude CLI credentials file (``~/.claude/.credentials.json``).

    The file is written with ``0o600`` permissions and uses the
    ``claudeAiOauth`` JSON structure expected by the Claude CLI.
    """

    #: Default path used when no override is supplied.
    DEFAULT_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"

    def __init__(
        self,
        credentials_path: Optional[Path] = None,
    ) -> None:
        """
        Initialise the manager.

        Args:
            credentials_path: Override the default credentials file path.
                               Useful for testing with ``tmp_path``.
        """
        if credentials_path is not None:
            self.credentials_path = Path(credentials_path)
        else:
            self.credentials_path = self.DEFAULT_CREDENTIALS_PATH

    def write_credentials(
        self,
        access_token: str,
        refresh_token: str,
        expires_at: Optional[int] = None,
    ) -> None:
        """
        Write the credentials file in the format expected by Claude CLI.

        Creates the parent directory if it does not exist.  Sets ``0o600``
        permissions on the file after writing.

        Args:
            access_token: The OAuth access token (e.g. ``sk-ant-oat01-...``).
            refresh_token: The OAuth refresh token (e.g. ``sk-ant-ort01-...``).
            expires_at: Expiry timestamp in milliseconds since epoch.
                        Defaults to ``now + 1 hour`` if omitted.
        """
        if expires_at is None:
            expires_at = int(time.time() * 1000) + _DEFAULT_EXPIRES_OFFSET_MS

        payload = {
            "claudeAiOauth": {
                "accessToken": access_token,
                "refreshToken": refresh_token,
                "expiresAt": expires_at,
                "scopes": _SCOPES,
                "subscriptionType": _SUBSCRIPTION_TYPE,
                "rateLimitTier": _RATE_LIMIT_TIER,
            }
        }

        self.credentials_path.parent.mkdir(parents=True, exist_ok=True)

        with open(self.credentials_path, "w") as fh:
            json.dump(payload, fh, indent=2)

        os.chmod(self.credentials_path, 0o600)
        logger.debug("Wrote Claude credentials to %s", self.credentials_path)

    def read_credentials(self) -> Optional[dict]:
        """
        Read the current tokens from the credentials file.

        Useful for token writeback after Claude CLI may have refreshed them.

        Returns:
            A dict with ``access_token`` and ``refresh_token`` keys, or
            ``None`` if the file does not exist or lacks the expected structure.
        """
        if not self.credentials_path.exists():
            return None

        with open(self.credentials_path, "r") as fh:
            data = json.load(fh)

        oauth = data.get("claudeAiOauth")
        if not oauth:
            return None

        access_token = oauth.get("accessToken")
        refresh_token = oauth.get("refreshToken")
        if access_token is None and refresh_token is None:
            return None

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
        }

    def delete_credentials(self) -> None:
        """
        Delete the credentials file if it exists.

        Does nothing if the file is already absent — no error is raised.
        """
        if self.credentials_path.exists():
            self.credentials_path.unlink()
            logger.debug("Deleted Claude credentials at %s", self.credentials_path)
