"""Story #2118: ONE shared runtime node-id resolver.

Promoted from search_embed_event_emit._resolve_node_id; the search-event
writers (mcp/handlers/search/code_search.py, routers/inline_query.py) and the
index-spend attribution all use it. Precedence: the runtime config's
``node_id``, else ``socket.gethostname()``.
"""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

from code_indexer.server.services import config_service
from code_indexer.server.utils.runtime_node_id import resolve_runtime_node_id


class _ConfigService:
    def __init__(self, node_id: str) -> None:
        self._config = SimpleNamespace(node_id=node_id)

    def get_config(self) -> SimpleNamespace:
        return self._config


def test_configured_node_id_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config_service, "get_config_service", lambda: _ConfigService("node-a")
    )
    assert resolve_runtime_node_id() == "node-a"


def test_blank_node_id_falls_back_to_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config_service, "get_config_service", lambda: _ConfigService("")
    )
    assert resolve_runtime_node_id() == socket.gethostname()


def test_config_lookup_failure_falls_back_to_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _broken() -> None:
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(config_service, "get_config_service", _broken)
    assert resolve_runtime_node_id() == socket.gethostname()
