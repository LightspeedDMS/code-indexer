"""global_repo_refresh moves from the leader's queue onto the pod-pull queue.

Refresh was M3: every pod's scheduler loop submitted into its OWN in-memory
pool, so the configured budget was spent once per process and nothing bounded
the fleet. Leader-gating it would be the wrong fix -- ~922 repos funnelled
through one pod trades a load spike for a throughput collapse. The right shape
is M2: rows left PENDING in the shared queue, claimed by whichever pod has
memory headroom, one repo per pod at a time, ``FOR UPDATE SKIP LOCKED``.

Most of this file is about the three ways the migration can look done and not
be:

* ``submit_job`` without ``metadata=`` silently falls back to the local pool --
  the migration becomes a no-op with no error anywhere;
* the claim loop calls ``complete_job(result)`` on any non-raising return, so a
  refresh that answers ``{"success": False}`` without raising is recorded as a
  SUCCESS (``DistributedJobWorkerService`` guards this; the pod-pull path has to
  guard it too);
* ``execute_refresh_for_claimed_job`` hardcoded ``force_reset=False``, so an
  admin's force-reset would silently become an ordinary refresh -- and since
  change detection then skips an unchanged repo, it would do nothing at all.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from code_indexer.global_repos import refresh_scheduler as rs
from code_indexer.global_repos.refresh_scheduler import (
    GLOBAL_REPO_REFRESH_OP,
    RefreshScheduler,
    global_repo_refresh_executor,
)
from code_indexer.server.repositories.background_jobs import POD_PULL_OPS


# ───────────────────────────── op-set membership ─────────────────────────────


def test_refresh_is_a_pod_pull_op() -> None:
    assert GLOBAL_REPO_REFRESH_OP in POD_PULL_OPS


def test_the_op_constant_matches_the_submitted_operation_type() -> None:
    """background_jobs spells the literal; this is the constant. They must agree.

    ``validate_dispatch_covers`` fails startup when the dispatch keys drift from
    POD_PULL_OPS, but nothing catches the op CONSTANT drifting from the string
    actually passed to submit_job -- that would just quietly stop work-stealing.
    """
    src = inspect.getsource(RefreshScheduler._submit_refresh_job)
    assert "GLOBAL_REPO_REFRESH_OP" in src, (
        "_submit_refresh_job should submit under the shared constant, not a "
        "second copy of the literal"
    )


# ──────────────────────────── submit carries metadata ────────────────────────


class _Bjm:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def submit_job(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "job-1"


def _scheduler(bjm: Optional[_Bjm]) -> RefreshScheduler:
    s = RefreshScheduler.__new__(RefreshScheduler)
    s.background_job_manager = bjm
    s._scheduled_local_repo_repair_is_quarantined = lambda _alias: False
    return s


def test_submit_passes_reconstruction_metadata(monkeypatch):
    """Without metadata= the row is NOT work-stealable and nothing says so.

    submit_job's pod-pull branch requires non-None metadata; otherwise it falls
    straight through to this pod's local pool. The migration would then be a
    silent no-op -- the op is in POD_PULL_OPS, the dispatch entry exists, and
    every refresh still runs where it was submitted.
    """
    bjm = _Bjm()
    _scheduler(bjm)._submit_refresh_job("alpha", submitter_username="admin")
    (call,) = bjm.calls
    assert call["metadata"] is not None
    assert call["metadata"]["alias_name"] == "alpha"


def test_force_reset_travels_in_the_metadata(monkeypatch):
    """The executing pod cannot see the submitting call's arguments."""
    bjm = _Bjm()
    _scheduler(bjm)._submit_refresh_job(
        "alpha", submitter_username="admin", force_reset=True
    )
    assert bjm.calls[0]["metadata"]["force_reset"] is True


def test_metadata_records_force_reset_false_explicitly(monkeypatch):
    bjm = _Bjm()
    _scheduler(bjm)._submit_refresh_job("alpha", submitter_username="admin")
    assert bjm.calls[0]["metadata"]["force_reset"] is False


# ────────────────────────────────── executor ─────────────────────────────────


