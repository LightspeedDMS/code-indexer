"""The dependency-map feature object: what cidx-depmap contributes to the
code-indexer core.

Registered on the ``code_indexer.features`` entry-point group as ``depmap``
(``cidx_depmap.feature:make_feature``). The server side is ON when the
distribution is installed AND ``"depmap"`` is in the core's bootstrap key
``enabled_features``. The dependency map has no CLI commands.

==================  ===============================================================
Hook                Contribution
==================  ===============================================================
``tools()``         MCP tools ``depmap_*`` (``cidx_depmap.server.mcp.handlers``)
                    and ``trigger_dependency_analysis``
                    (``cidx_depmap.server.mcp.trigger``); docs in
                    ``cidx_depmap/server/tool_docs``
``routers()``       ``/admin/dependency-map`` (the page, login redirect, with
                    ``static_dir`` at ``/admin/dependency-map/_static/``);
                    ``/admin`` (actions, data and HTMX partials) -- both
                    behind the core's hybrid authentication
``config_section``  ``depmap``: the scheduler, pass, refinement, fact-check and
                    auto-repair settings (runtime) -- ``cidx_depmap.config``
``migrations()``    1: adopts the tables the core keeps creating (001, 021)
``on_startup``      the context; the service and its scheduler; the
                    description fact check (``cidx_depmap.server.lifecycle``)
``health()``        templates and the page script present in the wheel
==================  ===============================================================

Required capabilities: :data:`REQUIRED_CAPABILITIES`. The catalog access
projection of the dependency map is the ``access_projection`` capability.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Tuple

from cidx_sdk import (
    ROUTER_AUTH_HYBRID,
    BaseFeature,
    ConfigSection,
    FeatureContext,
    FeatureManifest,
    HealthReport,
    Migration,
    RouterContribution,
    ToolContribution,
)
from cidx_sdk import capabilities as sdk_capabilities
from cidx_sdk.feature import DEGRADED, HEALTHY

from cidx_depmap._version import __version__
from cidx_depmap.config import DEPMAP_CONFIG_SECTION, SECTION_NAME

FEATURE_NAME = SECTION_NAME

#: The SDK range this feature is built for (0.x: a MINOR bump may break).
REQUIRES_SDK = ">=0.7,<0.8"

#: What the tools, pages and startup take from the context.
REQUIRED_CAPABILITIES = (
    sdk_capabilities.CONFIG,
    sdk_capabilities.ACCESS_PROJECTION,
)

_SERVER_DIR = Path(__file__).resolve().parent / "server"
#: Where the MCP tool docs ship in the wheel.
TOOL_DOCS_DIR = _SERVER_DIR / "tool_docs"
TEMPLATES_DIR = _SERVER_DIR / "web" / "templates"
STATIC_DIR = _SERVER_DIR / "web" / "static"
_STATIC_FILES = ("dependency_map.js",)


def _tool_handlers() -> Dict[str, Callable[..., object]]:
    """MCP tool name -> handler (imports the server side: server only)."""
    from cidx_depmap.server.mcp import handlers, trigger

    registered: Dict[str, Callable[..., object]] = {}
    handlers._register(registered)
    trigger._register(registered)
    return registered


class DepMapFeature(BaseFeature):
    """The ``depmap`` feature."""

    def __init__(self) -> None:
        super().__init__(
            FeatureManifest(
                name=FEATURE_NAME,
                version=__version__,
                requires_sdk=REQUIRES_SDK,
                required_capabilities=REQUIRED_CAPABILITIES,
                description="Dependency map: cross-repository domains",
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
        from cidx_depmap.server.web import routes

        return (
            RouterContribution(
                router=routes.dependency_map_page_router,
                prefix=routes.PAGE_PREFIX,
                auth=ROUTER_AUTH_HYBRID,
                login_redirect=True,
                static_dir=str(routes.STATIC_DIR),
                nav_label="Dependency Map",
                nav_permission=routes.NAV_PERMISSION,
            ),
            RouterContribution(
                router=routes.dependency_map_router,
                prefix=routes.ROUTER_PREFIX,
                auth=ROUTER_AUTH_HYBRID,
            ),
        )

    def config_section(self) -> Optional[ConfigSection]:
        return DEPMAP_CONFIG_SECTION

    def migrations(self) -> Sequence[Migration]:
        from cidx_depmap.server.migrations import DEPMAP_MIGRATIONS

        return tuple(DEPMAP_MIGRATIONS)

    def on_startup(self, ctx: FeatureContext) -> None:
        from cidx_depmap.server.lifecycle import DepMapRuntime

        started = DepMapRuntime()
        started.start(ctx)
        self._runtime = started

    def on_shutdown(self, ctx: FeatureContext) -> None:
        from cidx_depmap.server.lifecycle import DepMapRuntime

        started, self._runtime = self._runtime, None
        if isinstance(started, DepMapRuntime):
            started.stop()

    def health(self) -> Sequence[HealthReport]:
        """Whether the wheel carries what the page renders with (a packaging
        fault otherwise)."""
        missing = [name for name in _STATIC_FILES if not (STATIC_DIR / name).is_file()]
        if not (TEMPLATES_DIR / "dependency_map.html").is_file():
            missing.append("dependency_map.html")
        detail = {"missing_files": len(missing)}
        if not missing:
            return (
                HealthReport(component="depmap-assets", status=HEALTHY, detail=detail),
            )
        return (
            HealthReport(
                component="depmap-assets",
                status=DEGRADED,
                detail=detail,
                reasons=(
                    "dependency-map templates or static files missing from the "
                    "installed package: " + ", ".join(sorted(missing)),
                ),
            ),
        )


def tool_names() -> Tuple[str, ...]:
    """The MCP tools this feature contributes (one doc file each)."""
    return tuple(sorted(path.stem for path in TOOL_DOCS_DIR.glob("*.md")))


def make_feature() -> DepMapFeature:
    """The ``code_indexer.features`` entry point's factory."""
    return DepMapFeature()
