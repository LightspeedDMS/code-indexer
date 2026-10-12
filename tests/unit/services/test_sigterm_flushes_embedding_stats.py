"""A SIGTERMed ``cidx index`` child persists the embedding-call stats of the
provider calls it already made.

A cancelled refresh terminates its indexing child with SIGTERM
(``terminate_process_group``: SIGTERM, grace period, SIGKILL). The child's
stats writer buffers rows and flushes every 30 s or at exit, so a SIGTERM
that kills the interpreter without a flush loses exactly the spend the
refresh volume alarm (``index_spend_alarm``) must count.

Drives the REAL ``cidx`` CLI (subprocess, the tree under test) against the
local fake VoyageAI server under the network guard of
tests/utils/cidx_fake_provider_repo.py, with the server-side stats bootstrap
(``CIDX_EMBEDDING_STATS_BOOTSTRAP_DIR``, SQLite) the server gives its
indexing children.
"""

from __future__ import annotations

import json
import signal
import sqlite3
import time
from pathlib import Path
from typing import Iterator, Tuple

import pytest

from code_indexer.utils.process_group import terminate_process_group
from tests.utils.cidx_fake_provider_repo import FakeProviderRepo
from tests.utils.fake_voyage_provider import running_fake_voyage_server

_SENTINEL_KEY = "sigterm-stats-fake-key"
_BOOTSTRAP_ENV = "CIDX_EMBEDDING_STATS_BOOTSTRAP_DIR"

pytestmark = pytest.mark.timeout(240)


@pytest.fixture
def fake_provider() -> Iterator[object]:
    with running_fake_voyage_server(_SENTINEL_KEY) as server:
        yield server


def _server_dir_with_sqlite_stats(root: Path) -> Path:
    """A server data directory as the server hands it to an indexing child."""
    server_dir = root / "cidx-server"
    server_dir.mkdir()
    (server_dir / "config.json").write_text(
        json.dumps({"server_dir": str(server_dir), "storage_mode": "sqlite"})
    )
    return server_dir


def _persisted_stats(server_dir: Path) -> Tuple[int, int]:
    """(row count, sum of item_count) of the child's embedding_call_stats."""
    db_path = server_dir / "data" / "cidx_server.db"
    assert db_path.exists(), "the child never opened the stats database"
    conn = sqlite3.connect(str(db_path))
    try:
        rows, items = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(item_count), 0) FROM embedding_call_stats"
        ).fetchone()
        return int(rows), int(items)
    finally:
        conn.close()


def test_sigterm_mid_run_persists_stats_for_completed_calls(
    tmp_path: Path, fake_provider
) -> None:
    """SIGTERM (as a job cancel sends it) after some answered provider calls
    and while later calls are in flight: every answered input has a stats
    row, and the child still dies by SIGTERM (exit -15)."""
    server_dir = _server_dir_with_sqlite_stats(tmp_path)
    repo = FakeProviderRepo(tmp_path, fake_provider, _SENTINEL_KEY)
    repo.env[_BOOTSTRAP_ENV] = str(server_dir)
    repo.write_files("cancelled", 80)

    ledger = fake_provider.ledger
    ledger.begin_run("answered")
    started = time.monotonic()
    proc = repo.spawn_index()
    try:
        assert ledger.wait_for_run_inputs(10, timeout=120), (
            "the child never sent embedding requests"
        )
        # Every later request is held in flight, as a slow provider would.
        ledger.begin_run("in-flight", hold_seconds=120)
        assert ledger.wait_settled("answered", timeout=30)
        assert ledger.wait_for_held(1, timeout=60), (
            "the child finished before it could be cancelled"
        )
        # The answered responses reach the child's recorder (client side).
        time.sleep(1.0)
        assert proc.poll() is None, "the child exited before the cancel"
        # The 30 s periodic flush must not be what persists the rows.
        assert time.monotonic() - started < 25, "run too slow to discriminate"
        terminate_process_group(proc)
    finally:
        if proc.poll() is None:
            terminate_process_group(proc, grace_seconds=0.1)

    assert proc.returncode == -signal.SIGTERM, (
        f"the child must still die by SIGTERM, got exit {proc.returncode}"
    )
    answered_inputs = ledger.run_stats("answered")["inputs"]
    rows, items = _persisted_stats(server_dir)
    assert rows > 0 and items == answered_inputs, (
        f"stats rows={rows} items={items}; the provider answered "
        f"{answered_inputs} inputs before SIGTERM"
    )