class _Scheduler:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.seen: Dict[str, Any] = {}

    def execute_refresh_for_claimed_job(self, alias, **kwargs):
        self.seen = {"alias": alias, **kwargs}
        return self.result


def test_executor_runs_the_claimed_job_path_not_a_resubmit():
    """Re-entering submission would collide with the caller's own claimed row."""
    sched = _Scheduler({"success": True})
    cb = object()
    global_repo_refresh_executor(sched, {"alias_name": "alpha"}, cb)
    assert sched.seen["alias"] == "alpha"
    assert sched.seen["progress_callback"] is cb


def test_executor_forwards_force_reset():
    sched = _Scheduler({"success": True})
    global_repo_refresh_executor(
        sched, {"alias_name": "alpha", "force_reset": True}, None
    )
    assert sched.seen["force_reset"] is True


def test_executor_defaults_force_reset_off_for_an_old_row():
    """A row enqueued before this change has no force_reset key."""
    sched = _Scheduler({"success": True})
    global_repo_refresh_executor(sched, {"alias_name": "alpha"}, None)
    assert sched.seen["force_reset"] is False


def test_executor_forwards_the_claim_loop_s_job_id():
    """EVO-66105/EVO-66113: per-job VoyageAI cost correlation.

    IndexJobClaimLoop injects ``metadata["job_id"]`` for exactly this -- the
    executor signature is (metadata, callback), so a key is how the id travels
    without changing every dispatch target. _execute_refresh threads it into
    _index_source -> build_embedding_stats_child_env, which is what tags each
    EmbeddingCallRecord.

    Dropping it does not fail anything: refreshes still run, and every VoyageAI
    call record for a cluster refresh simply gets a NULL job id. Per-job cost
    attribution stops silently.
    """
    sched = _Scheduler({"success": True})
    global_repo_refresh_executor(
        sched, {"alias_name": "alpha", "job_id": "job-77"}, None
    )
    assert sched.seen["job_id"] == "job-77"


def test_executor_tolerates_a_row_without_a_job_id():
    """Direct _run_tick callers and older rows have no job_id key."""
    sched = _Scheduler({"success": True})
    global_repo_refresh_executor(sched, {"alias_name": "alpha"}, None)
    assert sched.seen["job_id"] is None


def test_executor_returns_the_result_on_success():
    sched = _Scheduler({"success": True, "changes": 3})
    assert global_repo_refresh_executor(sched, {"alias_name": "a"}, None) == {
        "success": True,
        "changes": 3,
    }


def test_executor_raises_on_a_non_raising_failure():
    """IndexJobClaimLoop calls complete_job() on ANY non-raising return.

    _execute_refresh can report a real failure as {"success": False} WITHOUT
    raising (an integrity-gate skip, for one). Returned as-is, the claim loop
    marks the job COMPLETED and the repo silently stops being refreshed.
    DistributedJobWorkerService has guarded this since Story #1586; the pod-pull
    executor must too, or the migration loses the guard.
    """
    sched = _Scheduler({"success": False, "message": "integrity gate refused"})
    with pytest.raises(RuntimeError, match="integrity gate refused"):
        global_repo_refresh_executor(sched, {"alias_name": "alpha"}, None)


def test_the_failure_message_survives_when_only_error_is_set():
    sched = _Scheduler({"success": False, "error": "clone vanished"})
    with pytest.raises(RuntimeError, match="clone vanished"):
        global_repo_refresh_executor(sched, {"alias_name": "alpha"}, None)


def test_a_failure_with_no_detail_still_names_the_repo():
    sched = _Scheduler({"success": False})
    with pytest.raises(RuntimeError, match="alpha"):
        global_repo_refresh_executor(sched, {"alias_name": "alpha"}, None)


