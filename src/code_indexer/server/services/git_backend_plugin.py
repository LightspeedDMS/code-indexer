"""
Pluggable git backend: let a deployment supply the delegated ``GitBackend``.

Why here (the sibling of ``cli_invoker_plugin``)
------------------------------------------------
EVO-64678 (EVO-64616 Phase 2) added a ``GitBackend`` seam: every internal git
operation cidx performs -- golden/activated repo lifecycle, refresh scheduling,
git-sync, pull-updates -- routes through one ``GitBackend``, selected by the
``git_backend`` server-config field via ``git_backend_wiring.build_git_backend``.
``local`` (the default) runs git in-process; ``delegated`` must run git in the
git-mcp spoke instead, so cidx itself performs no git work.

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
git-mcp endpoint/token/timeout are read from the environment
(``CIDX_GIT_MCP_URL`` / ``_TOKEN`` / ``_TIMEOUT``) by the backend itself, so the
factory needs nothing from cidx's config.

Selection (first match wins)
----------------------------
1. ``CIDX_GIT_BACKEND`` env var, as ``"package.module:factory"``.
2. An entry point in the ``code_indexer.git_backend`` group.
3. Nothing -- the caller uses the built-in ``LocalGitBackend``.

Misconfiguration fails LOUD (``GitBackendPluginError``) rather than silently
returning None: a deployment that set ``git_backend='delegated'`` because it must
NOT run git locally would otherwise fall back to exactly the in-process git it
was trying to avoid (the same fail-loud stance the wiring takes when no delegated
backend is available).
"""

from __future__ import annotations

import logging
import os
import sys
from importlib import import_module
from importlib.metadata import entry_points
from typing import Any, Callable, List, Optional, cast

logger = logging.getLogger(__name__)

ENV_VAR = "CIDX_GIT_BACKEND"
ENTRY_POINT_GROUP = "code_indexer.git_backend"

# () -> GitBackend
GitBackendFactory = Callable[[], Any]


class GitBackendPluginError(RuntimeError):
    """A plugin was configured but could not be loaded or is not callable."""


_NO_PLUGIN = object()
_cached: object = None


def _load_from_path(spec: str) -> GitBackendFactory:
    if ":" not in spec:
        raise GitBackendPluginError(
            f"{ENV_VAR}={spec!r} is not in 'package.module:factory' form"
        )
    module_name, _, attr = spec.partition(":")
    try:
        module = import_module(module_name)
    except ImportError as exc:
        raise GitBackendPluginError(
            f"{ENV_VAR}={spec!r}: cannot import module {module_name!r}: {exc}"
        ) from exc
    factory = getattr(module, attr, None)
    if factory is None:
        raise GitBackendPluginError(
            f"{ENV_VAR}={spec!r}: module {module_name!r} has no attribute {attr!r}"
        )
    if not callable(factory):
        raise GitBackendPluginError(f"{ENV_VAR}={spec!r}: {attr!r} is not callable")
    return cast(GitBackendFactory, factory)


def _load_from_entry_points() -> Optional[GitBackendFactory]:
    # importlib.metadata.entry_points() only accepts the `group=` keyword
    # starting in Python 3.10 (SelectableGroups). On 3.9 the function takes NO
    # parameters at all and returns a plain dict of group -> List[EntryPoint];
    # `.get(group, [])` is the pre-3.10 equivalent lookup. This repo's CI
    # matrix still targets 3.9, so both call shapes must be supported.
    candidates: List[Any]
    if sys.version_info >= (3, 10):
        candidates = list(entry_points(group=ENTRY_POINT_GROUP))
    else:
        candidates = list(entry_points().get(ENTRY_POINT_GROUP, []))
    selected = next(iter(candidates), None)
    if selected is None:
        return None
    try:
        factory = selected.load()
    except Exception as exc:  # noqa: BLE001 - surfaced as a config error
        raise GitBackendPluginError(
            f"entry point {selected.name!r} in group {ENTRY_POINT_GROUP!r} "
            f"failed to load: {exc}"
        ) from exc
    if not callable(factory):
        raise GitBackendPluginError(
            f"entry point {selected.name!r} in group {ENTRY_POINT_GROUP!r} "
            "did not resolve to a callable"
        )
    return cast(GitBackendFactory, factory)


def get_git_backend_factory() -> Optional[GitBackendFactory]:
    """Return the configured git-backend factory, or None for the local default.

    Raises:
        GitBackendPluginError: a plugin was configured but is unusable. Raised
            rather than silently returning None -- see module docstring.
    """
    global _cached
    if _cached is not None:
        return None if _cached is _NO_PLUGIN else cast(GitBackendFactory, _cached)

    spec = os.environ.get(ENV_VAR, "").strip()
    factory: Optional[GitBackendFactory]
    if spec:
        factory = _load_from_path(spec)
        logger.info("git backend plugin loaded from %s=%s", ENV_VAR, spec)
    else:
        factory = _load_from_entry_points()
        if factory is not None:
            logger.info(
                "git backend plugin loaded from entry-point group %r",
                ENTRY_POINT_GROUP,
            )

    _cached = factory if factory is not None else _NO_PLUGIN
    return factory


def resolve_delegated_git_backend() -> Optional[Any]:
    """Instantiate the delegated GitBackend, or None if no plugin is configured.

    Convenience wrapper for the startup path: resolves the factory and calls it.
    A configured-but-broken factory still raises (via ``get_git_backend_factory``
    or the factory call itself) -- fail loud, never a silent local fallback.
    """
    factory = get_git_backend_factory()
    if factory is None:
        return None
    return factory()


def reset_cache() -> None:
    """Clear the resolved plugin. For tests."""
    global _cached
    _cached = None
