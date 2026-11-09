"""issue-64616: a refresh heals recorded failures even with no upstream change.

!232 made a failure COUNT with no recorded path (``failed_files`` above
``failed_file_paths``) owe a full reconcile -- but only a run that published
scheduled it. A refresh that found no upstream change returned "No changes
detected" before looking at the record, so a record written before that fix
was never healed (tools-test: jplanner, failed_files=5605, no paths).

Now an owed record (an unattributed count, or recorded paths that still
exist) is a stale-index signal (``OWED_FAILURES_SIGNAL``): the refresh runs
the indexer with ``--reconcile`` even with no upstream change, in a scheduled
and in a forced refresh, for a git and a ``local://`` repository, and the
count clears. It is bounded like every stale signal (``admit_forced_reconcile``):
a reconcile that heals nothing leaves the same key.

Real scheduler, registry, snapshot manager and metadata store; the indexing
child is a real process with a scripted outcome (``fake_index_child``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

from code_indexer.global_repos.stale_index_signal import (
    OWED_FAILURES_SIGNAL,
    owed_failures_signal,
)
from code_indexer.server.services.metadata_reader import (
    RecordedFailures,
    read_recorded_failures,
)
from tests.utils.fake_index_child import (
    STALE_FAILED_FILES,
    FakeIndexChild,
    install_fake_index_child,
)
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

COUNT_ONLY = 5605


@pytest.fixture(params=STORE_KINDS)
def metadata(request: Any, tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store(request.param, tmp_path) as backend:
        yield backend


@pytest.fixture
def child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeIndexChild:
    fake = install_fake_index_child(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{fake.bin_dir}{os.pathsep}{os.environ['PATH']}")
    return fake


def _provider_metadata(harness: Harness) -> Path:
    return harness.source / ".code-indexer" / "metadata-voyage-ai.json"


def _record(harness: Harness, **fields: Any) -> None:
    """Edit the source's provider metadata as an earlier cidx left it."""
    path = _provider_metadata(harness)
    data: Dict[str, Any] = json.loads(path.read_text())
    data.update(fields)
    path.write_text(json.dumps(data))


def _reconciled(child: FakeIndexChild, after: int = 0) -> List[bool]:
    return ["--reconcile" in args for args in child.index_args()[after:]]


def _published_then_recorded(harness: Harness, child: FakeIndexChild) -> int:
    """One published refresh (so a local:// repo's newest snapshot postdates
    its files), then the count-only record an earlier cidx left, and no new
    data. Returns how many index runs that took."""
    child.set_mode("recover")
    first = harness.scheduler._execute_refresh(ALIAS, force_reset=True)
    assert first["message"] == "Refresh complete", first
    _record(harness, failed_files=COUNT_ONLY, failed_file_paths=[])
    age_source_files(harness)
    return child.index_calls()


@pytest.mark.parametrize("git_remote", [True, False], ids=["git", "local"])
@pytest.mark.parametrize("force_reset", [False, True], ids=["scheduled", "forced"])
def test_a_count_only_record_is_reconciled_with_no_upstream_change(
    tmp_path: Path,
    metadata: Any,
    child: FakeIndexChild,
    git_remote: bool,
    force_reset: bool,
) -> None:
    harness = build_harness(
        tmp_path, metadata, snapshot_mode="clean", git_remote=git_remote
    )
    before = _published_then_recorded(harness, child)

    result = harness.scheduler._execute_refresh(ALIAS, force_reset=force_reset)

    assert result["message"] == "Refresh complete", result
    assert _reconciled(child, before) == [True], "the owed count was not reconciled"
    healed = read_recorded_failures(harness.source)
    assert (healed.failed_files, healed.unattributed) == (0, 0)


@pytest.mark.parametrize("git_remote", [True, False], ids=["git", "local"])
def test_a_healed_record_owes_nothing_and_the_skip_returns(
    tmp_path: Path, metadata: Any, child: FakeIndexChild, git_remote: bool
) -> None:
    harness = build_harness(
        tmp_path, metadata, snapshot_mode="clean", git_remote=git_remote
    )
    before = _published_then_recorded(harness, child)
    harness.scheduler._execute_refresh(ALIAS)
    assert child.index_calls() == before + 1
    age_source_files(harness)

    result = harness.scheduler._execute_refresh(ALIAS)

    assert result["message"] == "No changes detected", result
    assert child.index_calls() == before + 1, "a healed record was reconciled again"


