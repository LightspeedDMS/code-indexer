"""The cidx-depmap feature wired into test apps the way the server wires it.

Since modular plan phase 4g the dependency map's MCP tools and pages exist
only as the feature's contributions: the core installs them (``tools()`` ->
``server/features/tools.py``, ``routers()`` -> ``server/features/web.py``)
when "depmap" is in ``enabled_features``. Tests that drive them through the
MCP dispatcher or a FastAPI app install them through the same consumers, and
give the tools and pages the core's ``access_projection`` (the catalog
projection) as the started feature has it.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Dict, Iterator, Optional, cast

import pytest
from cidx_sdk import KNOWN_CAPABILITIES
from fastapi import FastAPI

from code_indexer.server.features.loader import FeatureCandidate, load_features
from code_indexer.server.features.tools import install_feature_tools
from code_indexer.server.features.web import install_feature_web, publish_feature_nav


def loaded_depmap() -> Any:
    """The dependency-map feature, loaded and validated by the core's loader."""
    from cidx_depmap.feature import make_feature

    (loaded,) = load_features(
        ["depmap"],
        candidates={
            "depmap": FeatureCandidate(
                name="depmap",
                load=lambda: make_feature,
                source="cidx_depmap.feature:make_feature",
            )
        },
        provided_capabilities=KNOWN_CAPABILITIES,
    )
    return loaded


def mount_depmap_routes(app: FastAPI) -> FastAPI:
    """Include the dependency map's routers into *app* as the server does."""
    install_feature_web(app, [loaded_depmap()])
    return app


#: Apps the dependency map's routers were mounted into (by identity).
_MOUNTED: Dict[int, Any] = {}


def ensure_depmap_routes(app: FastAPI) -> FastAPI:
    """Mount the dependency map's routers into *app* once -- the process app
    (``code_indexer.server.app.app``) is shared by the tests of a session,
    and a second mount would collide with the first. The nav entry is
    republished every time: it lives on ``app.state``, which the server
    tests' conftest restores after each test, while the routes stay."""
    if _MOUNTED.get(id(app)) is not app:
        mount_depmap_routes(app)
        _MOUNTED[id(app)] = app
    else:
        publish_feature_nav(app, [loaded_depmap()])
    return app


@contextmanager
def depmap_tools_installed() -> Iterator[None]:
    """The dependency map's MCP tools in TOOL_REGISTRY / HANDLER_REGISTRY for
    the block, as an app built with "depmap" enabled has them."""
    install_feature_tools([loaded_depmap()])
    try:
        yield
    finally:
        install_feature_tools([])


@contextmanager
def depmap_registry_active() -> Iterator[None]:
    """This process's feature registry has the dependency map loaded for the
    block: its config section is known to the config service and
    ``feature_offered("depmap")`` is True."""
    from code_indexer.server.features.registry import (
        FeatureRegistry,
        get_active_registry,
        set_active_registry,
    )

    previous = get_active_registry()
    set_active_registry(
        FeatureRegistry([loaded_depmap()], installed=["depmap"], core_version="test")
    )
    try:
        yield
    finally:
        set_active_registry(previous)


class _Section:
    def __init__(self, settings: Dict[str, Any]) -> None:
        self._settings = settings

    def runtime_config(self) -> object:
        return None

    def section(self) -> Dict[str, Any]:
        return dict(self._settings)

    def subscribe(self, callback: Any) -> Any:
        return lambda: None


class _DepMapTestContext:
    """A started dependency-map feature's context: the core's real
    ``access_projection`` over the app (resolved per call, so a test that
    patches the app module is seen) and, when given, a ``config`` section."""

    def __init__(self, app: Any, settings: Optional[Dict[str, Any]]) -> None:
        self._app = app
        self._settings = settings

    def has_capability(self, name: str) -> bool:
        if name == "config":
            return self._settings is not None
        return name == "access_projection"

    def capability(self, name: str) -> object:
        if name == "config":
            return _Section(self._settings or {})
        from code_indexer.server.features.access_projection import (
            CoreAccessProjectionCapability,
        )

        return CoreAccessProjectionCapability(_AppOf(self._app))


class _AppOf:
    """The app the projection reads: *app*, or (None) the process app as the
    MCP handlers see it (``_utils.app_module.app``), looked up per call."""

    def __init__(self, app: Any) -> None:
        self._app = app

    @property
    def state(self) -> Any:
        if self._app is not None:
            return self._app.state
        from code_indexer.server.mcp.handlers import _utils

        return _utils.app_module.app.state


@contextmanager
def depmap_context_installed(
    app: Any = None, *, settings: Optional[Dict[str, Any]] = None
) -> Iterator[None]:
    """For the block the dependency map's tools and pages see the core
    capabilities (the catalog projection over *app*, or the process app the
    MCP handlers read) and *settings* as the ``depmap`` section -- as after
    the feature's on_startup."""
    from cidx_depmap.server import runtime

    previous = runtime.CONTEXT.get()
    runtime.CONTEXT.install(cast(Any, _DepMapTestContext(app, settings)))
    try:
        yield
    finally:
        runtime.CONTEXT.uninstall()
        if previous is not None:
            runtime.CONTEXT.install(previous)


