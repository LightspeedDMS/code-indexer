"""Bug #2022 Gap 4: the typed fatal chunk-store failure must survive the
``cidx index`` child-process boundary.

Every test here runs a REAL ``cidx index`` child with the exact
server-context arguments the refresh scheduler uses, against a REAL
CHUNKS_DB collection. Before the fix the child exited 1 for every failure,
so the server could only tell "corrupt store" apart from "anything else" by
parsing free-form stderr -- which it never did, so the integrity-gate
self-heal never ran.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Iterator
from unittest.mock import Mock

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.utils.index_command_layout import append_server_layout_args
from tests.utils.fatal_chunk_store_fixtures import (
    CHILD_TIMEOUT_SECONDS,
    DUMMY_VOYAGE_KEY,
    cidx_child_command,
    corrupt_btree_pages,
    install_cidx_shim,
    make_chunks_db_repo,
    quick_check_ok,
    run_server_index_child,
)
from tests.utils.refresh_fatal_store_harness import typed_kinds

GENERIC_FAILURE_EXIT_CODE = 1


@pytest.fixture
def corrupt_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "corrupt-repo"
    corrupt_btree_pages(make_chunks_db_repo(repo, metadata_marker="m"))
    return repo


@pytest.fixture
def unreadable_repo(tmp_path: Path) -> Iterator[Path]:
    repo = tmp_path / "unreadable-repo"
    chunks_db = make_chunks_db_repo(repo, metadata_marker="m")
    chunks_db.chmod(0o000)
    try:
        yield repo
    finally:
        chunks_db.chmod(0o644)


def _normalised(text: str) -> str:
    """Collapse whitespace: Rich wraps long stderr lines, possibly inside the
    phrase a test looks for."""
    return " ".join(text.split())


def test_corrupt_store_child_exit_code_is_not_the_generic_failure_code(
    corrupt_repo: Path,
) -> None:
    proc = run_server_index_child(corrupt_repo)

    assert "malformed" in _normalised(proc.stderr), proc.stderr
    assert proc.returncode not in (0, GENERIC_FAILURE_EXIT_CODE), (
        "a corrupt chunks.db must be reported with a machine-readable exit "
        f"code, got {proc.returncode}"
    )


def test_unreadable_store_exit_code_differs_from_corruption_code(
    corrupt_repo: Path, unreadable_repo: Path
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores file modes; cannot produce a permission failure")
    corrupt_proc = run_server_index_child(corrupt_repo)
    unreadable_proc = run_server_index_child(unreadable_repo)

    assert "unable to open database file" in _normalised(unreadable_proc.stderr), (
        unreadable_proc.stderr
    )
    assert unreadable_proc.returncode not in (0, GENERIC_FAILURE_EXIT_CODE)
    assert unreadable_proc.returncode != corrupt_proc.returncode, (
        "a permission/environment failure must never be classified as "
        "corruption (corruption triggers a restore over the store)"
    )


def test_ordinary_failure_keeps_generic_exit_code(tmp_path: Path) -> None:
    """Regression guard: a failure that is not a chunk-store failure (here a
    missing provider key) keeps exit code 1."""
    repo = tmp_path / "ordinary-repo"
    make_chunks_db_repo(repo, metadata_marker="m")
    proc = run_server_index_child_without_key(repo)

    assert proc.returncode == GENERIC_FAILURE_EXIT_CODE, proc.stderr


def run_server_index_child_without_key(repo: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "VOYAGE_API_KEY"}
    args = append_server_layout_args(["cidx", "index", "--fts", "--progress-json"])
    return subprocess.run(
        cidx_child_command(args[1:]),
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=CHILD_TIMEOUT_SECONDS,
    )


def test_index_source_surfaces_typed_corruption_kind(
    tmp_path: Path, corrupt_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refresh scheduler's own child invocation (real ``cidx`` resolved
    from PATH) must surface the failure as a typed error whose ``kind`` is
    corruption -- never a bare RuntimeError built from stderr text."""
    shim_dir = install_cidx_shim(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("VOYAGE_API_KEY", DUMMY_VOYAGE_KEY)
    golden = tmp_path / "golden-repos"
    golden.mkdir()
    registry = Mock()
    registry.get_global_repo.return_value = {
        "alias_name": "corrupt-repo-global",
        "repo_url": "local://corrupt-repo",
        "enable_temporal": False,
        "enable_scip": False,
    }
    config = Mock()
    config.get_global_refresh_interval.return_value = 3600
    metadata = Mock()
    metadata.get_repo.return_value = None
    scheduler = RefreshScheduler(
        golden_repos_dir=str(golden),
        config_source=config,
        query_tracker=Mock(spec=QueryTracker),
        cleanup_manager=Mock(spec=CleanupManager),
        registry=registry,
        golden_repo_metadata_backend=metadata,
    )

    with pytest.raises(Exception) as raised:
        scheduler._index_source(
            alias_name="corrupt-repo-global", source_path=str(corrupt_repo)
        )

    assert "corruption" in typed_kinds(raised.value), (
        f"no typed corruption kind in the raised chain: {raised.value!r}"
    )
    # _index_source alone never repairs: the store is still corrupt.
    assert not quick_check_ok(
        corrupt_repo / ".code-indexer" / "index" / "voyage-code-3" / "chunks.db"
    )
