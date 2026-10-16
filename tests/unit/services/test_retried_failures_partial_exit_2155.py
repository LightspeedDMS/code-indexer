"""Bug #2155: a ``cidx index`` run that processed no file and whose ONLY
failures are files a previous run already recorded as failed (re-planned by
``_merge_recorded_failures``), on a collection that already holds committed
points, is a partial outcome: exit 0, the failures stay recorded (so the
server publishes, reports the job partial and escalates its owed retry), and
the console never claims the index is empty. Any newly failing file, or an
empty store, keeps the Bug #1218 fail-loud exit 1.

Every test drives the REAL ``cidx`` CLI (subprocess, the tree under test) in
a temporary non-git repository against the local fake VoyageAI server
(plain HTTP on 127.0.0.1, sentinel key); a failing provider is an unroutable
loopback endpoint with retries off, so no real provider call is possible.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Iterator, List

import pytest

from tests.utils.cidx_fake_provider_repo import FakeProviderRepo
from tests.utils.fake_voyage_provider import running_fake_voyage_server

_SENTINEL_KEY = "issue-2155-fake-key"
_DEAD_ENDPOINT = "http://127.0.0.1:9/v1/embeddings"
_OWED = ["owed_000.py", "owed_001.py"]

pytestmark = pytest.mark.timeout(240)


@pytest.fixture
def fake_provider() -> Iterator[object]:
    # The shared helper stops the fake and asserts no ledger violation.
    with running_fake_voyage_server(_SENTINEL_KEY) as server:
        yield server


def _point_provider_at(repo: FakeProviderRepo, endpoint: str) -> None:
    config_path = repo.repo / ".code-indexer" / "config.json"
    config = json.loads(config_path.read_text())
    config["voyage_ai"]["api_endpoint"] = endpoint
    config["voyage_ai"]["max_retries"] = 0
    config_path.write_text(json.dumps(config))


def _age(paths: List[Path]) -> None:
    """Outside the incremental run's safety buffer: never re-planned by
    mtime, so a later run plans only what the test intends."""
    an_hour_ago = time.time() - 3600
    for path in paths:
        os.utime(path, (an_hour_ago, an_hour_ago))


def _recorded_failures(repo: FakeProviderRepo) -> List[str]:
    failed = repo.metadata()["failed_file_paths"]
    assert isinstance(failed, list)
    return sorted(Path(p).name for p in failed)


def _repo_owing_recorded_failures(tmp_path: Path, server) -> FakeProviderRepo:
    """A populated collection plus two files a previous run failed to index
    (recorded in the metadata); the provider is then down."""
    repo = FakeProviderRepo(tmp_path, server, _SENTINEL_KEY)
    repo.write_files("base", 4)
    _age(list((repo.repo / "src").glob("base_*.py")))
    first = repo.cidx(*repo.index_args())
    assert first.returncode == 0, first.stderr[-3000:]
    assert repo.committed_row_count() > 0

    repo.write_files("owed", len(_OWED))
    _point_provider_at(repo, _DEAD_ENDPOINT)
    owing = repo.cidx(*repo.index_args())
    # New files that all fail: the Bug #1218 fail-loud exit, unchanged.
    assert owing.returncode == 1, (owing.returncode, owing.stdout[-2000:])
    assert _recorded_failures(repo) == _OWED
    return repo


class _Store:
    """A vector store whose read-only population probe answers or raises."""

    def __init__(self, populated: bool = True, error: bool = False) -> None:
        self.populated = populated
        self.error = error

    def has_committed_points(self, collection_name: str) -> bool:
        if self.error:
            raise OSError("store unreadable")
        return self.populated


def test_the_partial_decision_refuses_unattributed_cancelled_or_unreadable_runs() -> (
    None
):
    from code_indexer.indexing.processor import ProcessingStats
    from code_indexer.services.index_failure_exit_codes import (
        failed_only_on_owed_retries,
    )

    owed = frozenset({"src/owed_000.py", "src/owed_001.py"})

    def stats(**overrides: object) -> ProcessingStats:
        fields: dict = dict(
            failed_files=2, failed_paths=owed, retried_failed_paths=owed
        )
        fields.update(overrides)
        return ProcessingStats(**fields)

    assert failed_only_on_owed_retries(stats(), _Store(), "c")
    assert not failed_only_on_owed_retries(stats(), _Store(populated=False), "c")
    assert not failed_only_on_owed_retries(stats(failed_files=3), _Store(), "c"), (
        "an unattributed failure was taken for a retry"
    )
    assert not failed_only_on_owed_retries(stats(cancelled=True), _Store(), "c")
    assert not failed_only_on_owed_retries(stats(files_processed=1), _Store(), "c")
    assert not failed_only_on_owed_retries(
        stats(failed_paths=owed | {"src/fresh_000.py"}, failed_files=3), _Store(), "c"
    )
    assert not failed_only_on_owed_retries(stats(), _Store(error=True), "c")


def test_recorded_failures_that_fail_again_on_a_populated_store_exit_0_and_stay_recorded(
    tmp_path: Path, fake_provider
) -> None:
    repo = _repo_owing_recorded_failures(tmp_path, fake_provider)
    rows_before = repo.stored_rows()

    retry = repo.cidx(*repo.index_args())

    assert retry.returncode == 0, (retry.returncode, retry.stdout[-2000:])
    assert "index is empty" not in retry.stdout, retry.stdout[-2000:]
    assert _recorded_failures(repo) == _OWED, "the owed files were forgotten"
    assert repo.stored_rows() == rows_before, "the kept index changed"


def test_a_new_failing_file_beside_retried_failures_exits_1(
    tmp_path: Path, fake_provider
) -> None:
    repo = _repo_owing_recorded_failures(tmp_path, fake_provider)
    (repo.repo / "src" / "fresh_000.py").write_text("def fresh():\n    return 1\n")

    run = repo.cidx(*repo.index_args())

    assert run.returncode == 1, (run.returncode, run.stdout[-2000:])
    assert "All files failed" in run.stdout, run.stdout[-2000:]
    assert _recorded_failures(repo) == sorted(_OWED + ["fresh_000.py"])


def test_retried_failures_on_an_empty_store_exit_1(
    tmp_path: Path, fake_provider
) -> None:
    repo = _repo_owing_recorded_failures(tmp_path, fake_provider)
    conn = sqlite3.connect(str(repo.chunks_db))
    try:
        conn.execute("DELETE FROM chunks")
        conn.commit()
    finally:
        conn.close()

    run = repo.cidx(*repo.index_args())

    assert run.returncode == 1, (run.returncode, run.stdout[-2000:])
    assert "Failed files: 2" in run.stdout, "fixture: only the retries ran"
    assert _recorded_failures(repo) == _OWED
