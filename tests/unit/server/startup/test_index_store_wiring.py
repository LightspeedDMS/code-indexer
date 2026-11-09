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
    SEAMS_REQUIRED_BY_DELEGATED,
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


def _delegated(**overrides: Any) -> SimpleNamespace:
    """A config selecting the delegated store with the seams it requires."""
    fields = {"index_store_backend": "delegated"}
    fields.update({seam: "delegated" for seam in SEAMS_REQUIRED_BY_DELEGATED})
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_local_is_the_default() -> None:
    assert VALID_INDEX_STORE_BACKENDS == ("local", "delegated")
    for config in (SimpleNamespace(), SimpleNamespace(index_store_backend=None), None):
        assert isinstance(build_index_store(config), LocalIndexStore)


def test_delegated_without_a_client_raises_rather_than_falling_back() -> None:
    with pytest.raises(RuntimeError, match="Refusing to fall back"):
        build_index_store(_delegated())


@pytest.mark.parametrize(
    "overrides,named",
    [
        ({"content_backend": "local"}, ["content_backend"]),
        ({"ripgrep_backend": None}, ["ripgrep_backend"]),
        (
            {"content_backend": "local", "ripgrep_backend": "local"},
            ["content_backend", "ripgrep_backend"],
        ),
    ],
)
def test_delegated_with_a_local_file_seam_refuses_to_start(
    overrides: Any, named: Any
) -> None:
    """A served snapshot holds index state only: with files read locally, a
    regex/file/X-Ray read of it would be silently empty on every pod."""
    with pytest.raises(RuntimeError, match="requires") as raised:
        build_index_store(_delegated(**overrides), artifacts=object())
    for field in named:
        assert f"{field}='delegated'" in str(raised.value)
    for field in set(SEAMS_REQUIRED_BY_DELEGATED) - set(named):
        assert field not in str(raised.value)


def test_delegated_with_both_file_seams_delegated_builds() -> None:
    assert SEAMS_REQUIRED_BY_DELEGATED == ("content_backend", "ripgrep_backend")
    store = build_index_store(_delegated(), artifacts=object())
    assert isinstance(store, DelegatedIndexStore)


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

    store = build_configured_index_store(_delegated())

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
