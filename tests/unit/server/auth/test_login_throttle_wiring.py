"""Solo-mode start-up wires the login throttle to the shared SQLite DB.

Every uvicorn worker of a node runs ``initialize_services()``; the throttle
state must land in the node's shared ``data/cidx_server.db`` so all workers
see it, never in per-process memory.  The real start-up runs in a child
process whose server data directory (and HOME) is a temp dir, so the real
``~/.cidx-server`` is never touched and this process's singletons stay
untouched.
"""

from __future__ import annotations

import os
import site
import sqlite3
import subprocess
import sys
from pathlib import Path

_CHILD = """
import os
from code_indexer.server.startup.service_init import initialize_services
from code_indexer.server.auth.login_rate_limiter import login_rate_limiter

initialize_services()
login_rate_limiter.begin_attempt("wiring-probe")
print("RECORDED", flush=True)
os._exit(0)
"""


def test_service_init_wires_the_throttle_to_the_shared_sqlite_db(tmp_path: Path):
    data_dir = tmp_path / "server"
    home = tmp_path / "home"
    home.mkdir()
    src = Path(__file__).resolve().parents[4] / "src"
    env = dict(os.environ)
    env.update(
        CIDX_SERVER_DATA_DIR=str(data_dir),
        HOME=str(home),
        PYTHONPATH=str(src),
        # An isolated HOME must not hide user-installed dependencies.
        PYTHONUSERBASE=site.getuserbase(),
    )
    result = subprocess.run(
        [sys.executable, "-c", _CHILD],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert "RECORDED" in result.stdout, result.stderr[-4000:]

    conn = sqlite3.connect(str(data_dir / "data" / "cidx_server.db"))
    try:
        rows = conn.execute("SELECT failure_count FROM login_throttle").fetchall()
    finally:
        conn.close()
    assert rows == [(1,)]
