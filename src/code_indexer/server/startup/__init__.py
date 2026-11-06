"""
Server startup module for server bootstrap.

(Story #19's eager SCIP audit database creation moved into the cidx-scip
feature's migration 1, modular phase 4d.)
Story #23: Smart Description Catch-Up Mechanism (AC2)
"""

from .claude_cli_startup import initialize_claude_manager_on_startup

__all__ = ["initialize_claude_manager_on_startup"]