def test_a_clean_record_still_skips(
    tmp_path: Path, metadata: Any, child: FakeIndexChild
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean", git_remote=True)

    result = harness.scheduler._execute_refresh(ALIAS)

    assert result["message"] == "No changes detected", result
    assert child.index_calls() == 0


def test_recorded_paths_that_still_exist_owe_a_rerun(
    tmp_path: Path, metadata: Any, child: FakeIndexChild
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean", git_remote=True)
    owed = next(p for p in harness.source.iterdir() if p.is_file())
    _record(harness, failed_files=1, failed_file_paths=[str(owed)])
    child.set_mode("recover")

    result = harness.scheduler._execute_refresh(ALIAS)

    assert result["message"] == "Refresh complete", result
    assert child.index_calls() == 1


# ── the signal ──────────────────────────────────────────────────────────────


def test_the_owed_signal_and_its_bound_key() -> None:
    count_only = RecordedFailures(
        known=True, failed_files=5, retryable_paths=0, unattributed=5
    )
    signal = owed_failures_signal(count_only, "abc1234", "a-global")
    assert signal is not None and signal.kind == OWED_FAILURES_SIGNAL
    assert not signal.may_skip_unchanged_publish
    # The same record and HEAD give the same key: a reconcile that heals
    # nothing is bounded by admit_forced_reconcile.
    again = owed_failures_signal(count_only, "abc1234", "a-global")
    assert again is not None and again.key == signal.key
    retryable = count_only._replace(unattributed=0, retryable_paths=2)
    other = owed_failures_signal(retryable, "abc1234", "a-global")
    assert other is not None and other.key != signal.key


@pytest.mark.parametrize(
    "failures",
    [
        RecordedFailures(known=True, failed_files=0, retryable_paths=0),
        RecordedFailures(known=True, failed_files=3, retryable_paths=0),
        RecordedFailures(known=False, failed_files=0, retryable_paths=0),
    ],
    ids=["clean", "vanished-paths", "unknown"],
)
def test_nothing_owed_is_no_signal(failures: RecordedFailures) -> None:
    assert owed_failures_signal(failures, "abc1234", "a-global") is None


def test_an_unchanged_owed_record_stops_being_forced_after_the_bound(
    tmp_path: Path, metadata: Any
) -> None:
    from code_indexer.global_repos.stale_index_signal import (
        MAX_FORCED_RECONCILES_PER_SIGNAL,
        admit_forced_reconcile,
        record_forced_reconcile_outcome,
    )

    signal = owed_failures_signal(
        RecordedFailures(known=True, failed_files=5, retryable_paths=0, unattributed=5),
        "abc1234",
        ALIAS,
    )
    assert signal is not None
    admitted = []
    for _ in range(MAX_FORCED_RECONCILES_PER_SIGNAL + 2):
        allowed = admit_forced_reconcile(metadata, ALIAS, signal)
        admitted.append(allowed)
        if allowed:
            record_forced_reconcile_outcome(metadata, ALIAS, signal, signal)
    assert admitted == [True] * MAX_FORCED_RECONCILES_PER_SIGNAL + [False, False]


@pytest.mark.parametrize("git_remote", [True, False], ids=["git", "local"])
def test_a_reconcile_that_heals_nothing_counts_toward_the_bound(
    tmp_path: Path, metadata: Any, child: FakeIndexChild, git_remote: bool
) -> None:
    """Through the scheduler: a reconcile that leaves the same count-only
    record is recorded against the SAME signal key each time, so the bound
    (admit_forced_reconcile) can stop it -- the key the no-change path admits
    with equals the one the outcome is recorded with, git and local:// alike."""
    harness = build_harness(
        tmp_path, metadata, snapshot_mode="clean", git_remote=git_remote
    )
    _published_then_recorded(harness, child)
    # The record a reconcile that heals nothing leaves behind, every time.
    _record(harness, failed_files=STALE_FAILED_FILES, failed_file_paths=[])
    child.set_mode("stale")

    for expected_attempts in (1, 2):
        harness.scheduler._execute_refresh(ALIAS)
        age_source_files(harness)
        state = metadata.get_forced_reconcile_state(ALIAS)
        assert state is not None and int(state["attempt_count"]) == expected_attempts
        assert state["signal"].startswith("owed failures unattributed=")
