"""Bug #1996: ONE launch.json path -- the writer (ConfigService) and its reader
(the auto-updater) must agree.

``ConfigService.materialize_launch_config`` writes exactly
``deployment_executor.LAUNCH_CONFIG_PATH`` (derived from ``CIDX_DATA_DIR``),
the file the auto-updater reads before restarting the server -- never a path
derived from the service's own ``server_dir``.  Tests keep that path out of
the developer's real ~/.cidx-server through environment isolation
(``tests/_isolated_server_home.py``, imported first by the root conftest, sets
``CIDX_DATA_DIR`` before any server import), not through a second path
authority.

Real ConfigService, real SQLite runtime DB, real files -- no mocks.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator, Optional

import pytest

from code_indexer.server.auto_update import deployment_executor
from code_indexer.server.services import config_service as cs_mod
from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.utils.config_manager import ServerConfigManager
from tests.fixtures.real_server_home_guard import is_under_real_home


@pytest.fixture
def preserved_launch_file() -> Iterator[Path]:
    """The shared launch path, restored to its prior state after the test."""
    path = deployment_executor.LAUNCH_CONFIG_PATH
    assert not is_under_real_home(path), f"launch path not isolated: {path}"
    prior: Optional[bytes] = path.read_bytes() if path.exists() else None
    yield path
    if prior is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(prior)


def _service(server_dir: Path) -> ConfigService:
    server_dir.mkdir(parents=True)
    (server_dir / "config.json").write_text(
        json.dumps({"host": "127.0.0.1", "port": 8123, "workers": 3})
    )
    svc = ConfigService(config_manager=ServerConfigManager(str(server_dir)))
    db_path = str(server_dir / "cidx_server.db")
    DatabaseSchema(db_path).initialize_database()
    svc.initialize_runtime_db(db_path)
    return svc


def test_launch_paths_resolve_inside_the_isolated_data_dir() -> None:
    data_dir = Path(os.environ["CIDX_DATA_DIR"])
    for path in (
        deployment_executor.LAUNCH_CONFIG_PATH,
        deployment_executor.APPLIED_LAUNCH_CONFIG_PATH,
        cs_mod.LAUNCH_CONFIG_PATH,
    ):
        assert not is_under_real_home(path), path
        assert path.parent == data_dir, path


def test_materialize_writes_the_path_the_auto_updater_reads(
    tmp_path: Path, preserved_launch_file: Path
) -> None:
    server_dir = tmp_path / "other-server-dir"
    assert server_dir != preserved_launch_file.parent
    svc = _service(server_dir)
    preserved_launch_file.unlink(missing_ok=True)

    assert svc.materialize_launch_config() is True

    payload = json.loads(preserved_launch_file.read_text())
    assert payload["port"] == 8123
    assert payload["workers"] == 3
    assert not (server_dir / "launch.json").exists(), (
        "launch.json must have ONE path: the one the auto-updater reads"
    )
