"""
Server startup module for database initialization and server bootstrap.

Story #19: Fix SCIP Audit Database Showing Error on Fresh Install
"""

from .database_init import initialize_scip_audit_database

__all__ = ["initialize_scip_audit_database"]
