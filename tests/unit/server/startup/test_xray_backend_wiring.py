"""xray_backend selection: local by default, delegated loads the plugin or
fails startup loudly.

Under ``delegated`` the backend comes from the ``code_indexer.xray_backend``
plugin. Every way that can go wrong -- nothing installed, a plugin that does not
load, a factory that raises, an object that is not a backend -- must stop the
server: if the error were swallowed, app.state would hold no backend and every
RustNativeBackend would quietly compile and run caller code in this pod, the
thing delegation exists to stop.
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from cidx_xray.server import backend_plugin as xray_backend_plugin
from cidx_xray.server.backend_wiring import (
    build_configured_xray_execution_backend,
    server_xray_execution_backend,
)
from cidx_xray.execution_backend import (
    XrayGraphRequest,
    XrayScanOutcome,
    XrayScanRequest,
)

DELEGATED = SimpleNamespace(xray_backend="delegated")


@pytest.fixture(autouse=True)
def _fresh_plugin(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv(xray_backend_plugin.ENV_VAR, raising=False)
    xray_backend_plugin.reset_cache()
    yield
    xray_backend_plugin.reset_cache()


class _FakeBackend:
    def execute_scan(self, request: XrayScanRequest) -> XrayScanOutcome:
        return XrayScanOutcome([])

    def execute_graph_analysis(self, request: XrayGraphRequest) -> Dict[str, Any]:
        return {}


class _FakeEntryPoint:
    def __init__(self, factory: Any, *, fails: bool = False) -> None:
        self.name = "sandbox"
        self._factory = factory
        self._fails = fails

    def load(self) -> Any:
        if self._fails:
            raise ImportError("cidx_content_backend.xray: no module named requests")
        return self._factory


def _install_entry_points(
    monkeypatch: pytest.MonkeyPatch, entries: List[_FakeEntryPoint]
) -> List[str]:
    """Answer entry_points() with *entries*; returns the groups looked up.

    Both call shapes the loader uses: ``entry_points(group=...)`` (3.10+) and
    ``entry_points().get(group, [])`` (3.9).
    """
    asked: List[str] = []
    registry = {"code_indexer.xray_backend": entries}

    class _Groups(Dict[str, List[_FakeEntryPoint]]):
        def get(self, group: str, default: Any = None) -> Any:
            asked.append(group)
            return super().get(group, default)

    def _entry_points(group: Any = None) -> Any:
        if group is None:
            return _Groups(registry)
        asked.append(group)
        return registry.get(group, [])

    monkeypatch.setattr(xray_backend_plugin, "entry_points", _entry_points)
    return asked


def test_the_seam_names() -> None:
    assert xray_backend_plugin.ENV_VAR == "CIDX_XRAY_BACKEND"
    assert xray_backend_plugin.ENTRY_POINT_GROUP == "code_indexer.xray_backend"


@pytest.mark.parametrize(
    "config",
    [
        SimpleNamespace(),
        SimpleNamespace(xray_backend="local"),
        SimpleNamespace(xray_backend=""),
        SimpleNamespace(xray_backend=None),
        None,
    ],
)
def test_local_and_unset_build_nothing(config: object) -> None:
    """None tells RustNativeBackend to execute in-process, as today."""
    assert build_configured_xray_execution_backend(config) is None


def test_local_never_loads_the_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken plugin configuration is irrelevant under local: nothing loads."""
    monkeypatch.setenv(xray_backend_plugin.ENV_VAR, "no_such_module:make")
    asked = _install_entry_points(monkeypatch, [_FakeEntryPoint(_FakeBackend)])
    assert build_configured_xray_execution_backend(SimpleNamespace()) is None
    assert asked == []


def test_delegated_loads_the_entry_point_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: List[_FakeBackend] = []

    def make_xray_backend() -> _FakeBackend:
        built.append(_FakeBackend())
        return built[-1]

    asked = _install_entry_points(monkeypatch, [_FakeEntryPoint(make_xray_backend)])
    backend = build_configured_xray_execution_backend(DELEGATED)
    assert asked == ["code_indexer.xray_backend"]
    assert len(built) == 1 and backend is built[0]