@contextmanager
def depmap_service_installed(service: Any) -> Iterator[Any]:
    """For the block *service* is the dependency map's running service (the
    feature's slot ``runtime.SERVICE``), as after a successful on_startup;
    the slot's previous content comes back afterwards. ``None`` means the
    service did not start. Never put the service on an ``app.state``: the
    feature no longer reads it there."""
    from cidx_depmap.server import runtime

    previous = runtime.SERVICE.get()
    runtime.SERVICE.install(service)
    try:
        yield service
    finally:
        runtime.SERVICE.uninstall()
        if previous is not None:
            runtime.SERVICE.install(previous)


def depmap_service_reading(read_path: Any) -> Any:
    """A running service as the MCP tools and pages read it: the dependency
    map lives under *read_path* (``cidx_meta_read_path``)."""
    return SimpleNamespace(cidx_meta_read_path=read_path)


@contextmanager
def depmap_started(service: Any, app_state: Any = None) -> Iterator[Any]:
    """The dependency map as after its on_startup, for the block: *service*
    is the running service and the tools and pages see the core's access
    projection over *app_state* (an app's ``state``; None: the process app
    the MCP handlers read)."""
    app = None if app_state is None else SimpleNamespace(state=app_state)
    with depmap_context_installed(app), depmap_service_installed(service):
        yield service


def install_depmap_service(monkeypatch: pytest.MonkeyPatch, service: Any) -> Any:
    """:func:`depmap_service_installed` for the rest of the current test:
    the feature's ``runtime.SERVICE`` is a slot holding *service* until
    teardown."""
    from cidx_depmap.server import runtime
    from cidx_sdk.slots import CapabilitySlot

    slot: CapabilitySlot[Any] = CapabilitySlot("depmap service (test)")
    if service is not None:
        slot.install(service)
    monkeypatch.setattr(runtime, "SERVICE", slot)
    return service


def depmap_claude_config(**values: Any) -> Any:
    """A ``claude_integration_config`` as the dependency map reads it in a
    library test: the core's ``ClaudeIntegrationConfig`` fields at their
    defaults, plus the dependency-map keys (the cidx-depmap feature's section
    since modular phase 4g) at their defaults, overridden by *values*."""
    from dataclasses import asdict
    from types import SimpleNamespace

    from cidx_depmap.config import OWNED_KEYS, default_values

    from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig

    core_keys = {k: v for k, v in values.items() if k not in OWNED_KEYS}
    fields: Dict[str, Any] = asdict(ClaudeIntegrationConfig(**core_keys))
    fields.update(default_values())
    fields.update({k: v for k, v in values.items() if k in OWNED_KEYS})
    return SimpleNamespace(**fields)


def depmap_section_of(source: Any) -> Any:
    """What the dependency map reads as its ``claude_integration_config``
    from *source* (a config service or a ``ServerConfig``): the core fields
    plus the ``depmap`` section's stored values (or defaults) -- the place
    the dependency-map settings live since modular phase 4g."""
    from dataclasses import asdict
    from types import SimpleNamespace

    from cidx_depmap.config import DEPMAP_CONFIG_SECTION

    from code_indexer.server.features import config as feature_config

    config = source.get_config() if hasattr(source, "get_config") else source
    fields: Dict[str, Any] = asdict(config.claude_integration_config)
    fields.update(
        feature_config.section_values("depmap", DEPMAP_CONFIG_SECTION, config)
    )
    return SimpleNamespace(**fields)


def set_depmap_settings(service: Any, **updates: Any) -> None:
    """Set dependency-map keys through *service* (a config service) the way
    the feature config door does (``features/config.commit_runtime_update``:
    validated against the section, one audited change)."""
    from cidx_depmap.config import DEPMAP_CONFIG_SECTION

    from code_indexer.server.features import config as feature_config

    feature_config.commit_runtime_update(
        service, "depmap", DEPMAP_CONFIG_SECTION, updates, "admin"
    )


def render_depmap_feature_settings() -> str:
    """The config screen's Feature Settings block (``feature_settings.html``)
    over the dependency-map section at its defaults: the only place the
    dependency-map settings are shown and edited."""
    from pathlib import Path
    from types import SimpleNamespace

    from cidx_depmap.config import DEPMAP_CONFIG_SECTION
    from jinja2 import Environment, FileSystemLoader

    import code_indexer.server.web as web_package
    from code_indexer.server.features.config import describe_sections

    config = SimpleNamespace(feature_settings={}, feature_bootstrap_settings={})
    view = {
        "sections": describe_sections([("depmap", DEPMAP_CONFIG_SECTION)], config),
        "csrf_token": "tok",
    }
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(feature_config_view=lambda r: view))
    )
    templates = Path(web_package.__file__).parent / "templates"
    env = Environment(loader=FileSystemLoader(str(templates)), autoescape=True)
    return str(
        env.get_template("partials/feature_settings.html").render(request=request)
    )
