"""Selecting the index store, and refusing to guess when delegation breaks.

The fourth seam on this pattern. Its no-fallback reason: on a pod without the
repository the built index sits on scratch space, so a silent fall back to the
local store would never publish it anywhere a restart could recover it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from code_indexer.server.services import index_artifacts_plugin
from code_indexer.server.startup.index_store_wiring import (
    VALID_INDEX_STORE_BACKENDS,
    build_configured_index_store,
    build_index_store,
)
from code_indexer.utils.index_store import DelegatedIndexStore, LocalIndexStore


@pytest.fixture(autouse=True)
def _fresh_plugin(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv(index_artifacts_plugin.ENV_VAR, raising=False)
    index_artifacts_plugin.reset_cache()
    yield
    index_artifacts_plugin.reset_cache()


def test_local_is_the_default() -> None:
    assert VALID_INDEX_STORE_BACKENDS == ("local", "delegated")
    for config in (SimpleNamespace(), SimpleNamespace(index_store_backend=None), None):
        assert isinstance(build_index_store(config), LocalIndexStore)


def test_delegated_without_a_client_raises_rather_than_falling_back() -> None:
    with pytest.raises(RuntimeError, match="Refusing to fall back"):
        build_index_store(SimpleNamespace(index_store_backend="delegated"))


def test_an_unknown_backend_raises() -> None:
    with pytest.raises(RuntimeError, match="Unknown index_store_backend"):
        build_index_store(SimpleNamespace(index_store_backend="s3"))


def test_delegated_wraps_the_plugins_client(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "fake_artifacts.py").write_text(
        "class _A:\n    is_local = False\ndef make():\n    return _A()\n"
    )
    monkeypatch.syspath_prepend(str(plugin))
    monkeypatch.setenv(index_artifacts_plugin.ENV_VAR, "fake_artifacts:make")

    store = build_configured_index_store(
        SimpleNamespace(index_store_backend="delegated")
    )

    assert isinstance(store, DelegatedIndexStore)
    assert type(store._artifacts).__name__ == "_A"


def test_the_local_store_never_loads_the_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(index_artifacts_plugin.ENV_VAR, "no_such_module:make")
    assert isinstance(build_configured_index_store(SimpleNamespace()), LocalIndexStore)


def test_the_scheduler_gets_the_store_from_the_lifecycle_manager() -> None:
    """Source-level: lifespan builds the store and every hop forwards it."""
    import inspect

    from code_indexer.server.lifecycle import global_repos_lifecycle
    from code_indexer.server.startup import lifespan

    assert "index_store=configured_index_store" in inspect.getsource(lifespan)
    assert "index_store=index_store" in inspect.getsource(global_repos_lifecycle)