def test_env_override_wins_over_the_entry_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "fake_xray_plugin.py").write_text(
        "class B:\n"
        "    def execute_scan(self, request): ...\n"
        "    def execute_graph_analysis(self, request): ...\n"
        "def make():\n"
        "    return B()\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(xray_backend_plugin.ENV_VAR, "fake_xray_plugin:make")
    _install_entry_points(monkeypatch, [_FakeEntryPoint(_FakeBackend)])
    try:
        backend = build_configured_xray_execution_backend(DELEGATED)
        assert type(backend).__module__ == "fake_xray_plugin"
    finally:
        sys.modules.pop("fake_xray_plugin", None)


def test_delegated_without_a_plugin_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_entry_points(monkeypatch, [])
    with pytest.raises(RuntimeError) as excinfo:
        build_configured_xray_execution_backend(DELEGATED)
    message = str(excinfo.value)
    assert "no X-Ray backend plugin is installed" in message
    assert "code_indexer.xray_backend" in message
    assert "CIDX_XRAY_BACKEND" in message
    assert "Refusing to fall back to local" in message


def test_a_plugin_that_fails_to_load_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_entry_points(monkeypatch, [_FakeEntryPoint(_FakeBackend, fails=True)])
    with pytest.raises(RuntimeError) as excinfo:
        build_configured_xray_execution_backend(DELEGATED)
    assert "could not be loaded or constructed" in str(excinfo.value)
    assert "Refusing to fall back to local" in str(excinfo.value)
    assert isinstance(
        excinfo.value.__cause__, xray_backend_plugin.XrayBackendPluginError
    )


def test_a_factory_that_raises_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    def make_xray_backend() -> Any:
        raise ValueError("delegated X-Ray needs a sandbox-mcp endpoint")

    _install_entry_points(monkeypatch, [_FakeEntryPoint(make_xray_backend)])
    with pytest.raises(RuntimeError) as excinfo:
        build_configured_xray_execution_backend(DELEGATED)
    message = str(excinfo.value)
    assert "ValueError: delegated X-Ray needs a sandbox-mcp endpoint" in message
    assert "Refusing to fall back to local" in message
    assert isinstance(excinfo.value.__cause__, ValueError)


def test_a_plugin_that_is_not_a_backend_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_entry_points(monkeypatch, [_FakeEntryPoint(object)])
    with pytest.raises(
        RuntimeError, match="has no execute_scan/execute_graph_analysis"
    ):
        build_configured_xray_execution_backend(DELEGATED)


def test_the_parked_plugin_backend_is_what_run_batch_uses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through the seam: what startup parks (the feature's
    runtime slot, read by its placement host) is what X-Ray calls."""
    from cidx_xray.rust_backend import RustNativeBackend
    from cidx_xray.server import runtime
    from cidx_xray.testing import placement_host_installed

    calls: List[XrayScanRequest] = []

    class _Recording(_FakeBackend):
        def execute_scan(self, request: XrayScanRequest) -> XrayScanOutcome:
            calls.append(request)
            return XrayScanOutcome([([], [], None)], debug_messages=["delegated"])

    _install_entry_points(monkeypatch, [_FakeEntryPoint(_Recording)])
    runtime.EXECUTION_BACKEND.install(
        build_configured_xray_execution_backend(DELEGATED)
    )
    try:
        with placement_host_installed():
            results = RustNativeBackend().run_batch(
                evaluator_code="fn evaluate_node() {}",
                file_specs=[{"file_path": "a.py"}],
                repo_path="/repo",
            )
    finally:
        runtime.EXECUTION_BACKEND.uninstall()
    assert results == [([], [], None)]
    assert [c.repo_path for c in calls] == ["/repo"]


@pytest.mark.parametrize("value", ["Local", "sandbox", "remote"])
def test_unknown_value_fails_loud(value: str) -> None:
    with pytest.raises(RuntimeError, match="Unknown xray_backend"):
        build_configured_xray_execution_backend(SimpleNamespace(xray_backend=value))


def test_server_backend_is_the_features_runtime_slot() -> None:
    from cidx_xray.server import runtime

    parked = object()
    runtime.EXECUTION_BACKEND.install(parked)
    try:
        assert server_xray_execution_backend() is parked
    finally:
        runtime.EXECUTION_BACKEND.uninstall()
    assert server_xray_execution_backend() is None


def test_xray_backend_is_a_bootstrap_key_of_the_feature_section() -> None:
    """Read before the DB exists (config.json), in the feature's section."""
    from cidx_xray.config import XRAY_CONFIG_SECTION

    from code_indexer.server.services.config_service import BOOTSTRAP_KEYS

    assert XRAY_CONFIG_SECTION.bootstrap_keys == {"xray_backend"}
    assert "feature_bootstrap_settings" in BOOTSTRAP_KEYS
    assert "xray_backend" not in BOOTSTRAP_KEYS  # no longer a core key


def test_xray_backend_round_trips_through_config_json(tmp_path) -> None:
    """An old config.json's top-level xray_backend becomes the feature's
    bootstrap setting on load, and stays there when saved."""
    import json

    from code_indexer.server.utils.config_manager import ServerConfigManager

    manager = ServerConfigManager(server_dir_path=str(tmp_path))
    manager.save_config(manager.create_default_config())
    path = tmp_path / "config.json"
    data = json.loads(path.read_text())
    data["xray_backend"] = "delegated"
    path.write_text(json.dumps(data))

    loaded = manager.load_config()
    assert loaded is not None
    assert loaded.feature_bootstrap_settings["xray"] == {"xray_backend": "delegated"}
    manager.save_config(loaded)
    saved = json.loads(path.read_text())
    assert "xray_backend" not in saved
    assert saved["feature_bootstrap_settings"]["xray"]["xray_backend"] == "delegated"


def test_the_feature_startup_builds_the_selection_outside_any_try() -> None:
    """A try around it would swallow the delegated RuntimeError and X-Ray
    would run caller code locally (cidx_xray.server.lifecycle)."""
    from cidx_xray.server import lifecycle

    tree = ast.parse(inspect.getsource(lifecycle))
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "build_configured_xray_execution_backend"
    ]
    assert len(calls) == 1
    current: Any = calls[0]
    while current in parents:
        current = parents[current]
        assert not isinstance(current, ast.Try)


def test_the_core_lifespan_no_longer_builds_it() -> None:
    from code_indexer.server.startup import lifespan

    assert "xray" not in inspect.getsource(lifespan).lower()
