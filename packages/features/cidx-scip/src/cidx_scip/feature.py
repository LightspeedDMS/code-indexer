"""The SCIP feature object: what cidx-scip contributes to the code-indexer core.

Registered on the ``code_indexer.features`` entry-point group as ``scip``
(``cidx_scip.feature:make_feature``). The server side is ON when the
distribution is installed AND ``"scip"`` is in the core's bootstrap key
``enabled_features``; the ``cidx scip`` CLI group is available whenever the
distribution is installed (the core's CLI discovers commands by entry point,
independent of the server's ``enabled_features``).

=====================  ============================================================
Hook                   Contribution
=====================  ============================================================
``tools()``            MCP tools ``scip_definition``, ``scip_references``,
                       ``scip_dependencies``, ``scip_dependents``, ``scip_impact``,
                       ``scip_callchain``, ``scip_context``, ``scip_pr_history``,
                       ``scip_cleanup_history``, ``scip_cleanup_workspaces``,
                       ``scip_cleanup_status`` (handlers in
                       ``cidx_scip.server.mcp.scip``, docs in
                       ``cidx_scip/server/tool_docs``)
``routers()``          ``GET /scip/*``, ``POST /api/scip/multi/*``, the admin
                       ``/api/admin/scip-cleanup-*`` routes
``web_query_modes()``  the query page's ``scip`` search mode
``index_builders()``   the ``scip`` index type of the indexing pipeline (local
                       ``cidx scip generate`` or the delegated build queue)
``cli_commands()``     the ``cidx scip`` group (``cidx_scip.cli``)
``config_section``     ``scip``: query limits, workspace retention (runtime),
                       ``scip_backend`` (bootstrap) -- ``cidx_scip.config``
``migrations()``       1: the dependency-installation audit table (core 025's
                       table, the solo ``scip_audit.db`` imported)
``background_jobs()``  the  backfill, while
                       ``CIDX_SCIP_AUTO_RECONCILE_ENABLED=true``
``on_startup``         ``scip_backend`` validation, audit repository, workspace
                       cleanup service (``cidx_scip.server.wiring``)
``health()``           ``scip-backend``: the delegated plugin is resolvable
=====================  ============================================================

The handlers take ``config``, ``storage`` and ``permissions`` from the
context; the richer server pieces they still need (search-event recording,
query admission, repository access, the job tracker) come through
``cidx_scip._platform`` until the SDK grows a capability for each (see
``ServerPlatformHost``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Tuple

from cidx_sdk import (
    BackgroundJob,
    BaseFeature,
    CliCommandContribution,
    ConfigSection,
    FeatureContext,
    FeatureManifest,
    HealthReport,
    IndexBuilderContribution,
    Migration,
    RouterContribution,
    ToolContribution,
    WebQueryModeContribution,
)
from cidx_sdk import capabilities as sdk_capabilities
from cidx_sdk.feature import DEGRADED, HEALTHY

from cidx_scip._version import __version__
from cidx_scip.config import SCIP_CONFIG_SECTION, SECTION_NAME

FEATURE_NAME = SECTION_NAME

#: The SDK range this feature is built for (0.x: a MINOR bump may break).
REQUIRES_SDK = ">=0.7,<0.8"

#: Capabilities the handlers, routes, jobs and startup take from the context
#: (the core provides every one).
CAPABILITIES_FOR_HANDLERS = (
    sdk_capabilities.CONFIG,
    sdk_capabilities.STORAGE,
    sdk_capabilities.PERMISSIONS,
    sdk_capabilities.BACKGROUND_JOBS,
)

#: The ``scip_backend`` value whose builds run in the plugin's sandbox.
_DELEGATED_PLUGIN = "delegated-plugin"

#: The backfill job's name and operation type (dashboard-visible, like the
#: delegated builds it enqueues).
RECONCILE_JOB = "scip_reconcile"

#: Where the MCP tool docs ship in the wheel.
TOOL_DOCS_DIR = Path(__file__).resolve().parent / "server" / "tool_docs"


def _tool_handlers() -> Dict[str, Callable[..., object]]:
    """MCP tool name -> handler (imports the server side: server only)."""
    from cidx_scip.server.mcp import scip

    handlers: Dict[str, Callable[..., object]] = {}
    scip._register(handlers)
    return handlers


def _run_reconcile(ctx: FeatureContext) -> None:
    """One  backfill cycle over the golden repositories."""
    from cidx_scip.server import _core, runtime
    from cidx_scip.server.reconcile import (
        reconcile_max_per_cycle,
        reconcile_scip_from_filesystem,
    )

    reconcile_scip_from_filesystem(
        _core.get_golden_repo_manager(),
        runtime.backend_config(),
        reconcile_max_per_cycle(),
    )


class ScipFeature(BaseFeature):
    """The ``scip`` feature."""

    def __init__(self) -> None:
        super().__init__(
            FeatureManifest(
                name=FEATURE_NAME,
                version=__version__,
                requires_sdk=REQUIRES_SDK,
                required_capabilities=CAPABILITIES_FOR_HANDLERS,
                optional_seams=("scip",),
                description="SCIP code intelligence: definitions, references, call chains",
            )
        )

    def tools(self) -> Sequence[ToolContribution]:
        handlers = _tool_handlers()
        return tuple(
            ToolContribution(
                name=name,
                handler=handlers[name],
                doc_markdown=(TOOL_DOCS_DIR / f"{name}.md").read_text(encoding="utf-8"),
            )
            for name in tool_names()
        )

    def routers(self) -> Sequence[RouterContribution]:
        from cidx_scip.server import admin_routes, multi_routes, routes

        return (
            RouterContribution(router=routes.router),
            RouterContribution(router=multi_routes.router),
            RouterContribution(
                router=admin_routes.router,
                required_permission=admin_routes.ROUTE_PERMISSION,
            ),
        )

    def web_query_modes(self) -> Sequence[WebQueryModeContribution]:
        from cidx_scip.server.web import query_page

        return (
            WebQueryModeContribution(mode=query_page.MODE, execute=query_page.execute),
        )

    def index_builders(self) -> Sequence[IndexBuilderContribution]:
        from cidx_scip.server.index_builder import SCIP_INDEX_BUILDER

        return (SCIP_INDEX_BUILDER,)

    def cli_commands(self) -> Sequence[CliCommandContribution]:
        from cidx_scip.cli import scip_group

        return (CliCommandContribution(name="scip", command=scip_group),)

    def config_section(self) -> Optional[ConfigSection]:
        return SCIP_CONFIG_SECTION

    def migrations(self) -> Sequence[Migration]:
        from cidx_scip.server.migrations import SCIP_MIGRATIONS

        return tuple(SCIP_MIGRATIONS)

    def background_jobs(self) -> Sequence[BackgroundJob]:
        from cidx_scip.server.reconcile import (
            reconcile_enabled,
            reconcile_interval_seconds,
        )

        interval = reconcile_interval_seconds()
        if not reconcile_enabled() or interval <= 0:
            return ()
        return (
            BackgroundJob(
                name=RECONCILE_JOB,
                operation_type=RECONCILE_JOB,
                run=_run_reconcile,
                interval_seconds=float(interval),
            ),
        )

    def on_startup(self, ctx: FeatureContext) -> None:
        from cidx_scip.server import wiring

        wiring.start(ctx)

    def on_shutdown(self, ctx: FeatureContext) -> None:
        from cidx_scip.server import wiring

        wiring.stop()

    def health(self) -> Sequence[HealthReport]:
        """Whether this node can build SCIP indexes the configured way.

        Under ``scip_backend=delegated-plugin`` builds need the
        ``code_indexer.scip_backend`` plugin; its factory is looked up (not
        called: no sandbox round trip). ``local`` needs nothing checked here
        (each indexer reports its own missing toolchain per build). Blocking;
        the core calls it off the event loop.
        """
        from cidx_scip.server import runtime

        backend = runtime.scip_backend()
        detail = {"scip_backend": backend}
        if backend != _DELEGATED_PLUGIN:
            return (
                HealthReport(component="scip-backend", status=HEALTHY, detail=detail),
            )
        from cidx_scip.server.backend_plugin import plugin_factory_available

        try:
            available = plugin_factory_available()
            reason = "no code_indexer.scip_backend plugin is installed"
        except Exception as exc:  # a broken plugin is a health finding
            available, reason = False, f"the SCIP backend plugin cannot load: {exc}"
        if available:
            return (
                HealthReport(component="scip-backend", status=HEALTHY, detail=detail),
            )
        return (
            HealthReport(
                component="scip-backend",
                status=DEGRADED,
                detail=detail,
                reasons=(f"{reason}: delegated SCIP builds are skipped on this node",),
            ),
        )


def tool_names() -> Tuple[str, ...]:
    """The MCP tools this feature contributes (one doc file each)."""
    return tuple(sorted(path.stem for path in TOOL_DOCS_DIR.glob("*.md")))


def make_feature() -> ScipFeature:
    """The ``code_indexer.features`` entry point's factory."""
    return ScipFeature()
