"""The search seam's own names, and that it stays independent of other seams.

The resolution mechanism is shared (``backend_plugin_loader``, tested there).
What is specific to this module is which env var it reads, which entry-point
group it scans, which error type its callers catch -- and that none of those
bleed into the content or git seams.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from code_indexer.server.services import (
    backend_plugin_loader,
    content_backend_plugin,
    search_backend_plugin,
)
from code_indexer.server.services.search_backend_plugin import (
    ENTRY_POINT_GROUP,
    ENV_VAR,
    SearchBackendPluginError,
    get_search_backend_factory,
    resolve_delegated_search_backend,
)


def _make_backend() -> str:
    return "delegated-search-backend"


class _FakeEntryPoint:
    name = "remote-search"

    def __init__(self, loaded: Any = _make_backend) -> None:
        self._loaded = loaded

    def load(self) -> Any:
        if isinstance(self._loaded, Exception):
            raise self._loaded
        return self._loaded


@pytest.fixture(autouse=True)
def _clear(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv(ENV_VAR, raising=False)
    # Deterministic regardless of what is pip-installed in the dev venv.
    monkeypatch.setattr(backend_plugin_loader, "entry_points", lambda **_: [])
    search_backend_plugin.reset_cache()
    content_backend_plugin.reset_cache()
    yield
    search_backend_plugin.reset_cache()
    content_backend_plugin.reset_cache()


# -- this seam's names --------------------------------------------------------


def test_the_seam_uses_its_own_env_var_and_group() -> None:
    assert ENV_VAR == "CIDX_SEARCH_BACKEND"
    assert ENTRY_POINT_GROUP == "code_indexer.search_backend"


def test_the_group_matches_what_the_plugin_publishes() -> None:
    """The one string that must agree with an out-of-tree package.

    Published by the delegated backend plugin's pyproject. A typo on either side
    is not an error anywhere -- the group simply never matches, the seam reports
    "no plugin configured", and the server quietly keeps reading local indexes.
    """
    assert ENTRY_POINT_GROUP == "code_indexer.search_backend"


def test_no_plugin_configured_returns_none() -> None:
    assert get_search_backend_factory() is None
    assert resolve_delegated_search_backend() is None


# -- resolution ----------------------------------------------------------------


def test_the_env_var_resolves_and_instantiates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        backend_plugin_loader, "_search_factory", _make_backend, raising=False
    )
    monkeypatch.setenv(
        ENV_VAR,
        "code_indexer.server.services.backend_plugin_loader:_search_factory",
    )
    search_backend_plugin.reset_cache()
    assert get_search_backend_factory() is _make_backend
    assert resolve_delegated_search_backend() == "delegated-search-backend"


def test_an_entry_point_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        backend_plugin_loader, "entry_points", lambda **_: [_FakeEntryPoint()]
    )
    search_backend_plugin.reset_cache()
    assert get_search_backend_factory() is _make_backend


def test_a_broken_plugin_raises_this_seams_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers catch SearchBackendPluginError; other seams' errors would escape."""
    monkeypatch.setenv(ENV_VAR, "not_a_valid_spec")
    search_backend_plugin.reset_cache()
    with pytest.raises(SearchBackendPluginError):
        get_search_backend_factory()


def test_a_broken_plugin_never_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """None would read as "no plugin" and silently select local indexes."""
    monkeypatch.setattr(
        backend_plugin_loader,
        "entry_points",
        lambda **_: [_FakeEntryPoint(loaded=ImportError("boom"))],
    )
    search_backend_plugin.reset_cache()
    with pytest.raises(SearchBackendPluginError):
        get_search_backend_factory()


# -- independence from other seams ---------------------------------------------


def test_this_seam_scans_only_its_own_group(monkeypatch: pytest.MonkeyPatch) -> None:
    groups: List[str] = []

    def fake(**kwargs: Any) -> List[Any]:
        groups.append(kwargs.get("group", ""))
        return []

    monkeypatch.setattr(backend_plugin_loader, "entry_points", fake)
    search_backend_plugin.reset_cache()
    get_search_backend_factory()
    assert groups == ["code_indexer.search_backend"], (
        "the search seam scanned a group that is not its own"
    )


def test_resetting_this_seam_does_not_disturb_the_content_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """They share the resolver CLASS, and must not share its cache."""
    monkeypatch.setattr(
        backend_plugin_loader, "entry_points", lambda **_: [_FakeEntryPoint()]
    )
    search_backend_plugin.reset_cache()
    content_backend_plugin.reset_cache()

    assert get_search_backend_factory() is _make_backend
    assert content_backend_plugin.get_content_backend_factory() is _make_backend

    search_backend_plugin.reset_cache()

    # The content seam must still hold its resolved factory without rescanning.
    def explode(**_: Any) -> List[Any]:
        raise AssertionError("the content seam rescanned after the search seam reset")

    monkeypatch.setattr(backend_plugin_loader, "entry_points", explode)
    assert content_backend_plugin.get_content_backend_factory() is _make_backend


def test_the_two_seams_read_different_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting one must not configure the other."""
    monkeypatch.setattr(
        backend_plugin_loader, "_search_factory", _make_backend, raising=False
    )
    monkeypatch.setenv(
        ENV_VAR,
        "code_indexer.server.services.backend_plugin_loader:_search_factory",
    )
    monkeypatch.delenv(content_backend_plugin.ENV_VAR, raising=False)
    search_backend_plugin.reset_cache()
    content_backend_plugin.reset_cache()

    assert get_search_backend_factory() is _make_backend
    assert content_backend_plugin.get_content_backend_factory() is None, (
        "CIDX_SEARCH_BACKEND configured the content seam too"
    )
