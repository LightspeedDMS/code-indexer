"""Story #2122 parent side: the refresh job result carries the indexing
child's metered provider spend (``inputs_sent``, ``attempts``) read from the
child's final ``--progress-json`` record, and a meter-defect child (exit 88)
fails the refresh loudly without a restore or a failure-backoff strike.

Real scheduler, registry, snapshot manager and metadata store (SQLite
always, PostgreSQL with ``TEST_POSTGRES_DSN``). The indexing child is a real
process whose ``index`` outcome and stdout are scripted (``fake_index_child``):
the real child's record itself is proven by the #2122 CLI tests.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterator

import pytest

from tests.unit.global_repos.test_refresh_forced_reconcile_convergence import (
    Harness as ConvergenceHarness,
)
from tests.utils.fake_index_child import FakeIndexChild, install_fake_index_child
from tests.utils.fatal_chunk_store_fixtures import corrupt_btree_pages
from tests.utils.golden_repo_metadata_stores import (
    STORE_KINDS,
    golden_repo_metadata_store,
)
from tests.utils.refresh_fatal_store_harness import ALIAS, build_harness, sha256_of

# Runs real snapshot and child subprocesses: seconds alone, slower under load.
pytestmark = pytest.mark.timeout(90)


@pytest.fixture(params=STORE_KINDS)
def metadata(request, tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store(request.param, tmp_path) as backend:
        yield backend


@pytest.fixture
def child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeIndexChild:
    fake = install_fake_index_child(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{fake.bin_dir}{os.pathsep}{os.environ['PATH']}")
    return fake


def test_refresh_results_carry_the_childs_metered_spend(
    tmp_path: Path, metadata: Any, child: FakeIndexChild
) -> None:
    """A first refresh with new content reports what its child sent; a
    second one that sent nothing reports ``inputs_sent: 0`` (never omitted)."""
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    child.set_mode("recover")
    child.set_run_spend(inputs_sent=12, attempts=3)

    first = harness.scheduler._execute_refresh(ALIAS, force_reset=True)

    assert first["message"] == "Refresh complete", first
    assert (first["inputs_sent"], first["attempts"]) == (12, 3)

    # Nothing new to embed: a touched file whose content did not change
    # still makes the local:// change check run the child, which sends 0.
    _touch_a_source_file(harness.source)
    child.set_run_spend(inputs_sent=0, attempts=0)
    calls_before = child.index_calls()
    second = harness.scheduler._execute_refresh(ALIAS, force_reset=True)

    assert child.index_calls() == calls_before + 1, "the child never ran"
    assert second["message"] == "Refresh complete", second
    assert (second["inputs_sent"], second["attempts"]) == (0, 0)


def test_partial_publish_result_carries_spend(
    tmp_path: Path, metadata: Any, child: FakeIndexChild
) -> None:
    """Bug #2125's partial publish (exit 0, recorded failed files) reports
    the spend beside ``partial``."""
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    child.set_mode("partial")
    child.set_run_spend(inputs_sent=7, attempts=2)

    result = harness.scheduler._execute_refresh(ALIAS, force_reset=True)

    assert result.get("partial") is True, result
    assert (result["inputs_sent"], result["attempts"]) == (7, 2)


def test_gate_failure_result_carries_spend(
    tmp_path: Path, metadata: Any, child: FakeIndexChild
) -> None:
    """A refresh whose integrity gate fails after indexing still reports
    what its child sent (the money was spent)."""
    harness = build_harness(tmp_path, metadata, snapshot_mode="none", git_remote=True)
    corrupt_btree_pages(harness.source_db)
    child.set_mode("recover")
    child.set_run_spend(inputs_sent=4, attempts=1)

    result = harness.scheduler._execute_refresh(ALIAS, force_reset=True)

    assert result["success"] is False, result
    assert result["message"] == "Refresh integrity gate failed; publish skipped"
    assert (result["inputs_sent"], result["attempts"]) == (4, 1)


def test_forced_reconcile_no_change_result_carries_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The post-indexing "No changes detected" of a forced reconcile that
    changed nothing reports the spend too (here 0: nothing re-embedded).
    The indexer runs in-process (convergence harness); the wrapper returns
    the record its child would print."""
    harness = ConvergenceHarness(tmp_path, monkeypatch)
    harness.index()
    harness.embedder.embedded_texts.clear()
    harness.write_stale_provider(status="completed", current_commit="0" * 40)
    indexer = harness.scheduler._index_source

    def index_and_report(**kwargs: Any) -> Dict[str, int]:
        indexer(**kwargs)
        sent = len(harness.embedder.embedded_texts)
        return {"inputs_sent": sent, "attempts": sent}

    monkeypatch.setattr(harness.scheduler, "_index_source", index_and_report)

    result = harness.refresh()

    assert harness.index_calls == [True], "the forced reconcile never indexed"
    assert result["message"] == "No changes detected", result
    assert (result["inputs_sent"], result["attempts"]) == (0, 0)


