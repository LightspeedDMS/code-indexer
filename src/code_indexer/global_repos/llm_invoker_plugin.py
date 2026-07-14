"""
Pluggable LLM invocation: let a deployment supply its own agent backend.

Why
---
Repo analysis (and the golden-repo registration lifecycle that depends on it)
runs its prompt by shelling out to the ``claude`` CLI. That requires the CLI --
and an API key -- to be present in whatever process/image runs the server.

Some deployments cannot ship it there. A containerized/multi-pod server may be
required to keep LLM execution inside a separate, sandboxed agent runner rather
than inside the server image; and without the CLI on PATH every registration
lifecycle job fails with ``exit 127``.

This module lets such a deployment substitute its own invocation backend --
e.g. one that submits the prompt to an external agent runner and returns the
result -- WITHOUT patching the server. Nothing about the default behaviour
changes: with no plugin configured, the built-in Claude CLI subprocess is used
exactly as before.

The contract
------------
A plugin is any callable with the same signature and return contract as
``repo_analyzer.invoke_claude_cli``::

    def invoke(
        repo_path: str,
        prompt: str,
        shell_timeout_seconds: int,
        outer_timeout_seconds: int,
    ) -> tuple[bool, str]:
        '''Return (True, output) on success, (False, error_message) on failure.'''

It is called from a worker thread and MAY block for the duration of the
timeouts -- the caller already blocks on a subprocess today, so a plugin that
submits a job and polls for its result fits the existing threading model with
no changes.

Selecting a plugin (first match wins)
-------------------------------------
1. ``CIDX_LLM_INVOKER`` env var, as ``"package.module:callable"``.
2. An entry point in the ``code_indexer.llm_invoker`` group::

       [project.entry-points."code_indexer.llm_invoker"]
       my_backend = "my_pkg.invoker:invoke"

3. Nothing -- the built-in Claude CLI subprocess.

Misconfiguration fails LOUD (``LlmInvokerPluginError``) rather than silently
falling back to the CLI: a deployment that asked for a plugin because it must
not execute the CLI locally would otherwise get exactly the behaviour it was
trying to avoid, and only find out from a stack trace inside a background job.
"""

from __future__ import annotations

import logging
import os
from importlib import import_module
from importlib.metadata import entry_points
from typing import Callable, Optional, Tuple, cast

logger = logging.getLogger(__name__)

ENV_VAR = "CIDX_LLM_INVOKER"
ENTRY_POINT_GROUP = "code_indexer.llm_invoker"

# (repo_path, prompt, shell_timeout_seconds, outer_timeout_seconds) -> (ok, output)
LlmInvoker = Callable[[str, str, int, int], Tuple[bool, str]]


class LlmInvokerPluginError(RuntimeError):
    """A plugin was configured but could not be loaded or is not callable."""


# Resolved once per process. None = "not looked up yet"; the sentinel below
# distinguishes that from "looked up, no plugin configured".
_NO_PLUGIN = object()
_cached: object = None


def _load_from_path(spec: str) -> LlmInvoker:
    """Load ``"package.module:callable"``."""
    if ":" not in spec:
        raise LlmInvokerPluginError(
            f"{ENV_VAR}={spec!r} is not in 'package.module:callable' form"
        )
    module_name, _, attr = spec.partition(":")
    try:
        module = import_module(module_name)
    except ImportError as exc:
        raise LlmInvokerPluginError(
            f"{ENV_VAR}={spec!r}: cannot import module {module_name!r}: {exc}"
        ) from exc

    invoker = getattr(module, attr, None)
    if invoker is None:
        raise LlmInvokerPluginError(
            f"{ENV_VAR}={spec!r}: module {module_name!r} has no attribute {attr!r}"
        )
    if not callable(invoker):
        raise LlmInvokerPluginError(f"{ENV_VAR}={spec!r}: {attr!r} is not callable")
    return cast(LlmInvoker, invoker)


def _load_from_entry_points() -> Optional[LlmInvoker]:
    selected = next(iter(entry_points(group=ENTRY_POINT_GROUP)), None)
    if selected is None:
        return None

    try:
        invoker = selected.load()
    except Exception as exc:  # noqa: BLE001 - surfaced as a config error
        raise LlmInvokerPluginError(
            f"entry point {selected.name!r} in group {ENTRY_POINT_GROUP!r} "
            f"failed to load: {exc}"
        ) from exc

    if not callable(invoker):
        raise LlmInvokerPluginError(
            f"entry point {selected.name!r} in group {ENTRY_POINT_GROUP!r} "
            "did not resolve to a callable"
        )
    return cast(LlmInvoker, invoker)


def get_llm_invoker() -> Optional[LlmInvoker]:
    """Return the configured plugin, or None to use the built-in Claude CLI.

    Raises:
        LlmInvokerPluginError: A plugin was configured but is unusable. Raised
            rather than silently falling back to the CLI -- see module docstring.
    """
    global _cached
    if _cached is not None:
        return None if _cached is _NO_PLUGIN else cast(LlmInvoker, _cached)

    spec = os.environ.get(ENV_VAR, "").strip()
    invoker: Optional[LlmInvoker]
    if spec:
        invoker = _load_from_path(spec)
        logger.info("LLM invoker plugin loaded from %s=%s", ENV_VAR, spec)
    else:
        invoker = _load_from_entry_points()
        if invoker is not None:
            logger.info(
                "LLM invoker plugin loaded from entry-point group %r",
                ENTRY_POINT_GROUP,
            )

    _cached = invoker if invoker is not None else _NO_PLUGIN
    return invoker


def reset_cache() -> None:
    """Clear the resolved plugin. For tests."""
    global _cached
    _cached = None
