"""Selecting the content writer, and refusing to guess when delegation breaks.

The writer follows ``content_backend``: local builds nothing and never loads a
plugin; delegated requires one, because a pod without the repository has no
disk worth writing to.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from code_indexer.server.services import content_writer_plugin
from code_indexer.server.startup.content_writer_wiring import (
    build_configured_content_writer,
    build_content_writer,
    server_content_writer,
)


@pytest.fixture(autouse=True)
def _fresh_plugin(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv(content_writer_plugin.ENV_VAR, raising=False)
    content_writer_plugin.reset_cache()
    yield
    content_writer_plugin.reset_cache()


def test_the_seam_names() -> None:
    assert content_writer_plugin.ENV_VAR == "CIDX_CONTENT_WRITER"
    assert content_writer_plugin.ENTRY_POINT_GROUP == "code_indexer.content_writer"


def test_local_is_the_default_and_builds_nothing() -> None:
    for config in (
        SimpleNamespace(),
        SimpleNamespace(content_backend=None),
        SimpleNamespace(content_backend="local"),
        None,
    ):
        assert build_content_writer(config) is None


def test_local_never_loads_the_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(content_writer_plugin.ENV_VAR, "no_such_module:make")
    assert build_configured_content_writer(SimpleNamespace()) is None


def test_delegated_without_a_writer_fails_loud() -> None:
    with pytest.raises(RuntimeError, match="Refusing to fall back"):
        build_content_writer(SimpleNamespace(content_backend="delegated"))


def test_delegated_with_no_plugin_installed_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        content_writer_plugin._resolver, "_load_from_entry_points", lambda: None
    )
    with pytest.raises(RuntimeError, match="no content writer"):
        build_configured_content_writer(SimpleNamespace(content_backend="delegated"))


def test_an_unknown_backend_raises() -> None:
    with pytest.raises(RuntimeError, match="Unknown content_backend"):
        build_content_writer(SimpleNamespace(content_backend="s3"))


def test_delegated_returns_the_plugins_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "fake_writer.py").write_text(
        "class _W:\n    pass\ndef make():\n    return _W()\n"
    )
    monkeypatch.syspath_prepend(str(plugin))
    monkeypatch.setenv(content_writer_plugin.ENV_VAR, "fake_writer:make")

    writer = build_configured_content_writer(
        SimpleNamespace(content_backend="delegated")
    )

    assert type(writer).__name__ == "_W"


def test_a_broken_plugin_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(content_writer_plugin.ENV_VAR, "no_such_module:make")
    with pytest.raises(content_writer_plugin.ContentWriterPluginError):
        build_configured_content_writer(SimpleNamespace(content_backend="delegated"))


def test_the_parked_writer_is_read_from_app_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_indexer.server.utils import registry_factory

    writer = object()
    state = SimpleNamespace(content_writer=writer)
    monkeypatch.setattr(registry_factory, "_running_server_app_state", lambda: state)
    assert server_content_writer() is writer
    monkeypatch.setattr(registry_factory, "_running_server_app_state", lambda: None)
    assert server_content_writer() is None


def test_lifespan_parks_the_configured_writer() -> None:
    """Source-level: app.state.content_writer is bound from the builder."""
    from code_indexer.server.startup import lifespan

    tree = ast.parse(inspect.getsource(lifespan))
    bound = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", None) == "build_configured_content_writer"
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    parked = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute)
        and target.attr == "content_writer"
        and isinstance(target.value, ast.Attribute)
        and target.value.attr == "state"
    ]
    assert parked, "app.state.content_writer is never assigned in lifespan"
    assert all(isinstance(v, ast.Name) and v.id in bound for v in parked)
