"""The cidx-xray feature wired into test apps the way the server wires it.

Since modular plan phase 4c X-Ray's MCP tools and REST routes exist only as
the feature's contributions: the core installs them (``tools()`` ->
``server/features/tools.py``, ``routers()`` -> ``server/features/web.py``)
when "xray" is in ``enabled_features``. Tests that drive them through the
MCP dispatcher or a FastAPI app install them through the same consumers.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from cidx_sdk import KNOWN_CAPABILITIES
from fastapi import FastAPI

from code_indexer.server.features.dispatch_policy import install_dispatch_policy
from code_indexer.server.features.loader import FeatureCandidate, load_features
from code_indexer.server.features.tools import install_feature_tools
from code_indexer.server.features.web import install_feature_web


def loaded_xray() -> Any:
    """The xray feature, loaded and validated by the core's loader."""
    from cidx_xray.feature import make_feature

    (loaded,) = load_features(
        ["xray"],
        candidates={
            "xray": FeatureCandidate(
                name="xray",
                load=lambda: make_feature,
                source="cidx_xray.feature:make_feature",
            )
        },
        provided_capabilities=KNOWN_CAPABILITIES,
    )
    return loaded


def mount_xray_routes(app: FastAPI) -> FastAPI:
    """Include the xray feature's routers into *app* as the server does."""
    install_feature_web(app, [loaded_xray()])
    return app


@contextmanager
def xray_tools_installed() -> Iterator[None]:
    """X-Ray's MCP tools in TOOL_REGISTRY / HANDLER_REGISTRY for the block,
    as an app built with "xray" enabled has them."""
    install_feature_tools([loaded_xray()])
    try:
        yield
    finally:
        install_feature_tools([])


@contextmanager
def xray_dispatch_policy_installed() -> Iterator[None]:
    """The job and tool dispatch policy X-Ray declares (e.g. its
    ``xray_search_batch`` job submitted without a repo_alias by design) for
    the block, as an app built with "xray" enabled has it."""
    install_dispatch_policy([loaded_xray()])
    try:
        yield
    finally:
        install_dispatch_policy([])
