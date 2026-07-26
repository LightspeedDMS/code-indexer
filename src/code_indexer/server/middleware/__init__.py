"""
Middleware modules for the CIDX server.

Contains middleware components for request/response processing.
"""

from typing import Any

__all__ = ["GlobalErrorHandler"]


def __getattr__(name: str) -> Any:
    """PEP 562 lazy attribute resolution (Bug #1468).

    Python always executes a package's __init__.py before any of its
    submodules -- so a plain `from code_indexer.server.middleware.correlation
    import get_correlation_id` (used deep inside FilesystemVectorStore's
    import chain: governed_call -> coalescer_registry -> config_service)
    previously forced THIS module's eager `from .error_handler import
    GlobalErrorHandler` to run too, pulling fastapi into every process that
    merely imports FilesystemVectorStore -- including pure CLI/solo usage
    with no legitimate need for fastapi at all.

    GlobalErrorHandler is still fully available via
    `from code_indexer.server.middleware import GlobalErrorHandler`,
    resolved lazily on first actual access.
    """
    if name == "GlobalErrorHandler":
        from .error_handler import GlobalErrorHandler

        return GlobalErrorHandler
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
