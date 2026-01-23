"""
API Key Management Service for CIDX Server.

Provides:
- ApiKeyValidator: Format validation for Anthropic and VoyageAI API keys
- ApiKeySyncService: Thread-safe synchronization of API keys to config files
- ApiKeyConnectivityTester: Async connectivity testing for API keys

Story #20: API Key Management for Claude CLI and VoyageAI
"""

import asyncio
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class ConnectivityTestResult:
    """Result of API key connectivity test.

    Attributes:
        success: Whether the connectivity test succeeded
        provider: The provider tested ('anthropic' or 'voyageai')
        error: Error message if test failed, None if successful
        response_time_ms: Response time in milliseconds, None if failed
    """

    success: bool
    provider: str
    error: Optional[str] = None
    response_time_ms: Optional[int] = None


@dataclass
class SyncResult:
    """Result of API key synchronization operation.

    Attributes:
        success: Whether the sync operation succeeded
        already_synced: True if key was already synced (idempotent no-op)
        error: Error message if sync failed, None if successful
    """

    success: bool
    already_synced: bool = False
    error: Optional[str] = None


class ApiKeySyncService:
    """
    Thread-safe service for synchronizing API keys to various targets.

    Syncs:
    - Anthropic key: ~/.claude.json, os.environ, systemd env file
    - VoyageAI key: os.environ, systemd env file

    All operations are idempotent - calling with same key is a no-op.
    """

    def __init__(
        self,
        claude_config_path: Optional[str] = None,
        systemd_env_path: Optional[str] = None,
        claude_credentials_path: Optional[str] = None,
    ):
        """
        Initialize ApiKeySyncService.

        Args:
            claude_config_path: Path to ~/.claude.json (default: ~/.claude.json)
            systemd_env_path: Path to systemd env file (default: /etc/cidx-server/env)
            claude_credentials_path: Path to credentials file to remove
        """
        self._sync_lock = threading.Lock()
        self._claude_config_path = Path(
            claude_config_path or Path.home() / ".claude.json"
        )
        self._systemd_env_path = Path(
            systemd_env_path or "/etc/cidx-server/env"
        )
        self._claude_credentials_path = (
            Path(claude_credentials_path)
            if claude_credentials_path
            else Path.home() / ".claude" / ".credentials.json"
        )

    def sync_anthropic_key(self, api_key: str) -> SyncResult:
        """
        Sync Anthropic API key to all targets.

        Targets:
        1. ~/.claude.json (apiKey field)
        2. os.environ["ANTHROPIC_API_KEY"]
        3. systemd environment file

        Args:
            api_key: The Anthropic API key to sync

        Returns:
            SyncResult indicating success/failure and idempotency status
        """
        with self._sync_lock:
            # Check if already synced (idempotent)
            if self._is_anthropic_key_synced(api_key):
                return SyncResult(success=True, already_synced=True)

            try:
                # Step 1: Write to ~/.claude.json
                self._write_claude_json(api_key)

                # Step 2: Set os.environ
                os.environ["ANTHROPIC_API_KEY"] = api_key

                # Step 3: Write to systemd env file
                self._update_systemd_env_file("ANTHROPIC_API_KEY", api_key)

                # Step 4: Remove legacy credentials file if it exists
                if self._claude_credentials_path.exists():
                    try:
                        self._claude_credentials_path.unlink()
                        logger.info(
                            f"Removed legacy credentials file: "
                            f"{self._claude_credentials_path}"
                        )
                    except OSError as e:
                        logger.warning(
                            f"Failed to remove credentials file: {e}"
                        )

                # Step 5: Update ~/.bashrc (for shell persistence)
                self._update_bashrc("ANTHROPIC_API_KEY", api_key)

                return SyncResult(success=True)

            except Exception as e:
                logger.error(f"Failed to sync Anthropic API key: {e}")
                return SyncResult(success=False, error=str(e))

    def sync_voyageai_key(self, api_key: str) -> SyncResult:
        """
        Sync VoyageAI API key to all targets.

        Targets:
        1. os.environ["VOYAGE_API_KEY"]
        2. systemd environment file

        Args:
            api_key: The VoyageAI API key to sync

        Returns:
            SyncResult indicating success/failure and idempotency status
        """
        with self._sync_lock:
            # Check if already synced (idempotent)
            if self._is_voyageai_key_synced(api_key):
                return SyncResult(success=True, already_synced=True)

            try:
                # Step 1: Set os.environ (immediate hot-reload)
                os.environ["VOYAGE_API_KEY"] = api_key

                # Step 2: Write to systemd env file
                self._update_systemd_env_file("VOYAGE_API_KEY", api_key)

                # Step 3: Update ~/.bashrc (for shell persistence)
                self._update_bashrc("VOYAGE_API_KEY", api_key)

                return SyncResult(success=True)

            except Exception as e:
                logger.error(f"Failed to sync VoyageAI API key: {e}")
                return SyncResult(success=False, error=str(e))

    def _is_voyageai_key_synced(self, api_key: str) -> bool:
        """Check if VoyageAI key is already synced to os.environ."""
        return os.environ.get("VOYAGE_API_KEY") == api_key

    def _is_anthropic_key_synced(self, api_key: str) -> bool:
        """Check if Anthropic key is already synced to all targets."""
        # Check os.environ
        if os.environ.get("ANTHROPIC_API_KEY") != api_key:
            return False

        # Check ~/.claude.json
        if self._claude_config_path.exists():
            try:
                config = json.loads(self._claude_config_path.read_text())
                if config.get("apiKey") != api_key:
                    return False
            except (json.JSONDecodeError, IOError):
                return False
        else:
            return False

        return True

    def _write_claude_json(self, api_key: str) -> None:
        """Write API key to ~/.claude.json, preserving existing fields."""
        config = {}

        # Read existing config if present
        if self._claude_config_path.exists():
            try:
                config = json.loads(self._claude_config_path.read_text())
            except json.JSONDecodeError:
                logger.warning(
                    f"Invalid JSON in {self._claude_config_path}, overwriting"
                )
                config = {}

        # Update apiKey field
        config["apiKey"] = api_key

        # Write back
        self._claude_config_path.parent.mkdir(parents=True, exist_ok=True)
        self._claude_config_path.write_text(json.dumps(config, indent=2))

    def _update_systemd_env_file(self, key: str, value: str) -> None:
        """Update or add a key in the systemd environment file."""
        try:
            lines = []

            # Read existing file if present
            if self._systemd_env_path.exists():
                lines = self._systemd_env_path.read_text().strip().split("\n")
                if lines == [""]:
                    lines = []

            # Update or add the key
            updated = False
            for i, line in enumerate(lines):
                if line.startswith(f"{key}="):
                    lines[i] = f"{key}={value}"
                    updated = True
                    break

            if not updated:
                lines.append(f"{key}={value}")

            # Write atomically
            self._systemd_env_path.parent.mkdir(parents=True, exist_ok=True)
            self._systemd_env_path.write_text("\n".join(lines) + "\n")

        except PermissionError:
            logger.warning(
                f"Cannot write to systemd env file {self._systemd_env_path}: "
                "permission denied"
            )
        except Exception as e:
            logger.warning(f"Failed to update systemd env file: {e}")

    def _update_bashrc(self, env_var_name: str, value: str) -> None:
        """Update or add an export line in ~/.bashrc (idempotent - skips if value matches)."""
        bashrc_path = Path.home() / ".bashrc"
        try:
            content = bashrc_path.read_text() if bashrc_path.exists() else ""

            # The exact line we want to have in the file
            new_line = f'export {env_var_name}="{value}"'

            # Pattern to match existing export for this var (captures the full line)
            pattern = rf'^export\s+{env_var_name}=["\']?.*["\']?\s*$'
            match = re.search(pattern, content, re.MULTILINE)

            if match:
                # Check if existing value already matches (idempotent)
                existing_line = match.group(0).strip()
                if existing_line == new_line:
                    logger.debug(f"{env_var_name} already set correctly in ~/.bashrc, skipping")
                    return

                # Replace existing export with new value
                content = re.sub(pattern, new_line, content, flags=re.MULTILINE)
            else:
                # Append new export (with newline if file doesn't end with one)
                if content and not content.endswith("\n"):
                    content += "\n"
                content += new_line + "\n"

            bashrc_path.write_text(content)
            logger.info(f"Updated {env_var_name} in ~/.bashrc")

        except Exception as e:
            logger.warning(f"Failed to update ~/.bashrc: {e}")


