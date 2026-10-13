"""xray_backend selection: local by default, delegated fails startup loudly.

``delegated`` is not implemented until sandbox-mcp has ``sandbox_xray``. Until
then selecting it must stop the server -- if the error were swallowed,
app.state would hold no backend and every RustNativeBackend would quietly
compile and run caller code in this pod, the thing delegation exists to stop.
"""

from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from code_indexer.server.startup.xray_backend_wiring import (
    build_configured_xray_execution_backend,
    server_xray_execution_backend,
)


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


def test_delegated_fails_loud_without_a_local_fallback() -> None:
    with pytest.raises(RuntimeError) as excinfo:
        build_configured_xray_execution_backend(
            SimpleNamespace(xray_backend="delegated")
        )
    message = str(excinfo.value)
    assert "xray_backend='delegated'" in message
    assert "sandbox_xray" in message
    assert "Refusing to fall back to local" in message


@pytest.mark.parametrize("value", ["Local", "sandbox", "remote"])
def test_unknown_value_fails_loud(value: str) -> None:
    with pytest.raises(RuntimeError, match="Unknown xray_backend"):
        build_configured_xray_execution_backend(SimpleNamespace(xray_backend=value))


def test_server_backend_is_read_from_app_state() -> None:
    from code_indexer.server.utils import registry_factory

    parked = object()
    with patch.object(
        registry_factory,
        "_running_server_app_state",
        return_value=SimpleNamespace(xray_execution_backend=parked),
    ):
        assert server_xray_execution_backend() is parked
    with patch.object(registry_factory, "_running_server_app_state", return_value=None):
        assert server_xray_execution_backend() is None


def test_server_config_declares_xray_backend_defaulting_to_local() -> None:
    import dataclasses

    from code_indexer.server.utils.config_manager import ServerConfig

    fields = {f.name: f for f in dataclasses.fields(ServerConfig)}
    assert "xray_backend" in fields, "the seam cannot be selected at all"
    assert fields["xray_backend"].default == "local"


def test_xray_backend_is_a_bootstrap_key() -> None:
    """Read before the DB exists; unclassified, config.json is stripped on
    first boot and the setting silently reverts to local."""
    from code_indexer.server.services.config_service import BOOTSTRAP_KEYS

    assert "xray_backend" in BOOTSTRAP_KEYS


def test_xray_backend_round_trips_through_config_json(tmp_path) -> None:
    from code_indexer.server.utils.config_manager import ServerConfigManager

    manager = ServerConfigManager(server_dir_path=str(tmp_path))
    config = manager.load_config() or manager.create_default_config()
    config.xray_backend = "delegated"
    manager.save_config(config)
    reloaded = manager.load_config()
    assert reloaded is not None
    assert reloaded.xray_backend == "delegated"


def _lifespan_wiring_calls() -> list:
    from code_indexer.server.startup import lifespan

    tree = ast.parse(inspect.getsource(lifespan))
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
    return [(call, parents) for call in calls]


def test_lifespan_parks_the_selection_on_app_state() -> None:
    found = _lifespan_wiring_calls()
    assert len(found) == 1, "lifespan must build the xray_backend selection once"
    call, parents = found[0]
    assign = parents[call]
    assert isinstance(assign, ast.Assign)
    target = assign.targets[0]
    assert isinstance(target, ast.Attribute)
    assert target.attr == "xray_execution_backend"


def test_lifespan_wiring_is_not_inside_a_try() -> None:
    """A try around it would swallow the delegated RuntimeError, leave
    app.state unset, and X-Ray would run caller code locally."""
    found = _lifespan_wiring_calls()
    call, parents = found[0]
    node = call
    while node in parents:
        node = parents[node]
        assert not isinstance(node, ast.Try), (
            "build_configured_xray_execution_backend must not run inside a try"
        )
