"""Build the server app singleton during pytest COLLECTION, not in a test.

``code_indexer.server.app.app`` is a lazy singleton (Bug #1638): its first
access runs the whole ``create_app()`` (DB schema, migrations, admin seeding:
~3.5 s, far more under gate load).  The server lanes run every test under a
15 s pytest-timeout, so whichever test first touches the singleton pays that
cost inside its own budget.  A real-app test file that calls
:func:`build_server_app_at_collection` at MODULE level moves the build to
collection time instead -- do not move those calls back into test bodies or
fixtures.

The build never reads or writes the real ``~/.cidx-server``: the data dir is
the lane's ``CIDX_SERVER_DATA_DIR`` (else a throwaway one under ``~/.tmp``),
``Path.home()`` points at a throwaway home (the HNSW/FTS cache config loaders
read ``Path.home()/.cidx-server/config.json``), and the import-frozen
launch.json paths ``create_app()`` writes through are redirected there too.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch


def build_server_app_at_collection() -> Any:
    """The server app singleton, built now (see the module docstring)."""
    from code_indexer.server.auto_update import deployment_executor
    from code_indexer.server.services import config_service

    local_tmp = Path.home() / ".tmp"
    local_tmp.mkdir(exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="cidx-collect-app-", dir=str(local_tmp)))
    atexit.register(shutil.rmtree, str(scratch), True)
    scratch_server_home = scratch / ".cidx-server"
    scratch_server_home.mkdir()
    data_dir = os.environ.get("CIDX_SERVER_DATA_DIR") or str(scratch_server_home)
    with ExitStack() as stack:
        stack.enter_context(
            patch.dict("os.environ", {"CIDX_SERVER_DATA_DIR": data_dir})
        )
        stack.enter_context(patch.object(Path, "home", lambda: scratch))
        for module in (deployment_executor, config_service):
            stack.enter_context(
                patch.object(
                    module, "LAUNCH_CONFIG_PATH", scratch_server_home / "launch.json"
                )
            )
            stack.enter_context(
                patch.object(
                    module,
                    "APPLIED_LAUNCH_CONFIG_PATH",
                    scratch_server_home / "applied_launch.json",
                )
            )
        config_service.reset_config_service()
        from code_indexer.server.app import app

    return app