class ApiKeyConnectivityTester:
    """
    Async connectivity tester for API keys.

    Tests:
    - Anthropic: Claude CLI hello world test
    - VoyageAI: Embedding API test call
    """

    VOYAGEAI_API_ENDPOINT = "https://api.voyageai.com/v1/embeddings"
    VOYAGEAI_TEST_MODEL = "voyage-3"

    def __init__(self, timeout_seconds: int = 30):
        """
        Initialize ApiKeyConnectivityTester.

        Args:
            timeout_seconds: Timeout for connectivity tests (default: 30s)
        """
        self._timeout_seconds = timeout_seconds

    async def test_anthropic_connectivity(
        self, api_key: str
    ) -> ConnectivityTestResult:
        """
        Test Anthropic API key connectivity via Claude CLI.

        Uses a simple Claude CLI invocation to verify the key works.

        Args:
            api_key: The Anthropic API key to test

        Returns:
            ConnectivityTestResult with success/failure status
        """
        start_time = time.time()

        try:
            # Create subprocess for Claude CLI test
            process = await asyncio.create_subprocess_exec(
                "claude",
                "--print",
                "Say hello in exactly one word",
                env={**os.environ, "ANTHROPIC_API_KEY": api_key},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                _, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=self._timeout_seconds
                )
            except asyncio.TimeoutError:
                process.kill()
                return ConnectivityTestResult(
                    success=False,
                    provider="anthropic",
                    error="Connection timeout - Claude CLI did not respond",
                )

            elapsed_ms = int((time.time() - start_time) * 1000)

            if process.returncode == 0:
                return ConnectivityTestResult(
                    success=True,
                    provider="anthropic",
                    response_time_ms=elapsed_ms,
                )
            else:
                error_msg = stderr.decode("utf-8", errors="replace").strip()
                return ConnectivityTestResult(
                    success=False,
                    provider="anthropic",
                    error=error_msg or "Claude CLI test failed",
                )

        except FileNotFoundError:
            return ConnectivityTestResult(
                success=False,
                provider="anthropic",
                error="Claude CLI not installed",
            )
        except Exception as e:
            return ConnectivityTestResult(
                success=False,
                provider="anthropic",
                error=f"Connectivity test failed: {str(e)}",
            )

    async def test_voyageai_connectivity(
        self, api_key: str
    ) -> ConnectivityTestResult:
        """
        Test VoyageAI API key connectivity via embedding API.

        Makes a minimal embedding request to verify the key works.

        Args:
            api_key: The VoyageAI API key to test

        Returns:
            ConnectivityTestResult with success/failure status
        """
        start_time = time.time()

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds
            ) as client:
                response = await client.post(
                    self.VOYAGEAI_API_ENDPOINT,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "input": ["test"],
                        "model": self.VOYAGEAI_TEST_MODEL,
                    },
                )

                elapsed_ms = int((time.time() - start_time) * 1000)

                if response.status_code == 200:
                    return ConnectivityTestResult(
                        success=True,
                        provider="voyageai",
                        response_time_ms=elapsed_ms,
                    )
                else:
                    response.raise_for_status()
                    # raise_for_status will throw, this is for type checker
                    return ConnectivityTestResult(
                        success=False,
                        provider="voyageai",
                        error=f"HTTP {response.status_code}",
                    )

        except asyncio.TimeoutError:
            return ConnectivityTestResult(
                success=False,
                provider="voyageai",
                error="Connection timeout - VoyageAI API did not respond",
            )
        except httpx.HTTPStatusError as e:
            return ConnectivityTestResult(
                success=False,
                provider="voyageai",
                error=f"HTTP {e.response.status_code}: {e.response.text}",
            )
        except Exception as e:
            return ConnectivityTestResult(
                success=False,
                provider="voyageai",
                error=f"Connectivity test failed: {str(e)}",
            )


