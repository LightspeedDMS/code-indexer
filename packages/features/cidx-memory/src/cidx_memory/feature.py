"""The memory feature object: what cidx-memory contributes to the code-indexer
core.

Registered on the ``code_indexer.features`` entry-point group as ``memory``
(``cidx_memory.feature:make_feature``). The server side is ON when the
distribution is installed AND ``"memory"`` is in the core's bootstrap key
``enabled_features``. The memory feature has no CLI commands and no tables:
memories are files in the catalog repository (``cidx-meta/memories``).

=======================  ============================================================
Hook                     Contribution
=======================  ============================================================
``tools()``              MCP tools ``create_memory``, ``edit_memory``,
                         ``delete_memory`` (handlers in
                         ``cidx_memory.server.mcp.handlers``, docs in
                         ``cidx_memory/server/tool_docs``)
``routers()``            ``/api/v1/memories``: create, edit, delete
                         (``auth="api"``)
``search_augmenters()``  ``relevant_memories`` in semantic/hybrid
                         ``search_code`` responses
                         (``cidx_memory.server.retrieval``)
``config_section``       ``memory``: the retrieval settings (runtime) --
                         ``cidx_memory.config``
``on_startup``           the context; the memory store
                         (``cidx_memory.server.lifecycle``)
``health()``             the empty-state nudge present in the wheel
=======================  ============================================================

Required capabilities: :data:`REQUIRED_CAPABILITIES`. The memory visibility
rule is the ``access_projection`` capability; nothing of the core's access
internals is imported.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Tuple

from cidx_sdk import (
    ROUTER_AUTH_API,
    BaseFeature,
    ConfigSection,
    FeatureContext,
    FeatureManifest,
    HealthReport,
    RouterContribution,
    SearchAugmenterContribution,
    ToolContribution,
)
from cidx_sdk import capabilities as sdk_capabilities
from cidx_sdk.feature import DEGRADED, HEALTHY

from cidx_memory._version import __version__
from cidx_memory.config import MEMORY_CONFIG_SECTION, SECTION_NAME

FEATURE_NAME = SECTION_NAME

#: The SDK range this feature is built for (0.x: a MINOR bump may break).
REQUIRES_SDK = ">=0.7,<0.8"

#: What the search section and startup take from the context.
REQUIRED_CAPABILITIES = (
    sdk_capabilities.CONFIG,
    sdk_capabilities.ACCESS_PROJECTION,
)

_SERVER_DIR = Path(__file__).resolve().parent / "server"
#: Where the MCP tool docs ship in the wheel.
TOOL_DOCS_DIR = _SERVER_DIR / "tool_docs"
#: The empty-state nudge the search section answers when nothing matched.
NUDGE_FILE = _SERVER_DIR / "prompts" / "memory_empty_nudge.md"


def _tool_handlers() -> Dict[str, Callable[..., object]]:
    """MCP tool name -> handler (imports the server side: server only)."""
    from cidx_memory.server.mcp import handlers

    registered: Dict[str, Callable[..., object]] = {}
    handlers._register(registered)
    return registered


class MemoryFeature(BaseFeature):
    """The ``memory`` feature."""

    def __init__(self) -> None:
        super().__init__(
            FeatureManifest(
                name=FEATURE_NAME,
                version=__version__,
                requires_sdk=REQUIRES_SDK,
                required_capabilities=REQUIRED_CAPABILITIES,
                description="Memory: shared, evidence-backed technical memories",
            )
        )
        self._runtime: Optional[object] = None

    def tools(self) -> Sequence[ToolContribution]:
        return tuple(
            ToolContribution(
                name=name,
                handler=handler,
                doc_markdown=(TOOL_DOCS_DIR / f"{name}.md").read_text(encoding="utf-8"),
            )
            for name, handler in sorted(_tool_handlers().items())
        )

    def routers(self) -> Sequence[RouterContribution]:
        from cidx_memory.server import routes

        return (
            RouterContribution(
                router=routes.router,
                prefix=routes.ROUTER_PREFIX,
                auth=ROUTER_AUTH_API,
            ),
        )

    def search_augmenters(self) -> Sequence[SearchAugmenterContribution]:
        from cidx_memory.server.retrieval import MEMORY_SEARCH_AUGMENTER

        return (MEMORY_SEARCH_AUGMENTER,)

    def config_section(self) -> Optional[ConfigSection]:
        return MEMORY_CONFIG_SECTION

    def on_startup(self, ctx: FeatureContext) -> None:
        from cidx_memory.server.lifecycle import MemoryRuntime

        started = MemoryRuntime()
        started.start(ctx)
        self._runtime = started

    def on_shutdown(self, ctx: FeatureContext) -> None:
        from cidx_memory.server.lifecycle import MemoryRuntime

        started, self._runtime = self._runtime, None
        if isinstance(started, MemoryRuntime):
            started.stop()

    def health(self) -> Sequence[HealthReport]:
        """Whether the wheel carries the empty-state nudge (a packaging fault
        otherwise: a search with no matching memory would fail its section)
        and whether the memory store is running."""
        from cidx_memory.server import runtime

        detail = {"store_running": runtime.memory_store() is not None}
        if NUDGE_FILE.is_file():
            return (
                HealthReport(component="memory-assets", status=HEALTHY, detail=detail),
            )
        return (
            HealthReport(
                component="memory-assets",
                status=DEGRADED,
                detail=detail,
                reasons=("memory_empty_nudge.md missing from the installed package",),
            ),
        )


def tool_names() -> Tuple[str, ...]:
    """The MCP tools this feature contributes (one doc file each)."""
    return tuple(sorted(path.stem for path in TOOL_DOCS_DIR.glob("*.md")))


def make_feature() -> MemoryFeature:
    """The ``code_indexer.features`` entry point's factory."""
    return MemoryFeature()
