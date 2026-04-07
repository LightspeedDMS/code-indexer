"""MCP handler package — backward-compatible namespace alias.

Phase 1: The entire handler implementation lives in _legacy.py.
This __init__.py registers _legacy as the primary package namespace
so that patch("code_indexer.server.mcp.handlers.X") targets the same
module object as _legacy.X — preserving all existing mock patches.

Future phases will extract domain modules from _legacy.py one at a time,
updating this __init__.py to import from the domain modules instead.
"""

import sys
import importlib

# Load _legacy as a submodule first
_legacy = importlib.import_module("code_indexer.server.mcp.handlers._legacy")

# Re-register _legacy's module object under the package name so that
# patch("code_indexer.server.mcp.handlers.X") patches the same binding
# that _legacy.py's internal code uses.
sys.modules[__name__] = _legacy

# Ensure _legacy is also accessible as handlers._legacy for direct imports
sys.modules["code_indexer.server.mcp.handlers._legacy"] = _legacy
