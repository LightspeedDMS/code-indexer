"""The analysis agents' credential is created once across the cluster.

Every pod runs ``migrate_at_startup`` at the same time after a rollout. Each
used to find no stored credential, mint its own and store it, leaving N live
``cidx-analysis-agent`` credentials (only the last one recorded). The
get-or-create is now single-flight (a PostgreSQL advisory lock in cluster
mode) and the store is insert-if-unchanged, so a pod that loses the race
adopts the winner's credential and revokes the one it minted.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, List, Optional, Tuple
from unittest.mock import patch

import pytest

from code_indexer.server.services.analysis_agent_principal import (
    ANALYSIS_AGENT_USERNAME,
    ensure_analysis_agent_principal,
)
from code_indexer.server.services.mcp_self_registration_service import (
    CREDENTIAL_LOCK_KEY,
    MCPSelfRegistrationService,
)
from tests.unit.server.services.test_analysis_agent_principal import _World


def _live_agent_credentials(world: _World) -> List[Any]:
    return list(world.credentials.get_credentials(ANALYSIS_AGENT_USERNAME))


def _peer(world: _World) -> MCPSelfRegistrationService:
    """A second pod: its own service (and locks) over the shared stores."""
    peer = MCPSelfRegistrationService(
        config_manager=world.config, mcp_credential_manager=world.credentials
    )
    peer.set_group_manager(world.groups)
    return peer


def test_concurrent_pods_end_with_one_live_credential(tmp_path: Path) -> None:
    world = _World(tmp_path)
    ensure_analysis_agent_principal(world.users, world.groups)
    pods = [world.service, _peer(world)]
    both_minted = threading.Barrier(len(pods), timeout=10)
    real_generate = world.credentials.generate_credential_audited

    def generate_then_wait(*args: Any, **kwargs: Any) -> Any:
        cred = real_generate(*args, **kwargs)
        both_minted.wait()  # neither pod stores before both have minted
        return cred

    results: List[Optional[dict]] = []

    def run(pod: MCPSelfRegistrationService) -> None:
        results.append(pod.get_or_create_credentials())

    with patch.object(
        world.credentials,
        "generate_credential_audited",
        side_effect=generate_then_wait,
    ):
        threads = [threading.Thread(target=run, args=(pod,)) for pod in pods]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

    assert len(results) == 2 and all(r is not None for r in results)
    assert results[0] == results[1]
    assert len(_live_agent_credentials(world)) == 1
    stored = world.config.get_config().mcp_self_registration
    assert stored is not None
    assert results[0] is not None and results[0]["client_id"] == stored.client_id
    assert world.credentials.verify_credential(
        stored.client_id, stored.client_secret
    ) == (ANALYSIS_AGENT_USERNAME)


def test_get_or_create_is_idempotent(tmp_path: Path) -> None:
    world = _World(tmp_path)
    first = world.service.get_or_create_credentials()
    again = _peer(world).get_or_create_credentials()
    assert first is not None and first == again
    assert len(_live_agent_credentials(world)) == 1


class _Conn:
    def __init__(self, log: List[Tuple[str, Tuple[Any, ...]]]) -> None:
        self._log = log

    def execute(self, sql: str, params: Tuple[Any, ...]) -> None:
        self._log.append((sql, params))


class _Pool:
    def __init__(self) -> None:
        self.log: List[Tuple[str, Tuple[Any, ...]]] = []

    @contextmanager
    def connection(self) -> Iterator[_Conn]:
        yield _Conn(self.log)


def test_cluster_mode_holds_the_advisory_lock_around_the_get_or_create(
    tmp_path: Path,
) -> None:
    world = _World(tmp_path)
    pool = _Pool()
    world.service.set_connection_pool(pool)
    seen_inside: List[int] = []
    real = world.service._get_or_create_credentials

    def inside() -> Optional[dict]:
        seen_inside.append(len(pool.log))
        return real()

    with patch.object(world.service, "_get_or_create_credentials", side_effect=inside):
        assert world.service.get_or_create_credentials() is not None
    assert seen_inside == [1]
    assert [sql.split("(")[0] for sql, _ in pool.log] == [
        "SELECT pg_advisory_lock",
        "SELECT pg_advisory_unlock",
    ]
    assert all(params == (CREDENTIAL_LOCK_KEY,) for _, params in pool.log)


def test_the_advisory_lock_is_released_when_the_get_or_create_raises(
    tmp_path: Path,
) -> None:
    world = _World(tmp_path)
    pool = _Pool()
    world.service.set_connection_pool(pool)
    with patch.object(
        world.service, "_get_or_create_credentials", side_effect=RuntimeError("x")
    ):
        with pytest.raises(RuntimeError):
            world.service.get_or_create_credentials()
    assert pool.log[-1][0].startswith("SELECT pg_advisory_unlock")