@dataclass(frozen=True)
class ValidationResult:
    """Result of API key format validation.

    Attributes:
        valid: Whether the API key format is valid
        error: Error message if validation failed, None if successful
    """

    valid: bool
    error: Optional[str] = None


class ApiKeyValidator:
    """
    Validator for API key formats.

    Validates:
    - Anthropic API keys (must start with 'sk-ant-' and be at least 40 characters)
    - VoyageAI API keys (must start with 'pa-' and be at least 20 characters)
    """

    # Anthropic API key format: sk-ant-* with minimum 40 characters
    ANTHROPIC_PREFIX = "sk-ant-"
    ANTHROPIC_MIN_LENGTH = 40

    # VoyageAI API key format: pa-* with minimum 20 characters
    VOYAGEAI_PREFIX = "pa-"
    VOYAGEAI_MIN_LENGTH = 20

    @classmethod
    def validate_anthropic_format(cls, api_key: Optional[str]) -> ValidationResult:
        """
        Validate Anthropic API key format.

        Args:
            api_key: The API key to validate

        Returns:
            ValidationResult with valid=True if format is correct,
            or valid=False with error message if validation fails
        """
        # Check for None or empty
        if api_key is None or not api_key.strip():
            return ValidationResult(valid=False, error="API key is required")

        # Strip whitespace for validation
        api_key = api_key.strip()

        # Check prefix
        if not api_key.startswith(cls.ANTHROPIC_PREFIX):
            return ValidationResult(
                valid=False,
                error=f"Invalid format: must start with '{cls.ANTHROPIC_PREFIX}'",
            )

        # Check minimum length
        if len(api_key) < cls.ANTHROPIC_MIN_LENGTH:
            return ValidationResult(
                valid=False,
                error=f"Invalid format: key too short (minimum {cls.ANTHROPIC_MIN_LENGTH} characters)",
            )

        return ValidationResult(valid=True)

    @classmethod
    def validate_voyageai_format(cls, api_key: Optional[str]) -> ValidationResult:
        """
        Validate VoyageAI API key format.

        Args:
            api_key: The API key to validate

        Returns:
            ValidationResult with valid=True if format is correct,
            or valid=False with error message if validation fails
        """
        # Check for None or empty
        if api_key is None or not api_key.strip():
            return ValidationResult(valid=False, error="API key is required")

        # Strip whitespace for validation
        api_key = api_key.strip()

        # Check prefix
        if not api_key.startswith(cls.VOYAGEAI_PREFIX):
            return ValidationResult(
                valid=False,
                error=f"Invalid format: must start with '{cls.VOYAGEAI_PREFIX}'",
            )

        # Check minimum length
        if len(api_key) < cls.VOYAGEAI_MIN_LENGTH:
            return ValidationResult(
                valid=False,
                error=f"Invalid format: key too short (minimum {cls.VOYAGEAI_MIN_LENGTH} characters)",
            )

        return ValidationResult(valid=True)


