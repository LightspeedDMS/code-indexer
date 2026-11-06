"""The cidx-wiki feature wired into test apps the way the server wires it.

Since modular plan phase 4e the wiki's MCP tool and routes exist only as the
feature's contributions: the core installs them (``tools()`` ->
``server/features/tools.py``, ``routers()`` -> ``server/features/web.py``)
when "wiki" is in ``enabled_features``. Tests that drive them through the MCP
dispatcher or a FastAPI app install them through the same consumers.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, cast

from cidx_sdk import KNOWN_CAPABILITIES
from fastapi import FastAPI

from code_indexer.server.features.loader import FeatureCandidate, load_features
from code_indexer.server.features.tools import install_feature_tools
from code_indexer.server.features.web import install_feature_web


def loaded_wiki() -> Any:
    """The wiki feature, loaded and validated by the core's loader."""
    from cidx_wiki.feature import make_feature

    (loaded,) = load_features(
        ["wiki"],
        candidates={
            "wiki": FeatureCandidate(
                name="wiki",
                load=lambda: make_feature,
                source="cidx_wiki.feature:make_feature",
            )
        },
        provided_capabilities=KNOWN_CAPABILITIES,
    )
    return loaded


def mount_wiki_routes(app: FastAPI) -> FastAPI:
    """Include the wiki feature's routers into *app* as the server does."""
    install_feature_web(app, [loaded_wiki()])
    return app


@contextmanager
def wiki_tools_installed() -> Iterator[None]:
    """The wiki's MCP tool in TOOL_REGISTRY / HANDLER_REGISTRY for the block,
    as an app built with "wiki" enabled has it."""
    install_feature_tools([loaded_wiki()])
    try:
        yield
    finally:
        install_feature_tools([])


@contextmanager
def wiki_registry_active() -> Iterator[None]:
    """This process's feature registry has the wiki loaded for the block, as
    on a server with "wiki" enabled: its config section is known to the
    config screen's legacy Wiki section and ``feature_offered("wiki")``."""
    from code_indexer.server.features.registry import (
        FeatureRegistry,
        get_active_registry,
        set_active_registry,
    )

    previous = get_active_registry()
    set_active_registry(
        FeatureRegistry([loaded_wiki()], installed=["wiki"], core_version="test")
    )
    try:
        yield
    finally:
        set_active_registry(previous)


class _WikiTestContext:
    """A started wiki feature's context over *app*: the core's real
    ``access_projection`` (the catalog projection) and ``permissions``."""

    def __init__(self, app: Any) -> None:
        from code_indexer.server.features.access_projection import (
            CoreAccessProjectionCapability,
        )
        from code_indexer.server.features.capabilities import (
            CorePermissionsCapability,
        )

        self._capabilities = {
            "access_projection": CoreAccessProjectionCapability(app),
            "permissions": CorePermissionsCapability(app),
        }

    def has_capability(self, name: str) -> bool:
        return name in self._capabilities

    def capability(self, name: str) -> object:
        return self._capabilities[name]


@contextmanager
def wiki_context_installed(app: Any) -> Iterator[None]:
    """The wiki's routes and MCP tool see the core capabilities of *app* for
    the block, as after the feature's on_startup (the catalog access
    projection is applied through ``access_projection``)."""
    from cidx_wiki.server import runtime

    runtime.CONTEXT.install(cast(Any, _WikiTestContext(app)))
    try:
        yield
    finally:
        runtime.CONTEXT.uninstall()
