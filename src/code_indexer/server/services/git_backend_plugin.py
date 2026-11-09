"""
Pluggable git backend: let a deployment supply the delegated ``GitBackend``.

Why here (the sibling of ``cli_invoker_plugin``)
------------------------------------------------
issue-64678 (issue-64616 Phase 2) added a ``GitBackend`` seam: every internal git
operation cidx performs -- golden/activated repo lifecycle, refresh scheduling,
git-sync, pull-updates -- routes through one ``GitBackend``, selected by the
``git_backend`` server-config field via ``git_backend_wiring.build_git_backend``.
``local`` (the default) runs git in-process; ``delegated`` must run git in the
the git service spoke instead, so cidx itself performs no git work.

``build_git_backend(config, *, delegated_backend=...)`` needs *something* to
inject for the delegated slot. This module resolves that something -- the
``GitMcpBackend`` shipped as the ``cidx_git_backend`` plugin -- exactly the way
``cli_invoker_plugin`` resolves the ClaudeInvoker replacement. With no plugin
configured, nothing is returned and the caller keeps the ``local`` default.

The contract
------------
A plugin is a zero-argument FACTORY callable::

    def make_git_backend() -> GitBackend:
        ...

returning any object satisfying the ``GitBackend`` protocol (the ~25 git
primitives in ``storage.shared.git_backend``). It takes no arguments: the
the git service endpoint/token/timeout are read from the environment
(``CIDX_GIT_MCP_URL`` / ``_TOKEN`` / ``_TIMEOUT``) by the backend itself, so the
factory needs nothing from cidx's config.

Selection (first match wins)
----------------------------
1. ``CIDX_GIT_BACKEND`` env var, as ``"package.module:factory"``.
2. An entry point in the ``code_indexer.git_backend`` group named for the
   mode: ``delegated`` (or the legacy ``gitmcp``) for the delegate, ``local``
   for the in-process backend (``cidx-backend-git``).
3. Nothing -- :func:`resolve_delegated_git_backend` returns None and the
   caller uses the ``local`` mode, itself resolved here
   (:func:`resolve_git_backend`), which refuses when nothing provides it.

Misconfiguration fails LOUD (``GitBackendPluginError``) rather than silently
returning None: a deployment that set ``git_backend='delegated'`` because it must
NOT run git locally would otherwise fall back to exactly the in-process git it
was trying to avoid (the same fail-loud stance the wiring takes when no delegated
backend is available).
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from cidx_sdk.entry_points import GIT_BACKEND_ENTRY_POINT_GROUP
from cidx_sdk.seams.errors import GitBackendPluginError as GitBackendPluginError

from code_indexer.server.services.backend_plugin_loader import BackendPluginResolver

ENV_VAR = "CIDX_GIT_BACKEND"
ENTRY_POINT_GROUP = GIT_BACKEND_ENTRY_POINT_GROUP

# () -> GitBackend
GitBackendFactory = Callable[[], Any]


# The resolution mechanism is shared with the other delegated seams
# (backend_plugin_loader); only the names below are this seam's own. Behaviour,
# message wording and the public functions are unchanged.
_resolver = BackendPluginResolver(
    env_var=ENV_VAR,
    entry_point_group=ENTRY_POINT_GROUP,
    error_type=GitBackendPluginError,
    seam_name="git",
)


def get_git_backend_factory() -> Optional[GitBackendFactory]:
    """Return the configured git-backend factory, or None for the local default.

    Raises:
        GitBackendPluginError: a plugin was configured but is unusable. Raised
            rather than silently returning None -- see module docstring.
    """
    return _resolver.get_factory()


def resolve_delegated_git_backend() -> Optional[Any]:
    """Instantiate the delegated GitBackend, or None if no plugin is configured.

    Convenience wrapper for the startup path: resolves the factory and calls it.
    A configured-but-broken factory still raises (via ``get_git_backend_factory``
    or the factory call itself) -- fail loud, never a silent local fallback.
    """
    return _resolver.resolve()


def resolve_git_backend(mode: str, *args: Any) -> Any:
    """Instantiate the git backend installed for *mode*: the provider
    registered under that entry name (``local``: ``cidx-backend-git``, given
    *args* -- the core's git process runner), or the env var for a delegate
    mode. Refuses loudly, naming the seam, when nothing provides *mode*.
    """
    return _resolver.resolve_mode(mode, *args)


def reset_cache() -> None:
    """Clear the resolved plugin. For tests."""
    _resolver.reset_cache()
