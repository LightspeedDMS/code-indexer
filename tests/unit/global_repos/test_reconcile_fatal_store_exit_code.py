"""A reconcile (``cidx index --reconcile``) reports a fatal chunk-store
failure with the same reserved exit code as the incremental: its
store-derived plan reads the store first, and a disk I/O fault or an
unreadable store there exits 87 (environment) and a corrupt store 86
(corruption) -- never the generic 1, which the refresh cannot classify, back
off or self-heal.

Each test runs a REAL ``cidx index`` child with the exact server-context
arguments the refresh scheduler uses (plus ``--reconcile``, which a local
refresh passes when a file was added or changed or the baseline is
unknown), against a REAL CHUNKS_DB collection.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from code_indexer.server.services.embedding_stats_attribution import (
    StatsAttribution,
)
from code_indexer.server.utils.index_command_layout import append_server_layout_args
from code_indexer.services.index_failure_exit_codes import (
    EXIT_CODE_CHUNK_STORE_CORRUPTION,
    EXIT_CODE_CHUNK_STORE_ENVIRONMENT,
)
from tests.utils.fatal_chunk_store_fixtures import (
    CHILD_TIMEOUT_SECONDS,
    child_env,
    cidx_child_command,
    corrupt_btree_pages,
    make_chunks_db_repo,
    make_journal_path_a_directory,
)

pytestmark = pytest.mark.slow


def _reconcile_child(repo: Path) -> subprocess.CompletedProcess:
    args = append_server_layout_args(
        ["cidx", "index", "--fts", "--progress-json", "--reconcile"],
        attribution=StatsAttribution(
            golden_repo_alias=f"{repo.name}-global",
            job_id="nojob:test",
            node_id="node-a",
            purpose="refresh",
        ),
    )
    return subprocess.run(
        cidx_child_command(args[1:]),
        cwd=repo,
        env=child_env(),
        capture_output=True,
        text=True,
        timeout=CHILD_TIMEOUT_SECONDS,
    )


def _assert_exit(proc: subprocess.CompletedProcess, expected: int) -> None:
    assert "Reconciling" in proc.stdout + proc.stderr, "not a reconcile run"
    assert proc.returncode == expected, (
        f"exit {proc.returncode}, expected {expected}: {proc.stdout[-1500:]}"
        f"{proc.stderr[-1500:]}"
    )


def test_disk_io_fault_in_a_reconcile_exits_environment(tmp_path: Path) -> None:
    repo = tmp_path / "io-fault-repo"
    make_journal_path_a_directory(make_chunks_db_repo(repo, metadata_marker="m"))

    _assert_exit(_reconcile_child(repo), EXIT_CODE_CHUNK_STORE_ENVIRONMENT)


def test_unreadable_store_in_a_reconcile_exits_environment(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores file modes; cannot produce a permission failure")
    repo = tmp_path / "unreadable-repo"
    chunks_db = make_chunks_db_repo(repo, metadata_marker="m")
    chunks_db.chmod(0o000)
    try:
        _assert_exit(_reconcile_child(repo), EXIT_CODE_CHUNK_STORE_ENVIRONMENT)
    finally:
        chunks_db.chmod(0o644)


def test_corrupt_store_in_a_reconcile_exits_corruption(tmp_path: Path) -> None:
    repo = tmp_path / "corrupt-repo"
    corrupt_btree_pages(make_chunks_db_repo(repo, metadata_marker="m"))

    _assert_exit(_reconcile_child(repo), EXIT_CODE_CHUNK_STORE_CORRUPTION)
