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
import sys
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union, cast

from cidx_sdk.entry_points import CLI_INVOKER_ENTRY_POINT_GROUP
from cidx_sdk.seams.cli_invoker import (
    REPOSITORY_SOURCE,
    RESOLVES_GOLDEN_REPOSITORIES as RESOLVES_GOLDEN_REPOSITORIES,
)
from cidx_sdk.seams.errors import CliInvokerPluginError as CliInvokerPluginError
from cidx_sdk.seams.resolver import SeamResolver
from cidx_sdk.seams.selection import DELEGATED_MODE

from code_indexer.kernel.seam_resolution import module_entry_point_source

logger = logging.getLogger(__name__)

ENV_VAR = "CIDX_CLI_INVOKER"
ENTRY_POINT_GROUP = CLI_INVOKER_ENTRY_POINT_GROUP
#: Kept importable: tests patch ``entry_points`` here to control resolution.
__all__ = ["ENTRY_POINT_GROUP", "ENV_VAR", "entry_points", "sys"]

# (analysis_model, soft_timeout_seconds) -> IntelligenceCliInvoker
InvokerFactory = Callable[[str, Optional[int]], Any]

#: Kept for callers that compared against it; resolution state is the
#: resolver's own cache.
_NO_PLUGIN = object()

# The shared seam resolver (``cidx_sdk.seams``), delegated mode only: with no
# plugin installed the built-in ClaudeInvoker is used.
_resolver = SeamResolver(
    seam="cli_invoker",
    env_var=ENV_VAR,
    error_type=CliInvokerPluginError,
    display_name="CLI invoker",
    entry_point_source=module_entry_point_source(__name__),
)


class _CoreRepositorySource:
    """The core's answer to "where is the repository behind this ``cwd``"
    (``cidx_sdk.seams.cli_invoker.GoldenRepositorySource``), for delegates.

    Both questions are answered by the content seam's wiring, looked up at
    CALL time so a test patching ``content_backend_wiring`` still reaches it.
    Imported lazily: resolving the CLI invoker seam must not import the
    server's startup wiring.
    """

    def holds_repositories(self) -> bool:
        from code_indexer.server.startup import content_backend_wiring

        return not content_backend_wiring.is_content_seam_delegated()

    def golden_repository(self, cwd: Union[str, Path]) -> Optional[Dict[str, str]]:
        from code_indexer.server.startup import content_backend_wiring

        return content_backend_wiring.golden_repo_source(cwd)


# Process wiring, set once and identical on every node: any process that can
# resolve a CLI invoker delegate has imported this module, so the delegate
# finds the core's repository source without importing the core.
REPOSITORY_SOURCE.install(_CoreRepositorySource())


def get_invoker_factory() -> Optional[InvokerFactory]:
    """Return the configured invoker factory, or None to use ClaudeInvoker.

    Raises:
        CliInvokerPluginError: a plugin was configured but is unusable. Raised
            rather than silently falling back to the CLI -- see module docstring.
    """
    return cast(Optional[InvokerFactory], _resolver.find_factory(DELEGATED_MODE))


# RESOLVES_GOLDEN_REPOSITORIES (from the SDK): the module attribute a plugin
# sets to True when its invoker provisions the golden repository ITSELF (from
# cidx's registry, see content_backend_wiring.golden_repo_source) and never
# reads the ``cwd`` it is handed -- so it can analyze a repository this pod
# does not hold.


def invoker_resolves_golden_repositories() -> bool:
    """Whether the configured invoker provisions golden repositories itself.

    False for the built-in ClaudeInvoker (it reads ``cwd`` on this pod) and for
    a plugin that does not declare :data:`RESOLVES_GOLDEN_REPOSITORIES`.
    """
    factory = get_invoker_factory()
    if factory is None:
        return False
    module = sys.modules.get(getattr(factory, "__module__", "") or "")
    return getattr(module, RESOLVES_GOLDEN_REPOSITORIES, False) is True


#: The ``content_backend`` value under which repositories are not on this pod.
_DELEGATED_CONTENT_BACKEND = "delegated"


def require_invoker_for_delegated_content(
    content_backend: str, *, description_generation_enabled: bool
) -> None:
    """Refuse to start a delegated-content server that has no CLI invoker plugin.

    Under ``content_backend=delegated`` the repositories are not on this pod,
    so the built-in ClaudeInvoker -- a local ``claude`` subprocess reading
    ``cwd`` here -- cannot analyze any of them. Without a plugin every golden
    repo add would fail its description generation one by one, at ERROR, for
    as long as the server runs (EVO-64616). Called once at startup so the
    misconfiguration fails once, loudly, naming the fix. A local content
    backend (the default) never resolves the plugin here, so its startup is
    unchanged. Neither does a deployment with description generation turned
    off (``golden_description_generation_enabled=false``): nothing would use
    the plugin, and the opt-out must stay a working rollback lever.

    Raises:
        CliInvokerPluginError: delegated content and no plugin configured, or a
            configured plugin that cannot be loaded (see get_invoker_factory).
    """
    if content_backend != _DELEGATED_CONTENT_BACKEND:
        return
    if not description_generation_enabled:
        return
    if get_invoker_factory() is None:
        raise CliInvokerPluginError(
            "content_backend='delegated' but no CLI invoker plugin is "
            "configured: the repositories are not on this pod, so the local "
            "claude CLI cannot generate their descriptions. Install a plugin "
            f"on the {ENTRY_POINT_GROUP!r} entry point (the image ships "
            "cidx-orchestrator-invoker) or set "
            f"{ENV_VAR}='package.module:factory'."
        )


def reset_cache() -> None:
    """Clear the resolved plugin. For tests."""
    _resolver.reset_cache()
