"""
Server startup module for database initialization and server bootstrap.

Story #19: Fix SCIP Audit Database Showing Error on Fresh Install
Story #23: Smart Description Catch-Up Mechanism (AC2)
"""

from .database_init import initialize_scip_audit_database
from .claude_cli_startup import initialize_claude_manager_on_startup

__all__ = ["initialize_scip_audit_database", "initialize_claude_manager_on_startup"]
