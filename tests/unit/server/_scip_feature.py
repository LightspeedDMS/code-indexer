"""The cidx-scip feature wired into test apps the way the server wires it.

Since modular phase 4d SCIP's MCP tools, REST routes, query-page mode and
index builder exist only as the feature's contributions: the core installs
them (``tools()`` -> ``server/features/tools.py``, ``routers()`` ->
``server/features/web.py``, ``index_builders()`` -> the kernel registry,
``web_query_modes()`` -> ``server/features/query_modes.py``) when "scip" is
in ``enabled_features``. Tests that drive them through the MCP dispatcher, a
FastAPI app, the query page or the indexing pipeline install them through
the same consumers.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from cidx_sdk import KNOWN_CAPABILITIES
from fastapi import FastAPI

from code_indexer.server.features.index_builders import (
    install_feature_index_builders,
)
from code_indexer.server.features.loader import FeatureCandidate, load_features
from code_indexer.server.features.query_modes import install_feature_query_modes
from code_indexer.server.features.tools import install_feature_tools
from code_indexer.server.features.web import install_feature_web


def loaded_scip() -> Any:
    """The scip feature, loaded and validated by the core's loader."""
    from cidx_scip.feature import make_feature

    (loaded,) = load_features(
        ["scip"],
        candidates={
            "scip": FeatureCandidate(
                name="scip",
                load=lambda: make_feature,
                source="cidx_scip.feature:make_feature",
            )
        },
        provided_capabilities=KNOWN_CAPABILITIES,
    )
    return loaded


def mount_scip_routes(app: FastAPI) -> FastAPI:
    """Include the scip feature's routers into *app* as the server does
    (behind the core's authentication)."""
    install_feature_web(app, [loaded_scip()])
    return app


@contextmanager
def scip_tools_installed() -> Iterator[None]:
    """SCIP's MCP tools in TOOL_REGISTRY / HANDLER_REGISTRY for the block,
    as an app built with "scip" enabled has them."""
    install_feature_tools([loaded_scip()])
    try:
        yield
    finally:
        install_feature_tools([])


@contextmanager
def scip_pipeline_installed() -> Iterator[None]:
    """SCIP's index builder and query-page mode for the block, as an app
    built with "scip" enabled has them."""
    loaded = [loaded_scip()]
    install_feature_index_builders(loaded)
    install_feature_query_modes(loaded)
    try:
        yield
    finally:
        install_feature_index_builders([])
        install_feature_query_modes([])
