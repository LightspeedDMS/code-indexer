"""
Unit tests wiring the Bug #1769 local-repo `cidx init` repair circuit
breaker into RefreshScheduler._execute_refresh().

Bug #1769: a local repo (e.g. an auto-discovery-created
langfuse_Claude_Code_*-global repo) whose .code-indexer/ directory exists
but has no valid config.json (Bug #1253) is self-healed by re-running
`cidx init --force` on every scheduled refresh cycle
(_repair_uninitialized_local_repo). Before this fix there was NO
persisted failure state for this repair path at all -- when the repair
itself keeps failing (e.g. a structurally broken repo the automated
self-heal can never fix), RefreshScheduler retried the identical `cidx
init --force` subprocess call and logged an identical ERROR on EVERY
SINGLE scheduled cycle, forever, with zero convergence. Confirmed live on
staging: 1,151 recurring "Failed to repair uninitialized local repo ...
via 'cidx init'" log entries over 3+ days across multiple deploys.

This test proves the desired FIXED behavior:
1. The first N (=_LOCAL_REPO_REPAIR_QUARANTINE_THRESHOLD) consecutive
   scheduled cycles each genuinely attempt the repair (subprocess call
   count increases 1:1 with cycles) and each records a persisted failure
   via golden_repo_metadata.record_local_repo_repair_failure().
2. Cycle N+1 onward: the repair subprocess is NOT invoked again -- the
   scheduler detects the persisted consecutive-failure count has reached
   the threshold and short-circuits with a distinct
   "local_repo_repair_quarantined" skip result instead of retrying
   identically forever (Messi Rule #14 -- Anti-Unbounded-Loop).

Against the CURRENT (pre-fix) code, this test fails: subprocess.run is
invoked on every single cycle with no cap, exactly reproducing the
unbounded-retry defect from the real staging incident.

Reuses the exact fixture/mocking pattern already established by
test_refresh_scheduler_integrity_gate_1506.py, but with a REAL
GoldenRepoMetadataSqliteBackend (not a bare Mock) so the persisted
consecutive-failure counter genuinely accumulates across simulated
scheduled cycles -- matching this project's anti-mock discipline for the
exact mechanism under test.
"""

import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend


ALIAS = "langfuse_Claude_Code_seba-global"
REPO_NAME = "langfuse_Claude_Code_seba"


@pytest.fixture
def golden_repos_dir(tmp_path):
    golden_dir = tmp_path / "golden-repos"
    golden_dir.mkdir(parents=True)
    return golden_dir


@pytest.fixture
def mock_query_tracker():
    return Mock(spec=QueryTracker)


@pytest.fixture
def mock_cleanup_manager():
    return Mock(spec=CleanupManager)


@pytest.fixture
def mock_config_source():
    config = Mock()
    config.get_global_refresh_interval.return_value = 3600
    return config


@pytest.fixture
def mock_registry():
    registry = Mock()
    registry.get_global_repo.return_value = {
        "alias_name": ALIAS,
        "repo_url": "local://langfuse",
    }
    registry.list_global_repos.return_value = []
    registry.update_refresh_timestamp.return_value = None
    return registry


@pytest.fixture
def golden_repo_metadata_backend():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = os.path.join(temp_dir, "test.db")
        be = GoldenRepoMetadataSqliteBackend(db_path)
        be.ensure_table_exists()
        yield be


@pytest.fixture
def scheduler(
    golden_repos_dir,
    mock_config_source,
    mock_query_tracker,
    mock_cleanup_manager,
    mock_registry,
    golden_repo_metadata_backend,
):
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=mock_config_source,
        query_tracker=mock_query_tracker,
        cleanup_manager=mock_cleanup_manager,
        registry=mock_registry,
        golden_repo_metadata_backend=golden_repo_metadata_backend,
    )


def _make_uninitialized_local_repo(golden_repos_dir: Path) -> Path:
    """Create master_path/.code-indexer/ with NO config.json -- the exact
    Bug #1253 broken state RefreshScheduler tries to self-heal."""
    master_path = golden_repos_dir / REPO_NAME
    (master_path / ".code-indexer").mkdir(parents=True)
    return master_path


def _run_one_cycle(scheduler, master_path):
    with (
        patch.object(
            scheduler.alias_manager, "read_alias", return_value=str(master_path)
        ),
        patch.object(scheduler, "_detect_existing_indexes", return_value={}),
        patch.object(scheduler, "_reconcile_registry_with_filesystem"),
        # Downstream indexing/snapshot/publish steps are irrelevant to
        # the repair-circuit-breaker behavior under test here (they are
        # covered by test_refresh_scheduler_integrity_gate_1506.py) --
        # neutralized so a cycle that gets PAST the repair step (i.e.
        # repair succeeded) does not fall through into real indexing of
        # an empty fixture repo.
        patch.object(scheduler, "_index_source"),
        patch.object(scheduler, "_create_snapshot", return_value=str(master_path)),
        patch.object(scheduler.alias_manager, "swap_alias"),
    ):
        return scheduler._execute_refresh(ALIAS)