def test_a_dict_without_a_success_key_is_not_a_failure():
    """Only an EXPLICIT success=False fails the job -- ``not result["success"]``
    would read a missing key as a failure.

    This is the same ``is False`` test DistributedJobWorkerService uses, and the
    difference is not academic: _execute_refresh's early-return shapes do not all
    carry the key, and raising on one of those would fail a refresh that in fact
    succeeded -- then retry it every cycle.
    """
    sched = _Scheduler({"status": "no changes"})
    assert global_repo_refresh_executor(sched, {"alias_name": "a"}, None) == {
        "status": "no changes"
    }


def test_a_falsy_but_present_success_value_is_not_an_explicit_failure():
    sched = _Scheduler({"success": None})
    assert global_repo_refresh_executor(sched, {"alias_name": "a"}, None) == {
        "success": None
    }


def test_a_non_dict_result_is_not_treated_as_a_failure():
    """Only an explicit success=False is a failure; None/other is passed through."""
    sched = _Scheduler(None)
    assert global_repo_refresh_executor(sched, {"alias_name": "a"}, None) is None


# ──────────────── execute_refresh_for_claimed_job honours force_reset ────────


def test_claimed_job_path_forwards_force_reset(monkeypatch):
    seen: Dict[str, Any] = {}
    s = RefreshScheduler.__new__(RefreshScheduler)
    s._resolve_global_alias = lambda a: a
    s._execute_refresh = lambda alias, **kw: seen.update({"alias": alias, **kw}) or {}

    s.execute_refresh_for_claimed_job("alpha", force_reset=True)
    assert seen["force_reset"] is True
    assert seen["tracked_by_caller"] is True


def test_claimed_job_path_forwards_job_id(monkeypatch):
    seen: Dict[str, Any] = {}
    s = RefreshScheduler.__new__(RefreshScheduler)
    s._resolve_global_alias = lambda a: a
    s._execute_refresh = lambda alias, **kw: seen.update({"alias": alias, **kw}) or {}

    s.execute_refresh_for_claimed_job("alpha", job_id="job-77")
    assert seen["job_id"] == "job-77"


def test_claimed_job_path_still_defaults_to_no_force_reset():
    seen: Dict[str, Any] = {}
    s = RefreshScheduler.__new__(RefreshScheduler)
    s._resolve_global_alias = lambda a: a
    s._execute_refresh = lambda alias, **kw: seen.update({"alias": alias, **kw}) or {}

    s.execute_refresh_for_claimed_job("alpha")
    assert seen["force_reset"] is False


# ─────────────────────────────── lifespan wiring ─────────────────────────────

_LIFESPAN = (
    Path(rs.__file__).resolve().parents[1] / "server" / "startup" / "lifespan.py"
)


def test_lifespan_dispatch_has_a_refresh_executor() -> None:
    """validate_dispatch_covers turns drift into a startup RuntimeError.

    That protects the running server but not the test suite, which never builds
    the dispatch table. Assert the entry exists so adding the op to POD_PULL_OPS
    without an executor fails here rather than at the next deploy.

    Matched as a dict KEY, not as a name appearing somewhere in the file: the
    looser form passes on the import statement alone, with no dispatch entry at
    all. (Reviewer observation on !93.)
    """
    tree = ast.parse(_LIFESPAN.read_text(encoding="utf-8"))
    keys = [
        k
        for d in ast.walk(tree)
        if isinstance(d, ast.Dict)
        for k in d.keys
        if isinstance(k, ast.Name) and k.id == "GLOBAL_REPO_REFRESH_OP"
    ]
    assert keys, (
        "lifespan.py's pod-pull dispatch has no GLOBAL_REPO_REFRESH_OP KEY; "
        "validate_dispatch_covers will raise at startup"
    )


def test_leader_worker_still_owns_the_other_refresh_op() -> None:
    """refresh_golden_repo is NOT migrated and must stay on the leader path.

    The leader claims with exclude_types=POD_PULL_OPS, so adding
    global_repo_refresh to that set hands it over automatically -- but the
    sibling op is submitted elsewhere and keeps its existing route. Pinned so a
    later pass does not assume both moved together.
    """
    from code_indexer.server.services import distributed_job_worker as djw

    src = inspect.getsource(djw.DistributedJobWorkerService._execute_retryable_job)
    assert "refresh_golden_repo" in src
