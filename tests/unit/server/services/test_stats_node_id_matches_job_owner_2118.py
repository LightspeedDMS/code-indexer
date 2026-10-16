"""Story #2118: embedding_call_stats node ids join to job ownership.

A job row's ``executing_node`` is the JobTracker's node id, which
startup/service_init.py resolves as
``resolve_cluster_node_id(<parsed config.json>)``: ``cluster.node_id`` when
set, else ``<hostname>-cidx``. The node id an attributed stats row records
must be that SAME identity, so slice 1b can join stats rows to jobs/nodes.

Both sides read one real config.json in a scratch server dir: the job side
exactly as service_init does, the stats side through the real
ServerConfigManager load the config service performs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from code_indexer.server.services import config_service
from code_indexer.server.services.embedding_stats_attribution import (
    build_server_stats_attribution,
)
from code_indexer.server.utils.cluster_node_id import resolve_cluster_node_id
from code_indexer.server.utils.config_manager import ServerConfig, ServerConfigManager


class _ConfigService:
    def __init__(self, config: ServerConfig) -> None:
        self._config = config

    def get_config(self) -> ServerConfig:
        return self._config


@pytest.mark.parametrize("cluster_node_id", ["node-7", None])
def test_stats_node_id_equals_the_job_owner_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cluster_node_id
) -> None:
    raw: Dict[str, Any] = {"server_dir": str(tmp_path), "storage_mode": "sqlite"}
    if cluster_node_id is not None:
        raw["cluster"] = {"node_id": cluster_node_id}
    (tmp_path / "config.json").write_text(json.dumps(raw))
    loaded = ServerConfigManager(server_dir_path=str(tmp_path)).load_config()
    assert loaded is not None
    monkeypatch.setattr(
        config_service, "get_config_service", lambda: _ConfigService(loaded)
    )

    job_owner = resolve_cluster_node_id(
        json.loads((tmp_path / "config.json").read_text())
    )
    stats_node = build_server_stats_attribution(
        alias="example-repo-global", job_id=None, site="test", purpose="index"
    ).node_id

    assert stats_node == job_owner
