"""EVO-64616: an idle repository gets its one chunk coverage check.

A file indexed while image chunks bypassed the text index is missing some of
its chunks, and only an indexing run looks for such files (once per index,
``chunk_coverage``). A repository with no upstream change never runs one, so
a completed index that does not record the current chunk coverage version is
a stale-index signal (``CHUNK_COVERAGE_SIGNAL``). It forces a plain
incremental run -- the check reads the stored payloads, so no ``--reconcile``
is needed -- and once the version is recorded the refresh skips again. Like
every signal it is bounded (``admit_forced_reconcile``).

Real scheduler, registry, snapshot manager and metadata store; the indexing
child is a real process with a scripted outcome (``fake_index_child``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator

import pytest

from code_indexer.global_repos.stale_index_signal import (
    MAX_FORCED_RECONCILES_PER_SIGNAL,
)
from code_indexer.services.chunk_coverage import CHUNK_COVERAGE_KEY
from tests.utils.fake_index_child import FakeIndexChild, install_fake_index_child
from tests.utils.golden_repo_metadata_stores import (
    STORE_KINDS,
    golden_repo_metadata_store,
)
from tests.utils.refresh_fatal_store_harness import (
    ALIAS,
    Harness,
    age_source_files,
    build_harness,
)

pytestmark = pytest.mark.slow


@pytest.fixture(params=STORE_KINDS)
def metadata(request: Any, tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store(request.param, tmp_path) as backend:
        yield backend


@pytest.fixture
def child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeIndexChild:
    fake = install_fake_index_child(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{fake.bin_dir}{os.pathsep}{os.environ['PATH']}")
    return fake


def _as_indexed_before_the_check(harness: Harness) -> None:
    """The source's provider metadata as a cidx older than the check left it."""
    path = harness.source / ".code-indexer" / "metadata-voyage-ai.json"
    data = json.loads(path.read_text())
    data.pop(CHUNK_COVERAGE_KEY, None)
    path.write_text(json.dumps(data))


@pytest.mark.parametrize("git_remote", [True, False], ids=["git", "local"])
def test_an_unchecked_index_gets_one_incremental_run_then_skips(
    tmp_path: Path, metadata: Any, child: FakeIndexChild, git_remote: bool
) -> None:
    harness = build_harness(
        tmp_path, metadata, snapshot_mode="clean", git_remote=git_remote
    )
    child.set_mode("recover")
    published = harness.scheduler._execute_refresh(ALIAS, force_reset=True)
    assert published["message"] == "Refresh complete", published
    before = child.index_calls()
    _as_indexed_before_the_check(harness)
    age_source_files(harness)

    first = harness.scheduler._execute_refresh(ALIAS)

    assert first["message"] == "Refresh complete", first
    assert child.index_calls() == before + 1
    assert "--reconcile" not in child.index_args()[before]

    age_source_files(harness)
    second = harness.scheduler._execute_refresh(ALIAS)

    assert second["message"] == "No changes detected", second
    assert child.index_calls() == before + 1, "a checked index was forced again"


def test_a_check_that_never_records_is_bounded(
    tmp_path: Path, metadata: Any, child: FakeIndexChild
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean", git_remote=True)
    _as_indexed_before_the_check(harness)
    child.set_mode("noop")

    for _ in range(MAX_FORCED_RECONCILES_PER_SIGNAL + 2):
        harness.scheduler._execute_refresh(ALIAS)

    assert child.index_calls() == MAX_FORCED_RECONCILES_PER_SIGNAL