class TestLocalRepoRepairCircuitBreaker:
    def test_repair_quarantines_after_threshold_confirmed_failures(
        self, scheduler, golden_repos_dir, golden_repo_metadata_backend
    ):
        from code_indexer.global_repos.refresh_scheduler import (
            _LOCAL_REPO_REPAIR_QUARANTINE_THRESHOLD,
        )

        master_path = _make_uninitialized_local_repo(golden_repos_dir)

        always_fail = subprocess.CalledProcessError(
            1, ["cidx", "init"], stderr="no configuration found"
        )

        with patch(
            "code_indexer.global_repos.refresh_scheduler.subprocess.run",
            side_effect=always_fail,
        ) as mock_subprocess_run:
            results = [
                _run_one_cycle(scheduler, master_path)
                for _ in range(_LOCAL_REPO_REPAIR_QUARANTINE_THRESHOLD + 2)
            ]

        # The first THRESHOLD cycles each genuinely attempted repair.
        assert mock_subprocess_run.call_count == _LOCAL_REPO_REPAIR_QUARANTINE_THRESHOLD
        for result in results[:_LOCAL_REPO_REPAIR_QUARANTINE_THRESHOLD]:
            assert result["success"] is False
            assert result.get("skipped") != "local_repo_repair_quarantined"

        # Cycles beyond THRESHOLD must NOT invoke the repair subprocess
        # again -- this is the circuit breaker. Pre-fix, call_count would
        # keep growing 1:1 with every cycle (unbounded retry).
        assert mock_subprocess_run.call_count == _LOCAL_REPO_REPAIR_QUARANTINE_THRESHOLD

        quarantined_result = results[-1]
        assert quarantined_result["success"] is False
        assert quarantined_result["skipped"] == "local_repo_repair_quarantined"
        assert (
            quarantined_result["consecutive_failure_count"]
            >= _LOCAL_REPO_REPAIR_QUARANTINE_THRESHOLD
        )

        # Persisted state confirms the counter stopped accumulating past
        # confirmation -- a quarantined skip must not itself re-record a
        # failure (it never re-attempted the repair).
        state = golden_repo_metadata_backend.get_local_repo_repair_failure_state(ALIAS)
        assert state is not None
        assert (
            state["consecutive_failure_count"]
            == _LOCAL_REPO_REPAIR_QUARANTINE_THRESHOLD
        )

    def test_successful_repair_resets_quarantine_state(
        self, scheduler, golden_repos_dir, golden_repo_metadata_backend
    ):
        """A repair that eventually succeeds must clear any prior failure
        count -- the breaker gives a repaired repo a fresh budget rather
        than carrying a stale count toward a future unrelated failure."""
        master_path = _make_uninitialized_local_repo(golden_repos_dir)
        always_fail = subprocess.CalledProcessError(
            1, ["cidx", "init"], stderr="no configuration found"
        )

        # One failure, then repair succeeds (writes a valid config.json,
        # as the real `cidx init --force` subprocess would). A stateful
        # callable (not a pre-built list) is required here -- a list
        # literal's elements are evaluated EAGERLY at construction time,
        # so building the "success" side-effect ahead of time would write
        # config.json before the first (failing) cycle ever runs.
        call_count = {"n": 0}

        def _subprocess_run_side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise always_fail
            config_path = master_path / ".code-indexer" / "config.json"
            config_path.write_text("{}")
            return Mock(returncode=0)

        with patch(
            "code_indexer.global_repos.refresh_scheduler.subprocess.run",
            side_effect=_subprocess_run_side_effect,
        ):
            _run_one_cycle(scheduler, master_path)
            state_after_failure = (
                golden_repo_metadata_backend.get_local_repo_repair_failure_state(ALIAS)
            )
            assert state_after_failure is not None
            assert state_after_failure["consecutive_failure_count"] == 1

            _run_one_cycle(scheduler, master_path)

        state_after_success = (
            golden_repo_metadata_backend.get_local_repo_repair_failure_state(ALIAS)
        )
        assert state_after_success is None


