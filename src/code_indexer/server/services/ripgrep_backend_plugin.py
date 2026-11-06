"""
Pluggable ripgrep backend: let a deployment supply the delegated ``RipgrepBackend``.

The third seam to use ``backend_plugin_loader``, after git and content. The
``RipgrepBackend`` seam decides WHERE a ripgrep argv is executed: ``local`` (the
default) runs the binary in this container, ``delegated`` hands it to the
service that owns the repositories, so cidx needs neither the mount nor the
binary.

``build_ripgrep_backend(config, *, delegated_backend=...)`` needs something to
inject for the delegated slot. This module resolves it. With no plugin
configured nothing is returned and the caller keeps the ``local`` default, so an
existing deployment is unaffected.

The contract
------------
A plugin is a zero-argument FACTORY callable::

    def make_ripgrep_backend() -> RipgrepBackend:
        ...

returning any object satisfying the ``RipgrepBackend`` protocol. Endpoint, token
and timeout come from the plugin's own environment.

Selection (first match wins)
----------------------------
1. ``CIDX_RIPGREP_BACKEND`` env var, as ``"package.module:factory"``.
2. An entry point in the ``code_indexer.ripgrep_backend`` group.
3. Nothing -- :func:`resolve_delegated_ripgrep_backend` returns None and the
   caller uses the ``local`` mode's module (``cidx-backend-workspace``),
   reached through :func:`resolve_ripgrep_backend`.

Misconfiguration fails LOUD (``RipgrepBackendPluginError``) rather than silently
returning None: an image built for delegation may not ship the ripgrep binary at
all, and a silent fallback would surface as the linear grep scan this seam
exists to retire.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from cidx_sdk.entry_points import RIPGREP_BACKEND_ENTRY_POINT_GROUP
from cidx_sdk.seams.errors import RipgrepBackendPluginError as RipgrepBackendPluginError

from code_indexer.server.services.backend_plugin_loader import BackendPluginResolver

ENV_VAR = "CIDX_RIPGREP_BACKEND"
ENTRY_POINT_GROUP = RIPGREP_BACKEND_ENTRY_POINT_GROUP

# () -> RipgrepBackend
RipgrepBackendFactoryCallable = Callable[[], Any]


_resolver = BackendPluginResolver(
    env_var=ENV_VAR,
    entry_point_group=ENTRY_POINT_GROUP,
    error_type=RipgrepBackendPluginError,
    seam_name="ripgrep",
)


def get_ripgrep_backend_factory() -> Optional[RipgrepBackendFactoryCallable]:
    """Return the configured ripgrep-backend factory, or None for local.

    Raises:
        RipgrepBackendPluginError: a plugin was configured but is unusable.
            Raised rather than silently returning None -- see module docstring.
    """
    return _resolver.get_factory()


def resolve_delegated_ripgrep_backend() -> Optional[Any]:
    """Instantiate the delegated RipgrepBackend, or None if none is configured."""
    return _resolver.resolve()


def resolve_ripgrep_backend(mode: str, *args: Any) -> Any:
    """Instantiate the ripgrep backend installed for *mode* (``local``,
    ``delegated``). The local factory takes the core's bounded process runner
    as its one argument. Refuses loudly, naming the seam, when nothing
    provides *mode*.
    """
    return _resolver.resolve_mode(mode, *args)


def reset_cache() -> None:
    """Clear the resolved plugin. For tests."""
    _resolver.reset_cache()
