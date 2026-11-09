"""The cidx-memory feature wired into test apps the way the server wires it.

Since modular plan phase 4f the memory tools, routes and the search section
exist only as the feature's contributions: the core installs them
(``tools()`` -> ``server/features/tools.py``, ``routers()`` ->
``server/features/web.py``, ``search_augmenters()`` ->
``server/features/search_augmenters.py``) when "memory" is in
``enabled_features``. Tests that drive them through the MCP dispatcher, a
FastAPI app or ``search_code`` install them through the same consumers.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional, cast

from cidx_sdk import KNOWN_CAPABILITIES
from fastapi import FastAPI

from code_indexer.server.features.loader import FeatureCandidate, load_features
from code_indexer.server.features.search_augmenters import (
    install_feature_search_augmenters,
)
from code_indexer.server.features.tools import install_feature_tools
from code_indexer.server.features.web import install_feature_web


def loaded_memory() -> Any:
    """The memory feature, loaded and validated by the core's loader."""
    from cidx_memory.feature import make_feature

    (loaded,) = load_features(
        ["memory"],
        candidates={
            "memory": FeatureCandidate(
                name="memory",
                load=lambda: make_feature,
                source="cidx_memory.feature:make_feature",
            )
        },
        provided_capabilities=KNOWN_CAPABILITIES,
    )
    return loaded


def mount_memory_routes(app: FastAPI) -> FastAPI:
    """Include the memory feature's routers into *app* as the server does."""
    install_feature_web(app, [loaded_memory()])
    return app


@contextmanager
def memory_tools_installed() -> Iterator[None]:
    """The memory MCP tools in TOOL_REGISTRY / HANDLER_REGISTRY for the
    block, as an app built with "memory" enabled has them."""
    install_feature_tools([loaded_memory()])
    try:
        yield
    finally:
        install_feature_tools([])


@contextmanager
def memory_search_section_installed() -> Iterator[None]:
    """``search_code`` answers ``relevant_memories`` for the block, as on a
    server with "memory" enabled."""
    install_feature_search_augmenters([loaded_memory()])
    try:
        yield
    finally:
        install_feature_search_augmenters([])


@contextmanager
def memory_registry_active() -> Iterator[None]:
    """This process's feature registry has the memory feature loaded for the
    block: its config section is known to the config service."""
    from code_indexer.server.features.registry import (
        FeatureRegistry,
        get_active_registry,
        set_active_registry,
    )

    previous = get_active_registry()
    set_active_registry(
        FeatureRegistry([loaded_memory()], installed=["memory"], core_version="test")
    )
    try:
        yield
    finally:
        set_active_registry(previous)


class _MemoryTestContext:
    """A started memory feature's context: the core's real
    ``access_projection`` over *app* (when given) and a ``config`` section
    with *settings*."""

    def __init__(self, app: Any, settings: Optional[Dict[str, Any]]) -> None:
        self._capabilities: Dict[str, object] = {}
        if app is not None:
            from code_indexer.server.features.access_projection import (
                CoreAccessProjectionCapability,
            )

            self._capabilities["access_projection"] = CoreAccessProjectionCapability(
                app
            )
        if settings is not None:
            self._capabilities["config"] = _SectionConfig(settings)

    def has_capability(self, name: str) -> bool:
        return name in self._capabilities

    def capability(self, name: str) -> object:
        return self._capabilities[name]


class _SectionConfig:
    def __init__(self, settings: Dict[str, Any]) -> None:
        self._settings = settings

    def runtime_config(self) -> object:
        return None

    def section(self) -> Dict[str, Any]:
        return dict(self._settings)

    def subscribe(self, callback: Any) -> Any:
        return lambda: None


@contextmanager
def memory_context_installed(
    app: Any = None,
    *,
    store: Any = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Iterator[None]:
    """For the block the memory tools, routes and search section see: the
    core capabilities of *app* (the memory visibility rule), *store* as the
    running memory store and *settings* as the ``memory`` config section --
    as after the feature's on_startup."""
    from cidx_memory.server import runtime

    runtime.CONTEXT.install(cast(Any, _MemoryTestContext(app, settings)))
    if store is not None:
        runtime.STORE.install(store)
    try:
        yield
    finally:
        runtime.STORE.uninstall()
        runtime.CONTEXT.uninstall()
