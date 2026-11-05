"""The wiki feature object: what cidx-wiki contributes to the code-indexer core.

Registered on the ``code_indexer.features`` entry-point group as ``wiki``
(``cidx_wiki.feature:make_feature``). The server side is ON when the
distribution is installed AND ``"wiki"`` is in the core's bootstrap key
``enabled_features``. The wiki has no CLI commands.

==================  ===============================================================
Hook                Contribution
==================  ===============================================================
``tools()``         MCP tool ``wiki_article_analytics`` (handler in
                    ``cidx_wiki.server.mcp.analytics``, doc in
                    ``cidx_wiki/server/tool_docs``)
``routers()``       ``/wiki``: the two ``_search`` routes (401 when
                    unauthenticated), then the article, root and asset routes
                    (login redirect), with ``static_dir`` served at
                    ``/wiki/_static/``; ``/admin``: the wiki toggle and
                    cache-refresh actions (``manage_users``)
``config_section``  ``wiki``: the five metadata settings (runtime) --
                    ``cidx_wiki.config``
``migrations()``    1: imports the wiki tables the core created (001, 024)
``on_startup``      the context; the cache invalidator subscribed to the core's
                    ``repository_events`` (``cidx_wiki.server.lifecycle``)
``health()``        templates and static files present in the wheel
==================  ===============================================================

Required capabilities: :data:`REQUIRED_CAPABILITIES`. The catalog access
projection is the ``access_projection`` capability; nothing of the core's
access internals is imported.
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

from cidx_wiki._version import __version__
from cidx_wiki.config import SECTION_NAME, WIKI_CONFIG_SECTION

FEATURE_NAME = SECTION_NAME

#: The SDK range this feature is built for (0.x: a MINOR bump may break).
REQUIRES_SDK = ">=0.7,<0.8"

#: What the routes, the MCP tool and startup take from the context.
REQUIRED_CAPABILITIES = (
    sdk_capabilities.CONFIG,
    sdk_capabilities.PERMISSIONS,
    sdk_capabilities.ACCESS_PROJECTION,
    sdk_capabilities.REPOSITORY_EVENTS,
)

_SERVER_DIR = Path(__file__).resolve().parent / "server"
#: Where the MCP tool docs ship in the wheel.
TOOL_DOCS_DIR = _SERVER_DIR / "tool_docs"
TEMPLATES_DIR = _SERVER_DIR / "templates"
STATIC_DIR = _SERVER_DIR / "static"
_TEMPLATES = ("article.html", "index.html", "wiki_base.html")
_STATIC_FILES = ("wiki.css", "wiki.js", "wiki-theme.js")


def _tool_handlers() -> Dict[str, Callable[..., object]]:
    """MCP tool name -> handler (imports the server side: server only)."""
    from cidx_wiki.server.mcp import analytics

    handlers: Dict[str, Callable[..., object]] = {}
    analytics._register(handlers)
    return handlers


class WikiFeature(BaseFeature):
    """The ``wiki`` feature."""

    def __init__(self) -> None:
        super().__init__(
            FeatureManifest(
                name=FEATURE_NAME,
                version=__version__,
                requires_sdk=REQUIRES_SDK,
                required_capabilities=REQUIRED_CAPABILITIES,
                description="Wiki: repository markdown served as browsable articles",
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
        from cidx_wiki.server import admin_routes, routes

        return (
            # The search routes first: the article routes' catch-all would
            # take "_search" as an article path.
            RouterContribution(
                router=routes.search_router,
                prefix=routes.ROUTER_PREFIX,
                auth=ROUTER_AUTH_HYBRID,
            ),
            RouterContribution(
                router=routes.page_router,
                prefix=routes.ROUTER_PREFIX,
                auth=ROUTER_AUTH_HYBRID,
                login_redirect=True,
                static_dir=str(routes.WIKI_STATIC_DIR),
            ),
            RouterContribution(
                router=admin_routes.web_router,
                prefix=admin_routes.ROUTER_PREFIX,
                required_permission=admin_routes.ROUTE_PERMISSION,
                auth=ROUTER_AUTH_HYBRID,
            ),
        )

    def config_section(self) -> Optional[ConfigSection]:
        return WIKI_CONFIG_SECTION

    def migrations(self) -> Sequence[Migration]:
        from cidx_wiki.server.migrations import WIKI_MIGRATIONS

        return tuple(WIKI_MIGRATIONS)

    def on_startup(self, ctx: FeatureContext) -> None:
        from cidx_wiki.server.lifecycle import WikiRuntime

        started = WikiRuntime()
        started.start(ctx)
        self._runtime = started

    def on_shutdown(self, ctx: FeatureContext) -> None:
        from cidx_wiki.server.lifecycle import WikiRuntime

        started, self._runtime = self._runtime, None
        if isinstance(started, WikiRuntime):
            started.stop()

    def health(self) -> Sequence[HealthReport]:
        """Whether the wheel carries what the pages render with (a packaging
        fault otherwise: every page would fail). Blocking (a few stats); the
        core calls it off the event loop."""
        missing = [
            name for name in _TEMPLATES if not (TEMPLATES_DIR / name).is_file()
        ] + [name for name in _STATIC_FILES if not (STATIC_DIR / name).is_file()]
        detail = {"missing_files": len(missing)}
        if not missing:
            return (
                HealthReport(component="wiki-assets", status=HEALTHY, detail=detail),
            )
        return (
            HealthReport(
                component="wiki-assets",
                status=DEGRADED,
                detail=detail,
                reasons=(
                    "wiki templates or static files missing from the installed "
                    "package: " + ", ".join(sorted(missing)),
                ),
            ),
        )


def tool_names() -> Tuple[str, ...]:
    """The MCP tools this feature contributes (one doc file each)."""
    return tuple(sorted(path.stem for path in TOOL_DOCS_DIR.glob("*.md")))


def make_feature() -> WikiFeature:
    """The ``code_indexer.features`` entry point's factory."""
    return WikiFeature()
