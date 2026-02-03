"""
ClaudeCliManager Initialization on Server Startup.

Provides functionality to initialize the global ClaudeCliManager singleton
during server startup, passing the Anthropic API key and cidx-meta directory.

Story #23: Smart Description Catch-Up Mechanism (AC2)
"""

import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def initialize_claude_manager_on_startup(
    golden_repos_dir: str,
    server_config: Any,
) -> bool:
    """
    Initialize the global ClaudeCliManager singleton during server startup.

    This function should be called during server startup (lifespan function)
    to create the ClaudeCliManager singleton with the Anthropic API key
    from server config and the cidx-meta directory path.

    The manager is created even if the API key is not configured (None),
    allowing it to be updated later when the key is saved via Web UI.

    Args:
        golden_repos_dir: Path to golden-repos directory (cidx-meta is at {golden_repos_dir}/cidx-meta)
        server_config: The ServerConfig instance with claude_integration_config

    Returns:
        True if initialization succeeded, False on error
    """
    from code_indexer.server.services.claude_cli_manager import (
        initialize_claude_cli_manager,
        get_claude_cli_manager,
    )
    from code_indexer.server.middleware.correlation import get_correlation_id
    from code_indexer.server.logging_utils import format_error_log, get_log_extra

    try:
        # Check if already initialized (idempotent)
        existing_manager = get_claude_cli_manager()
        if existing_manager is not None:
            logger.info(
                "ClaudeCliManager already initialized (skipping re-initialization)",
                extra={"correlation_id": get_correlation_id()},
            )
            return True

        # Calculate cidx-meta directory path
        cidx_meta_dir = Path(golden_repos_dir) / "cidx-meta"

        # Create cidx-meta directory if it doesn't exist
        cidx_meta_dir.mkdir(parents=True, exist_ok=True)

        # Extract configuration from server config
        api_key: Optional[str] = None
        max_workers: int = 2  # Default value

        if (
            server_config
            and hasattr(server_config, "claude_integration_config")
            and server_config.claude_integration_config
        ):
            api_key = server_config.claude_integration_config.anthropic_api_key
            max_workers = (
                server_config.claude_integration_config.max_concurrent_claude_cli or 2
            )

        # Initialize the global singleton
        manager = initialize_claude_cli_manager(
            api_key=api_key,
            meta_dir=cidx_meta_dir,
            max_workers=max_workers,
        )

        # Log success with key status
        key_status = "configured" if api_key else "not configured"
        logger.info(
            f"ClaudeCliManager initialized during server startup "
            f"(meta_dir={cidx_meta_dir}, api_key={key_status}, max_workers={max_workers})",
            extra={"correlation_id": get_correlation_id()},
        )

        return True

    except Exception as e:
        # Log error but don't block server startup
        logger.error(
            format_error_log(
                "MCP-GENERAL-195",
                "Failed to initialize ClaudeCliManager during server startup",
                error=str(e),
            ),
            extra=get_log_extra("MCP-GENERAL-195"),
            exc_info=True,
        )
        return False
