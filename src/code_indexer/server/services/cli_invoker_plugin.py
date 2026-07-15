"""
Pluggable CLI invoker: let a deployment supply the IntelligenceCliInvoker.

Why here, and not at invoke_claude_cli
--------------------------------------
Every description/analysis LLM call -- golden-repo registration lifecycle,
dep-map passes, description refresh, self-monitoring -- is dispatched through a
single CliDispatcher built by ``build_dep_map_dispatcher`` (its own docstring:
"the single source of truth for CliDispatcher construction across all callers").
That dispatcher calls ``invoker.invoke(flow, cwd, prompt, timeout, max_turns)``
on a ClaudeInvoker, which shells out to the ``claude`` CLI.

``repo_analyzer.invoke_claude_cli`` is a DIFFERENT, secondary path. Seaming
there does not touch the lifecycle; seaming here does, and covers every consumer
at once, with the richer InvocationResult contract dep-map needs.

A deployment that cannot ship the ``claude`` CLI in the server image -- e.g. one
that must keep LLM execution inside a separate sandboxed agent runner -- can
supply its own IntelligenceCliInvoker for the primary (``claude=``) slot. With
no plugin configured, nothing changes: the built-in ClaudeInvoker is used.

The contract
------------
A plugin is a FACTORY callable::

    def make_invoker(analysis_model: str,
                     soft_timeout_seconds: int | None) -> IntelligenceCliInvoker:
        ...

returning any object with
``invoke(flow, cwd, prompt, timeout, max_turns=0) -> InvocationResult``. The two
arguments mirror what ClaudeInvoker is constructed with, so the plugin can honour
the same model/timeout budget.

Selection (first match wins)
----------------------------
1. ``CIDX_CLI_INVOKER`` env var, as ``"package.module:factory"``.
2. An entry point in the ``code_indexer.cli_invoker`` group.
3. Nothing -- the built-in ClaudeInvoker.

Misconfiguration fails LOUD (``CliInvokerPluginError``) rather than silently
falling back to the CLI: a deployment that asked for a plugin because it must
NOT execute the CLI locally would otherwise get exactly the behaviour it was
trying to avoid.
"""

from __future__ import annotations

import logging
import os
from importlib import import_module
from importlib.metadata import entry_points
from typing import Any, Callable, Optional, cast

logger = logging.getLogger(__name__)

ENV_VAR = "CIDX_CLI_INVOKER"
ENTRY_POINT_GROUP = "code_indexer.cli_invoker"

# (analysis_model, soft_timeout_seconds) -> IntelligenceCliInvoker
InvokerFactory = Callable[[str, Optional[int]], Any]


class CliInvokerPluginError(RuntimeError):
    """A plugin was configured but could not be loaded or is not callable."""


_NO_PLUGIN = object()
_cached: object = None


def _load_from_path(spec: str) -> InvokerFactory:
    if ":" not in spec:
        raise CliInvokerPluginError(
            f"{ENV_VAR}={spec!r} is not in 'package.module:factory' form"
        )
    module_name, _, attr = spec.partition(":")
    try:
        module = import_module(module_name)
    except ImportError as exc:
        raise CliInvokerPluginError(
            f"{ENV_VAR}={spec!r}: cannot import module {module_name!r}: {exc}"
        ) from exc
    factory = getattr(module, attr, None)
    if factory is None:
        raise CliInvokerPluginError(
            f"{ENV_VAR}={spec!r}: module {module_name!r} has no attribute {attr!r}"
        )
    if not callable(factory):
        raise CliInvokerPluginError(f"{ENV_VAR}={spec!r}: {attr!r} is not callable")
    return cast(InvokerFactory, factory)


def _load_from_entry_points() -> Optional[InvokerFactory]:
    selected = next(iter(entry_points(group=ENTRY_POINT_GROUP)), None)
    if selected is None:
        return None
    try:
        factory = selected.load()
    except Exception as exc:  # noqa: BLE001 - surfaced as a config error
        raise CliInvokerPluginError(
            f"entry point {selected.name!r} in group {ENTRY_POINT_GROUP!r} "
            f"failed to load: {exc}"
        ) from exc
    if not callable(factory):
        raise CliInvokerPluginError(
            f"entry point {selected.name!r} in group {ENTRY_POINT_GROUP!r} "
            "did not resolve to a callable"
        )
    return cast(InvokerFactory, factory)


def get_invoker_factory() -> Optional[InvokerFactory]:
    """Return the configured invoker factory, or None to use ClaudeInvoker.

    Raises:
        CliInvokerPluginError: a plugin was configured but is unusable. Raised
            rather than silently falling back to the CLI -- see module docstring.
    """
    global _cached
    if _cached is not None:
        return None if _cached is _NO_PLUGIN else cast(InvokerFactory, _cached)

    spec = os.environ.get(ENV_VAR, "").strip()
    factory: Optional[InvokerFactory]
    if spec:
        factory = _load_from_path(spec)
        logger.info("CLI invoker plugin loaded from %s=%s", ENV_VAR, spec)
    else:
        factory = _load_from_entry_points()
        if factory is not None:
            logger.info(
                "CLI invoker plugin loaded from entry-point group %r",
                ENTRY_POINT_GROUP,
            )

    _cached = factory if factory is not None else _NO_PLUGIN
    return factory


def reset_cache() -> None:
    """Clear the resolved plugin. For tests."""
    global _cached
    _cached = None
