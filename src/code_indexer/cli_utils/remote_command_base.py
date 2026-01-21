"""Remote command base utilities for CIDX CLI.

Provides reusable patterns for remote commands including:
- Error handling and formatting
- Client factory for domain-specific API clients
- Configuration loading
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)


def _load_remote_config() -> Dict[str, Any]:
    """Load remote configuration from .code-indexer/.remote-config.

    Returns:
        Dictionary with server_url and credentials

    Raises:
        FileNotFoundError: If remote config doesn't exist
        ValueError: If config is invalid
    """
    from code_indexer.mode_detection.command_mode_detector import find_project_root

    project_root = find_project_root(Path.cwd())
    config_path = project_root / ".code-indexer" / ".remote-config"

    if not config_path.exists():
        raise FileNotFoundError(
            f"Remote configuration not found at {config_path}. "
            "Run 'cidx auth login' to configure remote access."
        )

    try:
        with open(config_path, "r") as f:
            config_data = json.load(f)

        server_url = config_data.get("server_url")
        if not server_url:
            raise ValueError("server_url not found in remote config")

        encrypted_creds = config_data.get("encrypted_credentials", {})
        return {"server_url": server_url, "credentials": encrypted_creds}
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in remote config: {e}")


def _get_error_message(error: Exception) -> Tuple[str, str]:
    """Get base message and verbose details for an error.

    Returns:
        Tuple of (base_message, verbose_details)
    """
    from code_indexer.api_clients.base_client import AuthenticationError, APIClientError
    from code_indexer.api_clients.network_error_handler import (
        NetworkConnectionError,
        NetworkTimeoutError,
        DNSResolutionError,
        SSLCertificateError,
        ServerError,
        RateLimitError,
    )

    error_msg = str(error)

    if isinstance(error, AuthenticationError):
        return (
            "Authentication failed. Please run 'cidx auth login' to re-authenticate.",
            f"Details: {error_msg}",
        )

    if isinstance(error, NetworkConnectionError):
        return (
            f"Network connection error: {error_msg}",
            "Check your network connection and server availability.",
        )

    if isinstance(error, NetworkTimeoutError):
        return (
            f"Request timed out: {error_msg}",
            "The server may be busy. Please try again later.",
        )

    if isinstance(error, DNSResolutionError):
        return (
            f"DNS resolution failed: {error_msg}",
            "Check the server URL and your network configuration.",
        )

    if isinstance(error, SSLCertificateError):
        return (
            f"SSL certificate error: {error_msg}",
            "The server's SSL certificate could not be verified.",
        )

    if isinstance(error, ServerError):
        status = getattr(error, "status_code", "unknown")
        return (
            f"Server error (HTTP {status}): {error_msg}",
            "The server encountered an internal error. Please try again later.",
        )

    if isinstance(error, RateLimitError):
        retry_after = getattr(error, "retry_after", 60)
        return (
            f"Rate limited: {error_msg}",
            f"Please wait {retry_after} seconds before trying again.",
        )

    if isinstance(error, APIClientError):
        status = getattr(error, "status_code", None)
        base = (
            f"API error (HTTP {status}): {error_msg}"
            if status
            else f"API error: {error_msg}"
        )
        return (base, "")

    return (f"Unexpected error: {error_msg}", f"Error type: {type(error).__name__}")


def handle_remote_error(error: Exception, verbose: bool = False) -> str:
    """Format remote API errors for user-friendly display.

    Args:
        error: The exception that occurred
        verbose: Include additional details if True

    Returns:
        Formatted error message string
    """
    base_msg, verbose_detail = _get_error_message(error)
    if verbose and verbose_detail:
        return f"{base_msg}\n{verbose_detail}"
    return base_msg


def get_remote_client(domain: str) -> Any:
    """Factory function to get domain-specific API clients.

    Args:
        domain: The domain identifier (repos, jobs, admin, system, etc.)

    Returns:
        Appropriate API client instance

    Raises:
        ValueError: If domain is unknown
        FileNotFoundError: If remote config doesn't exist
    """
    config = _load_remote_config()
    server_url = config["server_url"]
    credentials = config["credentials"]

    client_map = {
        "repos": ("code_indexer.api_clients.repos_client", "ReposAPIClient"),
        "jobs": ("code_indexer.api_clients.jobs_client", "JobsAPIClient"),
        "admin": ("code_indexer.api_clients.admin_client", "AdminAPIClient"),
        "system": ("code_indexer.api_clients.system_client", "SystemAPIClient"),
        "base": ("code_indexer.api_clients.base_client", "CIDXRemoteAPIClient"),
    }

    if domain not in client_map:
        raise ValueError(f"Unknown domain: {domain}")

    module_path, class_name = client_map[domain]
    module = __import__(module_path, fromlist=[class_name])
    client_class = getattr(module, class_name)
    return client_class(server_url, credentials)