class ApiKeyAutoSeeder:
    """
    Auto-seeds API keys from environment variables and config files.

    Priority order for Anthropic:
    1. ANTHROPIC_API_KEY environment variable
    2. ~/.claude.json (apiKey field)

    Priority for VoyageAI:
    1. VOYAGE_API_KEY environment variable
    """

    def __init__(self, claude_json_path: Optional[str] = None):
        """
        Initialize ApiKeyAutoSeeder.

        Args:
            claude_json_path: Path to ~/.claude.json (default: ~/.claude.json)
        """
        self._claude_json_path = Path(
            claude_json_path or Path.home() / ".claude.json"
        )

    def get_anthropic_key(self) -> Optional[str]:
        """
        Get Anthropic API key from available sources.

        Priority:
        1. ANTHROPIC_API_KEY environment variable
        2. ~/.claude.json apiKey field

        Returns:
            The API key if found, None otherwise
        """
        # Priority 1: Environment variable
        env_key = os.environ.get("ANTHROPIC_API_KEY")
        if env_key:
            return env_key

        # Priority 2: ~/.claude.json
        if self._claude_json_path.exists():
            try:
                config = json.loads(self._claude_json_path.read_text())
                json_key = config.get("apiKey")
                if json_key:
                    return json_key
            except (json.JSONDecodeError, IOError):
                logger.warning(
                    f"Failed to read {self._claude_json_path} for auto-seeding"
                )

        return None

    def get_voyageai_key(self) -> Optional[str]:
        """
        Get VoyageAI API key from available sources.

        Priority:
        1. VOYAGE_API_KEY environment variable

        Returns:
            The API key if found, None otherwise
        """
        return os.environ.get("VOYAGE_API_KEY")