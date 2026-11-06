"""The content seam, resolved for the core's library code (no server import).

The server resolves the content backend it serves with at startup
(``server/startup/content_backend_wiring.py``). Library code that needs a
backend without a running server -- a manifest built for the plain CLI -- asks
here, by MODE, and gets whatever module is installed for it: there is no
built-in implementation in the kernel, so with nothing installed for the mode
this raises :class:`cidx_sdk.errors.SeamUnavailableError` naming the seam.
"""

from __future__ import annotations

import sys
from importlib.metadata import entry_points

from cidx_sdk.seams.content import ContentBackend
from cidx_sdk.seams.errors import ContentBackendPluginError
from cidx_sdk.seams.resolver import SeamResolver
from cidx_sdk.seams.selection import LOCAL_MODE

from code_indexer.kernel.seam_resolution import module_entry_point_source

#: The operator override of the delegated content backend (shared with the
#: server's resolver, ``server/services/content_backend_plugin.py``).
ENV_VAR = "CIDX_CONTENT_BACKEND"
#: Kept importable: tests patch ``entry_points`` here to control resolution.
__all__ = ["ENV_VAR", "content_backend_for", "entry_points", "reset_cache", "sys"]

_resolver = SeamResolver(
    seam="content",
    env_var=ENV_VAR,
    error_type=ContentBackendPluginError,
    display_name="content",
    entry_point_source=module_entry_point_source(__name__),
)


def content_backend_for(mode: str = LOCAL_MODE) -> ContentBackend:
    """A new instance of the content backend installed for *mode*.

    Raises:
        SeamUnavailableError: nothing (or more than one module) provides *mode*.
        ContentBackendPluginError: the provider is installed but unusable.
    """
    backend: ContentBackend = _resolver.resolve(mode)
    return backend


def reset_cache() -> None:
    """Forget the resolved providers. For tests."""
    _resolver.reset_cache()
