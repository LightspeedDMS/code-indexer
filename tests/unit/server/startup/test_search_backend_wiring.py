"""Selecting the search backend, and refusing to guess when delegation breaks.

The no-fallback rule matters for the search seam because a stateless EKS pod
configured for delegation has no local Tantivy/HNSW index files at all. Falling
back to local would surface as "FTS index not available" errors on every query
-- correct error text, wrong root cause, invisible to the operator.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from code_indexer.server.startup.search_backend_wiring import (
    VALID_SEARCH_BACKENDS,
    build_configured_search_backend,
    build_search_backend,
)


class _StubDelegatedBackend:
    """Satisfies the SearchBackend protocol without touching indexes."""

    def search(
        self,
        repository_alias: str,
        query_text: str,
        search_mode: str,
        *,
        limit: int = 50,
        min_score: Optional[float] = None,
        language: Optional[str] = None,
        exclude_language: Optional[str] = None,
        path_filter: Optional[str] = None,
        exclude_path: Optional[str] = None,
        case_sensitive: bool = False,
        fuzzy: bool = False,
        edit_distance: int = 0,
        snippet_lines: int = 5,
        regex: bool = False,
        accuracy: str = "balanced",
        include_source: bool = True,
    ) -> List[Dict[str, Any]]:
        raise AssertionError("not called in these tests")


# -- the default ---------------------------------------------------------------


def test_an_absent_config_field_selects_local() -> None:
    """A partial/bootstrap config must not raise AttributeError."""
    assert build_search_backend(SimpleNamespace()) is None


def test_an_explicit_local_returns_none() -> None:
    assert build_search_backend(SimpleNamespace(search_backend="local")) is None


def test_an_empty_string_falls_back_to_local_not_an_error() -> None:
    """A config round-tripped through JSON can arrive as "" rather than unset."""
    assert build_search_backend(SimpleNamespace(search_backend="")) is None


def test_none_falls_back_to_local() -> None:
    assert build_search_backend(SimpleNamespace(search_backend=None)) is None


# -- delegation ----------------------------------------------------------------


def test_delegated_returns_the_injected_backend() -> None:
    stub = _StubDelegatedBackend()
    backend = build_search_backend(
        SimpleNamespace(search_backend="delegated"), delegated_backend=stub
    )
    assert backend is stub


def test_delegated_without_a_backend_raises() -> None:
    """The whole point. Falling back here would produce index-not-found errors."""
    with pytest.raises(RuntimeError, match="no delegated backend"):
        build_search_backend(SimpleNamespace(search_backend="delegated"))


def test_the_refusal_names_the_remedy_an_operator_can_act_on() -> None:
    """Asserts the WIRING's message, not merely that something raised.

    The entry-point group and the env var are what an operator needs to fix it.
    """
    with pytest.raises(RuntimeError) as excinfo:
        build_search_backend(SimpleNamespace(search_backend="delegated"))
    message = str(excinfo.value)
    assert "code_indexer.search_backend entry point" in message, (
        "the refusal does not name the entry-point group to register"
    )
    assert "CIDX_SEARCH_BACKEND" in message, (
        "the refusal does not name the env var alternative"
    )
    assert "no Tantivy/HNSW index files" in message


# -- typos ---------------------------------------------------------------------


@pytest.mark.parametrize("value", ["Local", "LOCAL", "deleGated", "remote", "mcp"])
def test_an_unrecognised_value_raises_instead_of_defaulting(value: str) -> None:
    """A typo'd config must not degrade into a surprising default.

    Case matters: 'Local' silently treated as local would mean a config that
    reads as deliberate is doing something nobody chose.
    """
    with pytest.raises(RuntimeError, match="Unknown search_backend"):
        build_search_backend(SimpleNamespace(search_backend=value))


def test_the_valid_set_is_exactly_local_and_delegated() -> None:
    assert VALID_SEARCH_BACKENDS == ("local", "delegated")


# -- the configured entry point ------------------------------------------------


def test_the_local_default_never_resolves_the_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing deployments must not pay for, or fail on, a plugin they never set.

    A broken plugin installed in the image would otherwise break every server
    that had not opted into delegation at all.
    """
    from code_indexer.server.services import search_backend_plugin

    def explode() -> Any:
        raise AssertionError("the plugin was resolved on the local path")

    monkeypatch.setattr(
        search_backend_plugin, "resolve_delegated_search_backend", explode
    )
    backend = build_configured_search_backend(
        SimpleNamespace(search_backend="local")
    )
    assert backend is None


def test_delegated_resolves_through_the_plugin_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_indexer.server.services import search_backend_plugin

    stub = _StubDelegatedBackend()
    monkeypatch.setattr(
        search_backend_plugin, "resolve_delegated_search_backend", lambda: stub
    )
    backend = build_configured_search_backend(
        SimpleNamespace(search_backend="delegated")
    )
    assert backend is stub


def test_delegated_with_no_plugin_installed_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam returning None means 'nothing installed', not 'use local'."""
    from code_indexer.server.services import search_backend_plugin

    monkeypatch.setattr(
        search_backend_plugin, "resolve_delegated_search_backend", lambda: None
    )
    with pytest.raises(RuntimeError, match="no delegated backend"):
        build_configured_search_backend(
            SimpleNamespace(search_backend="delegated")
        )


# -- the config field itself ---------------------------------------------------


def test_server_config_declares_search_backend_defaulting_to_local() -> None:
    """Without the field an operator has no way to select delegation at all."""
    from code_indexer.server.utils.config_manager import ServerConfig

    import dataclasses

    fields = {f.name: f for f in dataclasses.fields(ServerConfig)}
    assert "search_backend" in fields, (
        "ServerConfig has no search_backend field, so the seam cannot be selected"
    )
    assert fields["search_backend"].default == "local"


def test_search_backend_is_a_bootstrap_key() -> None:
    """It is read before the DB exists, like clone/git/content/ripgrep.

    Left unclassified, config.json is stripped on first boot and the setting
    reverts to local -- search would silently go back to reading index files
    the pod may not have.
    """
    from code_indexer.server.services.config_service import BOOTSTRAP_KEYS

    assert "search_backend" in BOOTSTRAP_KEYS


# -- this seam is its own ------------------------------------------------------


def test_the_plugin_seam_uses_its_own_names() -> None:
    """Sharing the resolver class must not mean sharing a group or an env var."""
    from code_indexer.server.services import search_backend_plugin as sb

    assert sb.ENV_VAR == "CIDX_SEARCH_BACKEND"
    assert sb.ENTRY_POINT_GROUP == "code_indexer.search_backend"
