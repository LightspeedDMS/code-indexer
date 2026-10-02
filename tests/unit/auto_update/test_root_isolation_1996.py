"""Bug #1996: server-home isolation covers EVERY test that imports server code,
not only tests/unit/server.

Server modules fix data-dir paths at import time (e.g. the auto-updater's
launch files from CIDX_DATA_DIR).  The root tests/conftest.py isolates the
server data-dir environment before anything imports them, so this test --
outside tests/unit/server -- must see those paths in the scratch home.
"""

from __future__ import annotations

import os
from pathlib import Path

from code_indexer.server.auto_update import deployment_executor
from tests.fixtures.real_server_home_guard import is_under_real_home


def test_auto_updater_launch_paths_resolve_in_the_isolated_data_dir() -> None:
    data_dir = os.environ.get("CIDX_DATA_DIR", "")
    assert data_dir and not is_under_real_home(data_dir), data_dir
    for path in (
        deployment_executor.LAUNCH_CONFIG_PATH,
        deployment_executor.APPLIED_LAUNCH_CONFIG_PATH,
    ):
        assert not is_under_real_home(path), path
        assert path.parent == Path(data_dir), path
