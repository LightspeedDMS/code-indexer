"""
Database initialization functions for server startup.

Story #19: Fix SCIP Audit Database Showing Error on Fresh Install

This module provides eager database initialization functions that run
during server startup, before any health checks are performed.

The pattern follows existing eager initialization for groups.db (app.py line 2213)
and oauth.db (app.py line 2712).
"""

import logging
import sqlite3
from pathlib import Path
from typing import Optional
from code_indexer.server.logging_utils import format_error_log, get_log_extra

logger = logging.getLogger(__name__)


def initialize_scip_audit_database(server_data_dir: str) -> Optional[Path]:
    """
    Initialize the SCIP audit database for dependency installation tracking.

    Creates scip_audit.db with the same schema as SCIPAuditRepository._init_database()
    to ensure the database exists before health checks run.

    This function is idempotent - it uses CREATE TABLE IF NOT EXISTS and
    CREATE INDEX IF NOT EXISTS, so it's safe to call multiple times.

    Args:
        server_data_dir: Path to the server data directory (e.g., ~/.cidx-server)

    Returns:
        Path to scip_audit.db if successful, None if initialization failed

    Note:
        Initialization failure logs a warning but does not block server startup.
        This matches the error handling pattern for other database initializations.
    """
    try:
        server_dir = Path(server_data_dir)

        # Ensure parent directory exists
        server_dir.mkdir(parents=True, exist_ok=True)

        scip_audit_path = server_dir / "scip_audit.db"

        # Initialize database schema
        # Schema must match SCIPAuditRepository._init_database() exactly
        with sqlite3.connect(str(scip_audit_path), timeout=30) as conn:
            # Create scip_dependency_installations table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scip_dependency_installations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    job_id VARCHAR(36) NOT NULL,
                    repo_alias VARCHAR(255) NOT NULL,
                    project_path VARCHAR(255),
                    project_language VARCHAR(50),
                    project_build_system VARCHAR(50),
                    package VARCHAR(255) NOT NULL,
                    command TEXT NOT NULL,
                    reasoning TEXT,
                    username VARCHAR(255)
                )
            """
            )

            # Create indexes for efficient querying
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_timestamp
                ON scip_dependency_installations (timestamp)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_repo_alias
                ON scip_dependency_installations (repo_alias)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_job_id
                ON scip_dependency_installations (job_id)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_project_language
                ON scip_dependency_installations (project_language)
            """
            )

            conn.commit()

        logger.info(
            f"SCIP audit database initialized: {scip_audit_path}",
            extra={"db_path": str(scip_audit_path)},
        )

        return scip_audit_path

    except Exception as e:
        logger.warning(format_error_log(
            "MCP-GENERAL-196",
            f"Failed to initialize SCIP audit database: {e}",
            exc_info=True,
            extra={"server_data_dir": server_data_dir},
        ))
        return None