def test_child_without_a_record_reports_no_spend_fields(
    tmp_path: Path, metadata: Any, child: FakeIndexChild
) -> None:
    """An older child prints no record: the fields are omitted, never 0, so
    "not metered" is never read as "sent nothing"."""
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    child.set_mode("recover")
    child.set_stdout(None)

    result = harness.scheduler._execute_refresh(ALIAS, force_reset=True)

    assert result["message"] == "Refresh complete", result
    assert "inputs_sent" not in result and "attempts" not in result


def test_malformed_record_fails_the_refresh_loudly(
    tmp_path: Path, metadata: Any, child: FakeIndexChild
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    target_before = harness.scheduler.alias_manager.read_alias(ALIAS)
    child.set_mode("recover")
    child.set_stdout('{"record": "run_spend", "inputs_sent": "12"}\n')

    with pytest.raises(RuntimeError, match="malformed run_spend record"):
        harness.scheduler._execute_refresh(ALIAS, force_reset=True)

    assert harness.scheduler.alias_manager.read_alias(ALIAS) == target_before


def test_meter_defect_child_fails_loudly_without_restore_or_strike(
    tmp_path: Path, metadata: Any, child: FakeIndexChild
) -> None:
    """Exit 88 (the child's run-spend meter refused a send) is not a
    chunk-store kind: the refresh fails loudly like any indexing failure --
    nothing published, nothing restored over the source, no
    failure-backoff strike."""
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    target_before = harness.scheduler.alias_manager.read_alias(ALIAS)
    source_db_before = sha256_of(harness.source_db)
    child.set_mode("defect")
    child.set_run_spend(inputs_sent=3, attempts=1)

    with pytest.raises(RuntimeError, match="run-spend meter defect") as raised:
        harness.scheduler._execute_refresh(ALIAS, force_reset=True)

    assert "semantic indexing on source failed" in str(raised.value)
    assert "run spend: inputs_sent=3 attempts=1" in str(raised.value)
    assert child.index_calls() == 1
    assert harness.scheduler.alias_manager.read_alias(ALIAS) == target_before
    assert sha256_of(harness.source_db) == source_db_before, "restored"
    state = metadata.get_refresh_failure_backoff_state(ALIAS)
    assert state is not None and state["consecutive_failure_count"] == 0, state
    assert harness.strikes() == 0
    # Reviewer ruling: exit 88 keeps the #2125 owed retry of a trigger-only
    # (local://) repository.
    assert state["pending_trigger"] is True, state


@pytest.mark.parametrize(
    "mode, expected",
    [
        ("fail", "embedding provider rejected the request"),
        ("chunk_store_env", "fatal chunk-store environment failure"),
    ],
)
def test_failed_child_error_carries_its_spend(
    tmp_path: Path, metadata: Any, child: FakeIndexChild, mode: str, expected: str
) -> None:
    """A child that spent, then failed (generic exit 1, or a fatal
    chunk-store exit 87): the refresh error still says what it sent."""
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    child.set_mode(mode)
    child.set_run_spend(inputs_sent=10000, attempts=80)

    with pytest.raises(RuntimeError) as raised:
        harness.scheduler._execute_refresh(ALIAS, force_reset=True)

    message = str(raised.value)
    assert expected in message, message
    assert "run spend: inputs_sent=10000 attempts=80" in message, message


def test_malformed_record_on_a_failure_reports_both(
    tmp_path: Path, metadata: Any, child: FakeIndexChild
) -> None:
    """A malformed record never masks the child's own failure."""
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    child.set_mode("fail")
    child.set_stdout('{"record": "run_spend", "inputs_sent": -1, "attempts": 0}\n')

    with pytest.raises(RuntimeError) as raised:
        harness.scheduler._execute_refresh(ALIAS, force_reset=True)

    message = str(raised.value)
    assert "embedding provider rejected the request" in message, message
    assert "run spend unknown: malformed run_spend record" in message, message


def test_failure_after_indexing_carries_the_spend(
    tmp_path: Path,
    metadata: Any,
    child: FakeIndexChild,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Indexing succeeded and spent, then a later step failed (e.g. the
    snapshot, or the cidx-meta backup sync): the job error carries the spend."""
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    child.set_mode("recover")
    child.set_run_spend(inputs_sent=21, attempts=5)

    def failing_snapshot(**_: Any) -> str:
        raise OSError("snapshot volume full")

    monkeypatch.setattr(harness.scheduler, "_create_snapshot", failing_snapshot)

    with pytest.raises(RuntimeError) as raised:
        harness.scheduler._execute_refresh(ALIAS, force_reset=True)

    message = str(raised.value)
    assert "snapshot volume full" in message, message
    assert "run spend: inputs_sent=21 attempts=5" in message, message


def test_successful_child_without_a_record_logs_a_warning(
    tmp_path: Path,
    metadata: Any,
    child: FakeIndexChild,
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    child.set_mode("recover")
    child.set_stdout(None)

    with caplog.at_level(logging.WARNING):
        harness.scheduler._execute_refresh(ALIAS, force_reset=True)

    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "run-spend record" in r.getMessage()
    ]
    assert len(warnings) == 1 and ALIAS in warnings[0], warnings


def test_semantic_spend_survives_the_temporal_phase(
    tmp_path: Path,
    metadata: Any,
    child: FakeIndexChild,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The temporal child prints no record; the semantic record is read
    before its run clears the captured stdout.

    The harness repo holds no temporal shards, so the start-of-refresh
    registry reconcile would turn temporal off; the real detection is
    wrapped to report the temporal index present (this test pins the
    phase ordering, not the detection)."""
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean", git_remote=True)
    harness.registry.update_enable_temporal(ALIAS, True)
    detect = harness.scheduler._detect_existing_indexes

    def detect_with_temporal(*args: Any, **kwargs: Any) -> Dict[str, bool]:
        return {**detect(*args, **kwargs), "temporal": True}

    monkeypatch.setattr(
        harness.scheduler, "_detect_existing_indexes", detect_with_temporal
    )
    child.set_mode("recover")
    child.set_run_spend(inputs_sent=6, attempts=2)

    result = harness.scheduler._execute_refresh(ALIAS, force_reset=True)

    assert child.index_calls() == 2, "the temporal phase did not run"
    assert result["message"] == "Refresh complete", result
    assert (result["inputs_sent"], result["attempts"]) == (6, 2)


def _touch_a_source_file(source: Path) -> None:
    path = next(
        p
        for p in sorted(source.iterdir())
        if p.is_file() and not p.name.startswith(".")
    )
    future = time.time() + 5
    os.utime(path, (future, future))
