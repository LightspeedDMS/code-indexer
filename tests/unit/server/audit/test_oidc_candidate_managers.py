"""OIDC managers prepared for a candidate configuration.

The state manager for a candidate OIDC configuration is built the way
startup builds it: on a worker thread (never on the event loop -- it opens
its SQLite store and ensures its schema), and, in cluster mode, wired to
the SAME shared PostgreSQL pool (critical pool first, else the general
pool) so SSO state is shared across nodes.

The OIDC manager's own initialization is replaced by a stand-in here (it is
not the unit under test); the StateManager is the real class, observed
through a recording subclass.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import anyio
import pytest

from code_indexer.server.utils.config_manager import OIDCProviderConfig, ServerConfig


class _Pool:
    """Stands in for the cluster's shared connection pool (never queried)."""


@pytest.fixture()
def recorded(tmp_path: Path, monkeypatch) -> List[dict]:
    from code_indexer.server.auth.oidc import state_manager as state_module
    from code_indexer.server.web import routes as web_routes

    monkeypatch.setattr(
        state_module, "_configured_sqlite_path", str(tmp_path / "oidc_state.db")
    )
    constructions: List[dict] = []

    class _RecordingStateManager(state_module.StateManager):
        def __init__(self) -> None:
            try:
                asyncio.get_running_loop()
                on_loop = True
            except RuntimeError:
                on_loop = False
            constructions.append(
                {"on_loop": on_loop, "thread": threading.current_thread().name}
            )
            super().__init__()

    monkeypatch.setattr(state_module, "StateManager", _RecordingStateManager)

    async def _initialized(oidc_config) -> Any:
        return SimpleNamespace(config=oidc_config)

    monkeypatch.setattr(web_routes, "_initialized_oidc_manager", _initialized)
    return constructions


def _candidate(enabled: bool) -> ServerConfig:
    return ServerConfig(
        server_dir="/nonexistent",
        oidc_provider_config=OIDCProviderConfig(enabled=enabled),
    )


def _prepare(candidate: ServerConfig) -> dict:
    """Run the before_publish step the way the Web route does (worker thread)."""
    from code_indexer.server.web import routes as web_routes

    prepared: dict = {}

    async def _main() -> None:
        await anyio.to_thread.run_sync(
            web_routes._prepare_oidc_candidate_from_worker_thread, candidate, prepared
        )

    anyio.run(_main)
    return prepared


def _set_registry(monkeypatch, **pools: Any) -> None:
    from code_indexer.server import app as app_module

    registry = SimpleNamespace(
        critical_connection_pool=pools.get("critical"),
        connection_pool=pools.get("general"),
    )
    monkeypatch.setattr(
        app_module.app.state, "backend_registry", registry, raising=False
    )


def test_state_manager_is_built_off_the_event_loop(recorded, monkeypatch) -> None:
    _set_registry(monkeypatch)
    prepared = _prepare(_candidate(enabled=True))
    assert len(recorded) == 1
    assert recorded[0]["on_loop"] is False
    oidc_manager, state_manager = prepared["managers"]
    assert state_manager is not None and oidc_manager is not None


def test_cluster_state_manager_uses_the_critical_pool(recorded, monkeypatch) -> None:
    critical, general = _Pool(), _Pool()
    _set_registry(monkeypatch, critical=critical, general=general)
    _, state_manager = _prepare(_candidate(enabled=True))["managers"]
    assert state_manager._pool is critical


def test_cluster_state_manager_falls_back_to_the_general_pool(
    recorded, monkeypatch
) -> None:
    general = _Pool()
    _set_registry(monkeypatch, general=general)
    _, state_manager = _prepare(_candidate(enabled=True))["managers"]
    assert state_manager._pool is general


def test_solo_state_manager_keeps_its_sqlite_store(recorded, monkeypatch) -> None:
    _set_registry(monkeypatch)
    _, state_manager = _prepare(_candidate(enabled=True))["managers"]
    assert state_manager._pool is None


def test_disabled_candidate_builds_no_managers(recorded, monkeypatch) -> None:
    _set_registry(monkeypatch, general=_Pool())
    assert _prepare(_candidate(enabled=False))["managers"] == (None, None)
    assert recorded == []
