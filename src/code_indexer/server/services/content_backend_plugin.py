"""
Pluggable content backend: let a deployment supply the delegated ``ContentBackend``.

The sibling of ``git_backend_plugin``
-------------------------------------
The ``ContentBackend`` seam answers "what files are in this repository, and what
are their bytes" -- a manifest of ``{path, size, blob_sha, is_symlink}`` and a
fetch for named paths. ``local`` (the default) reads this container's
filesystem. ``delegated`` must read them through the service that owns
filesystem access instead, so that cidx needs no repository mount of its own.

``build_content_backend(config, *, delegated_backend=...)`` needs something to
inject for the delegated slot. This module resolves it, exactly the way the git
seam resolves ``GitMcpBackend``. With no plugin configured nothing is returned
and the caller keeps the ``local`` default, so an existing deployment is
unaffected.

The contract
------------
A plugin is a zero-argument FACTORY callable::

    def make_content_backend() -> ContentBackend:
        ...

returning any object satisfying the ``ContentBackend`` protocol (``manifest``,
``fetch``, ``is_local``). It takes no arguments: the endpoint, token and timeout
are read from the environment by the backend itself.

Selection (first match wins)
----------------------------
1. ``CIDX_CONTENT_BACKEND`` env var, as ``"package.module:factory"``.
2. An entry point in the ``code_indexer.content_backend`` group.
3. Nothing -- the caller uses the built-in ``LocalContentBackend``.

Misconfiguration fails LOUD (``ContentBackendPluginError``) rather than silently
returning None. This matters more here than for any other seam: a pod configured
for delegation may not mount the repositories at all, so a silent fall back to
the local filesystem would not raise -- it would report an EMPTY repository and
index nothing, which looks like a successful run.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from code_indexer.server.services.backend_plugin_loader import BackendPluginResolver

ENV_VAR = "CIDX_CONTENT_BACKEND"
ENTRY_POINT_GROUP = "code_indexer.content_backend"

# () -> ContentBackend
ContentBackendFactoryCallable = Callable[[], Any]


class ContentBackendPluginError(RuntimeError):
    """A plugin was configured but could not be loaded or is not callable."""


_resolver = BackendPluginResolver(
    env_var=ENV_VAR,
    entry_point_group=ENTRY_POINT_GROUP,
    error_type=ContentBackendPluginError,
    seam_name="content",
)


def get_content_backend_factory() -> Optional[ContentBackendFactoryCallable]:
    """Return the configured content-backend factory, or None for local.

    Raises:
        ContentBackendPluginError: a plugin was configured but is unusable.
            Raised rather than silently returning None -- see module docstring.
    """
    return _resolver.get_factory()


def resolve_delegated_content_backend() -> Optional[Any]:
    """Instantiate the delegated ContentBackend, or None if none is configured.

    A configured-but-broken factory still raises -- fail loud, never a silent
    fallback to reading the local filesystem.
    """
    return _resolver.resolve()


def reset_cache() -> None:
    """Clear the resolved plugin. For tests."""
    _resolver.reset_cache()
