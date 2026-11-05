"""
Multi-User CIDX Server Package

FastAPI-based server for multi-user semantic code search with role-based access control.
"""

from code_indexer.server.kernel_wiring import install_kernel_hosts

__all__: list[str] = []

# Any process that loads server code -- the app, its workers, the server
# tests -- gets the server's kernel capabilities before X-Ray or SCIP runs.
install_kernel_hosts()