class TestLocalRepoRepairQuarantineResetOnHealthyCycle:
    """Bug #1769 code-review HIGH finding: before this fix,
    ``_reset_local_repo_repair_quarantine`` had exactly ONE call site,
    reachable only INSIDE the ``if not self._is_local_config_valid(...)``
    branch after a successful REPAIR. There was no ``else`` branch, so a
    cycle where config.json is ALREADY valid -- the ordinary healthy
    outcome, the vast majority of cycles -- never reset the failure
    counter. Two consequences: (a) the counter is not genuinely
    "consecutive" -- it silently accumulates across healthy periods
    (fail once in January, run healthy for two months, fail once in
    March, run healthy again, fail once in June -- quarantined in June
    despite months of healthy operation in between); (b) once
    quarantined, NOTHING can ever clear it, since the skip-check returns
    before the repair code (and therefore the reset call) ever runs
    again -- a genuine permanent one-way latch with no recovery path,
    violating this project's "cleanup/repair must self-heal and
    converge" invariant.

    This test proves the fix: a healthy cycle (config already valid, no
    repair needed at all) must clear a pre-existing non-zero failure
    count back to a clean state -- mirroring Bug #1506's
    ``_run_refresh_integrity_gate``, which calls its own
    ``_reset_integrity_gate_quarantine`` UNCONDITIONALLY on every
    passing cycle, not only after a just-failed-then-recovered one.
    """

    def test_healthy_cycle_with_already_valid_config_resets_failure_count(
        self, scheduler, golden_repos_dir, golden_repo_metadata_backend
    ):
        # Repo whose config.json is ALREADY valid -- no repair needed at
        # all this cycle (the ordinary healthy outcome).
        master_path = golden_repos_dir / REPO_NAME
        (master_path / ".code-indexer").mkdir(parents=True)
        (master_path / ".code-indexer" / "config.json").write_text("{}")

        # Pre-seed a non-zero failure count from a PRIOR, unrelated
        # failure period (e.g. a repair that failed once months ago and
        # has been healthy ever since) -- below the quarantine threshold
        # so it does not itself block this cycle.
        golden_repo_metadata_backend.record_local_repo_repair_failure(
            ALIAS, "a prior unrelated failure"
        )
        state_before = golden_repo_metadata_backend.get_local_repo_repair_failure_state(
            ALIAS
        )
        assert state_before is not None
        assert state_before["consecutive_failure_count"] == 1

        with patch(
            "code_indexer.global_repos.refresh_scheduler.subprocess.run"
        ) as mock_subprocess_run:
            _run_one_cycle(scheduler, master_path)

        # Config was already valid -- the repair subprocess must never
        # have been invoked this cycle.
        mock_subprocess_run.assert_not_called()

        state_after = golden_repo_metadata_backend.get_local_repo_repair_failure_state(
            ALIAS
        )
        assert state_after is None


class TestLocalRepoRepairQuarantineReadFailureFailsClosed:
    """Bug #1769 code-review MEDIUM-1 finding:
    ``_local_repo_repair_quarantine_check_failed_result`` (the fail-closed
    path taken when reading the persisted quarantine state itself raises)
    had ZERO test coverage anywhere in the original diff, unlike its Bug
    #1506 precedent (``TestQuarantineReadFailureFailsClosed`` in
    test_refresh_scheduler_integrity_gate_1506.py), which this test
    mirrors. A read failure must fail this refresh cycle CLOSED --
    skipping the repair attempt entirely -- rather than silently
    treating a possibly-quarantined repo as healthy and re-running
    `cidx init --force` anyway.
    """

    def test_quarantine_state_read_failure_skips_repair_fails_closed(
        self, scheduler, golden_repos_dir, golden_repo_metadata_backend
    ):
        master_path = _make_uninitialized_local_repo(golden_repos_dir)

        with (
            patch.object(
                golden_repo_metadata_backend,
                "get_local_repo_repair_failure_state",
                side_effect=RuntimeError("metadata backend outage"),
            ),
            patch(
                "code_indexer.global_repos.refresh_scheduler.subprocess.run"
            ) as mock_subprocess_run,
        ):
            result = _run_one_cycle(scheduler, master_path)

        mock_subprocess_run.assert_not_called()
        assert result["success"] is False
        assert result.get("skipped") == "local_repo_repair_quarantine_check_failed"


class TestLocalRepoRepairFailureDetailIsRealStderr:
    """Bug #1769 code-review MEDIUM-2 finding: every recorded repair
    failure persisted the identical literal placeholder text
    "cidx init --force repair failed; see preceding ERROR log for the
    subprocess stderr detail" into ``last_detail``, even though
    ``_repair_uninitialized_local_repo`` already captures the REAL
    stderr internally (``stderr = getattr(e, "stderr", None) or
    str(e)``) -- it was simply discarded because the method's return
    type was a bare ``bool``. This test proves the real stderr text
    ends up persisted into the quarantine bookkeeping's ``last_detail``
    field instead of the useless generic placeholder -- mirroring Bug
    #1506's ``detail_summary`` pattern.
    """

    def test_real_stderr_persisted_as_last_detail_not_generic_placeholder(
        self, scheduler, golden_repos_dir, golden_repo_metadata_backend
    ):
        master_path = _make_uninitialized_local_repo(golden_repos_dir)
        distinctive_stderr = (
            "fatal: distinctive real cidx init stderr text 42 that must "
            "be persisted verbatim"
        )
        repair_failure = subprocess.CalledProcessError(
            1, ["cidx", "init"], stderr=distinctive_stderr
        )

        with patch(
            "code_indexer.global_repos.refresh_scheduler.subprocess.run",
            side_effect=repair_failure,
        ):
            _run_one_cycle(scheduler, master_path)

        state = golden_repo_metadata_backend.get_local_repo_repair_failure_state(ALIAS)
        assert state is not None
        assert distinctive_stderr in state["last_detail"]
        assert (
            state["last_detail"]
            != "cidx init --force repair failed; see preceding ERROR log "
            "for the subprocess stderr detail"
        )
