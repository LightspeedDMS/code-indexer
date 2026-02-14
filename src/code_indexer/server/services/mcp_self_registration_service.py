"""
MCP Self-Registration Service for Story #203.

Automatically registers CIDX server as an MCP server in Claude Code configuration
before launching Claude CLI exploration jobs, enabling dependency map analysis to
leverage CIDX's semantic search and code intelligence tools.
"""

import base64
import logging
import subprocess
import threading
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class MCPSelfRegistrationService:
    """
    Service for auto-registering CIDX server as MCP server in Claude Code.

    Provides idempotent registration that checks for existing registration,
    manages credentials persistently, and gracefully handles CLI unavailability.
    """

    def __init__(self, config_manager, mcp_credential_manager):
        """
        Initialize MCP self-registration service.

        Args:
            config_manager: ServerConfigManager for persistent config storage
            mcp_credential_manager: MCPCredentialManager for credential generation
        """
        self._config_manager = config_manager
        self._mcp_credential_manager = mcp_credential_manager
        self._registration_checked = False
        self._registration_lock = threading.Lock()  # Story #203 Finding 5: Thread safety

    def ensure_registered(self) -> bool:
        """
        Ensure CIDX is registered as MCP server in Claude Code (AC1, AC2, AC5).

        Idempotent method that:
        - Returns immediately if already checked this process (fast-path)
        - Checks if Claude CLI is available
        - Checks if already registered
        - Registers if not already registered
        - Sets flag on success

        Returns:
            True if registered successfully or already registered
            False if Claude CLI unavailable or registration failed
        """
        # Fast-path: already checked this process (Story #203 Finding 5: outside lock)
        if self._registration_checked:
            return True

        # Story #203 Finding 5: Double-check locking for thread safety
        with self._registration_lock:
            # Double-check inside lock
            if self._registration_checked:
                return True

            # Check CLI availability
            if not self.claude_cli_available():
                logger.warning("Claude CLI not available - skipping MCP self-registration")
                # Story #203 Finding 4: Do NOT set flag on failure - allow retry if CLI becomes available
                return False

            # Check if already registered
            if self.is_already_registered():
                logger.info("CIDX already registered as MCP server in Claude Code")
                self._registration_checked = True
                return True

            # Get or create credentials
            creds = self.get_or_create_credentials()
            if creds is None:
                logger.error("Failed to get or create MCP credentials")
                return False

            # Register in Claude Code
            success = self.register_in_claude_code(creds)

            if success:
                self._registration_checked = True
                logger.info("Successfully registered CIDX as MCP server in Claude Code")
            else:
                logger.warning("Failed to register CIDX as MCP server in Claude Code")

            return success

    def claude_cli_available(self) -> bool:
        """
        Check if Claude CLI is installed (AC3, AC4).

        Returns:
            True if Claude CLI is available, False otherwise
        """
        try:
            result = subprocess.run(
                ["claude", "--version"],
                capture_output=True,
                timeout=10,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def is_already_registered(self) -> bool:
        """
        Check if CIDX is already registered as MCP server (AC2).

        Returns:
            True if cidx-local MCP server exists, False otherwise
        """
        try:
            result = subprocess.run(
                ["claude", "mcp", "get", "cidx-local"],
                capture_output=True,
                timeout=10,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def get_or_create_credentials(self) -> Optional[Dict[str, str]]:
        """
        Get or create MCP credentials for self-registration (AC2, AC5).

        Checks if credentials are already stored in config.
        If stored, validates they still exist in credential manager.
        If not stored or invalidated, creates new credentials.

        Returns:
            Dict with client_id and client_secret, or None on error
        """
        # Load config
        config = self._config_manager.load_config()
        if config is None:
            logger.error("Failed to load server config")
            return None

        # Check for stored credentials (Story #203 Finding 1: use proper dataclass field)
        stored_config = config.mcp_self_registration
        assert stored_config is not None  # Guaranteed by ServerConfig.__post_init__

        if stored_config.client_id:
            # Validate stored credential still exists
            result = self._mcp_credential_manager.get_credential_by_client_id(
                stored_config.client_id
            )
            if result:
                logger.debug("Reusing stored MCP self-registration credentials")
                return {
                    "client_id": stored_config.client_id,
                    "client_secret": stored_config.client_secret,
                }
            else:
                logger.info("Stored MCP credential no longer valid, creating new one")

        # Generate new credential
        try:
            cred = self._mcp_credential_manager.generate_credential(
                user_id="admin", name="cidx-local-auto"
            )

            # Store in config (Story #203 Finding 1: update dataclass fields)
            stored_config.client_id = cred["client_id"]
            stored_config.client_secret = cred["client_secret"]
            self._config_manager.save_config(config)

            logger.info("Generated new MCP self-registration credentials")
            return {
                "client_id": cred["client_id"],
                "client_secret": cred["client_secret"],
            }

        except Exception as e:
            logger.error(f"Failed to generate MCP credentials: {e}")
            return None

    def register_in_claude_code(self, creds: Dict[str, str]) -> bool:
        """
        Register CIDX as MCP server in Claude Code config (AC1).

        Args:
            creds: Dict with client_id and client_secret

        Returns:
            True if registration succeeded, False otherwise
        """
        # Load config for port
        config = self._config_manager.load_config()
        if config is None:
            logger.error("Failed to load server config")
            return False

        port = config.port

        # Build Basic auth header
        auth_string = f"{creds['client_id']}:{creds['client_secret']}"
        auth_bytes = auth_string.encode("ascii")
        auth_b64 = base64.b64encode(auth_bytes).decode("ascii")
        auth_header = f"Authorization: Basic {auth_b64}"

        # Build command
        cmd = [
            "claude",
            "mcp",
            "add",
            "--transport",
            "http",
            "--header",
            auth_header,
            "--scope",
            "user",
            "cidx-local",
            f"http://localhost:{port}/mcp",
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=30,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.error(f"Failed to register MCP server: {e}")
            return False
