"""The X-Ray feature object: what cidx-xray contributes to the code-indexer core.

Registered on the ``code_indexer.features`` entry-point group as ``xray``
(``cidx_xray.feature:make_feature``). The server side is ON when the
distribution is installed AND ``"xray"`` is in the core's bootstrap key
``enabled_features``; the ``cidx xray`` CLI group is available whenever the
distribution is installed (the core's CLI discovers commands by entry point,
independent of the server's ``enabled_features``).

==================  ===============================================================
Hook                Contribution
==================  ===============================================================
``tools()``         MCP tools ``xray_search``, ``xray_explore``, ``xray_dump_ast``,
                    ``store_xray_pattern``, ``xray_search_batch``, ``analyze_graph``
                    (handlers in ``cidx_xray.server.mcp``, docs in
                    ``cidx_xray/server/tool_docs``)
``routers()``       ``POST /api/xray/search``, ``POST /api/xray/search/batch``
``cli_commands()``  the ``cidx xray`` group (``cidx_xray.cli``)
``config_section``  ``xray``: timeout and worker threads (runtime), ``xray_backend``
                    (bootstrap) -- ``cidx_xray.config``
``migrations()``    1: the graph-build K-calibration table (was core migration 051)
``on_startup``      executor, execution backend and placement host, cell limiter,
                    graph cache under the memory governor, K provider
                    (``cidx_xray.server.lifecycle``)
``health()``        ``xray-cli`` found and executable, or execution delegated
``job_types()``     the search jobs: hidden from the dashboard; batches carry no
                    repo_alias (the tools contribution marks the three tools that
                    bound their own runtime: ``manages_own_timeout``)
==================  ===============================================================

Required capabilities are :data:`CAPABILITIES_FOR_HANDLERS`. The handlers
take ``config``, ``background_jobs``, ``permissions``, ``storage`` and
``telemetry`` from the context; the richer server pieces they still need
(job tracker lifecycle, paged payload cache, the search-event recorder, the
repository guard) come through ``cidx_xray._platform`` until the SDK grows a
capability for each (see ``ServerPlatformHost``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Tuple

from cidx_sdk import (
    BaseFeature,
    CliCommandContribution,
    ConfigSection,
    FeatureContext,
    FeatureManifest,
    HealthReport,
    JobTypeContribution,
    Migration,
    RouterContribution,
    ToolContribution,
)
from cidx_sdk import capabilities as sdk_capabilities
from cidx_sdk.feature import DEGRADED, HEALTHY

from cidx_xray._version import __version__
from cidx_xray.config import BACKEND, DEFAULT_BACKEND, SECTION_NAME
from cidx_xray.config import XRAY_CONFIG_SECTION as XRAY_CONFIG_SECTION

FEATURE_NAME = SECTION_NAME

#: The SDK range this feature is built for (0.x: a MINOR bump may break).
REQUIRES_SDK = ">=0.7,<0.8"

#: Capabilities the MCP handlers, REST routes and startup take from the
#: context (the core provides every one).
CAPABILITIES_FOR_HANDLERS = (
    sdk_capabilities.CONFIG,
    sdk_capabilities.ACCESS_FILTER,
    sdk_capabilities.PERMISSIONS,
    sdk_capabilities.BACKGROUND_JOBS,
    sdk_capabilities.PAYLOAD_CACHE,
    sdk_capabilities.SEARCH_EVENTS,
    sdk_capabilities.TELEMETRY,
    sdk_capabilities.STORAGE,
)

#: The ``xray_backend`` value that runs evaluators outside this process.
_DELEGATED = "delegated"

#: Tools that bound their own runtime (a validated ``await_seconds`` /
#: ``timeout_seconds``; analyze_graph runs in a worker thread the generic
#: dispatch timeout could not cancel), so the core applies no generic
#: dispatch timeout to them (Story #1491 AC6, Issue #1811).
OWN_TIMEOUT_TOOLS = frozenset({"xray_search", "xray_explore", "analyze_graph"})

#: Operation types of the per-search jobs: kept out of the dashboard's
#: recent-jobs panel (Story #1400 Phase 9); a batch has no per-repository
#: dedup and is submitted without a repo_alias by design (Bug #1535).
JOB_TYPES = (
    JobTypeContribution("xray_search", hidden_from_dashboard=True),
    JobTypeContribution("xray_explore", hidden_from_dashboard=True),
    JobTypeContribution(
        "xray_search_batch", hidden_from_dashboard=True, repo_alias_optional=True
    ),
)

#: Where the MCP tool docs ship in the wheel.
TOOL_DOCS_DIR = Path(__file__).resolve().parent / "server" / "tool_docs"


def _tool_handlers() -> Dict[str, Callable[..., object]]:
    """MCP tool name -> handler (imports the server side: server only)."""
    from cidx_xray.server.mcp import xray, xray_batch, xray_graph

    handlers: Dict[str, Callable[..., object]] = {}
    xray._register(handlers)
    xray_batch._register(handlers)
    xray_graph._register(handlers)
    return handlers


def _configured_xray_backend() -> str:
    """``xray_backend`` from the feature's config section; ``local`` when the
    feature has not started (or the section cannot be read)."""
    from cidx_xray.server import runtime

    try:
        value = runtime.config_section().get(BACKEND)
    except Exception:  # health never fails on a config read
        value = None
    return value if isinstance(value, str) and value else DEFAULT_BACKEND


class XrayFeature(BaseFeature):
    """The ``xray`` feature."""

    def __init__(self) -> None:
        super().__init__(
            FeatureManifest(
                name=FEATURE_NAME,
                version=__version__,
                requires_sdk=REQUIRES_SDK,
                required_capabilities=CAPABILITIES_FOR_HANDLERS,
                optional_seams=("xray",),
                description="X-Ray: precision AST-aware code search",
            )
        )
        self._runtime: Optional[object] = None

    def tools(self) -> Sequence[ToolContribution]:
        return tuple(
            ToolContribution(
                name=name,
                handler=handler,
                doc_markdown=(TOOL_DOCS_DIR / f"{name}.md").read_text(encoding="utf-8"),
                manages_own_timeout=name in OWN_TIMEOUT_TOOLS,
            )
            for name, handler in sorted(_tool_handlers().items())
        )

    def job_types(self) -> Sequence[JobTypeContribution]:
        return JOB_TYPES

    def routers(self) -> Sequence[RouterContribution]:
        from cidx_xray.server.routes import ROUTE_PERMISSION, ROUTER_PREFIX, router

        return (
            RouterContribution(
                router=router,
                prefix=ROUTER_PREFIX,
                required_permission=ROUTE_PERMISSION,
            ),
        )

    def cli_commands(self) -> Sequence[CliCommandContribution]:
        from cidx_xray.cli import xray_group

        return (CliCommandContribution(name="xray", command=xray_group),)

    def config_section(self) -> Optional[ConfigSection]:
        return XRAY_CONFIG_SECTION

    def migrations(self) -> Sequence[Migration]:
        from cidx_xray.server.migrations import XRAY_MIGRATIONS

        return tuple(XRAY_MIGRATIONS)

    def on_startup(self, ctx: FeatureContext) -> None:
        from cidx_xray.server.lifecycle import XrayRuntime

        started = XrayRuntime()
        started.start(ctx)
        self._runtime = started

    def on_shutdown(self, ctx: FeatureContext) -> None:
        from cidx_xray.server.lifecycle import XrayRuntime

        started, self._runtime = self._runtime, None
        if isinstance(started, XrayRuntime):
            started.stop()

    def health(self) -> Sequence[HealthReport]:
        """Whether this node can execute an X-Ray evaluator.

        In-process execution needs the ``xray-cli`` binary on this node; under
        ``xray_backend=delegated`` evaluators run in the plugin's sandbox and
        the local binary is not used. Reports no path (health output is not
        the place for this node's filesystem layout). Blocking (one stat);
        the core calls it off the event loop.
        """
        from cidx_xray.rust_backend import resolve_xray_cli_path

        backend = _configured_xray_backend()
        binary = resolve_xray_cli_path()
        found = binary.is_file() and os.access(binary, os.X_OK)
        detail = {"xray_backend": backend, "xray_cli_found": found}
        if found or backend == _DELEGATED:
            return (HealthReport(component="xray-cli", status=HEALTHY, detail=detail),)
        return (
            HealthReport(
                component="xray-cli",
                status=DEGRADED,
                detail=detail,
                reasons=(
                    "xray-cli binary not found or not executable: X-Ray "
                    "searches will fail on this node (set CIDX_XRAY_CLI_PATH "
                    "or build rust/ with cargo)",
                ),
            ),
        )


def tool_names() -> Tuple[str, ...]:
    """The MCP tools this feature contributes (one doc file each)."""
    return tuple(sorted(path.stem for path in TOOL_DOCS_DIR.glob("*.md")))


def make_feature() -> XrayFeature:
    """The ``code_indexer.features`` entry point's factory."""
    return XrayFeature()
