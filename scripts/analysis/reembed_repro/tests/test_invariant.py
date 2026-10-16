"""Self-tests for the reproduction's verdict (design 15.1: R-a..R-12, A1-A5)."""

from dataclasses import replace

import pytest

from invariant import (
    SKIPPED_NOT_EXERCISED,
    SKIPPED_NOT_IMPLEMENTED,
    Check,
    FinalState,
    RunRecord,
    evaluate,
)


def _run(label, kind, sent=None, **kw):
    sent = dict(sent or {})
    defaults = dict(
        label=label,
        kind=kind,
        interrupted=False,
        exit_code=0,
        inputs_embedded=sum(sent.values()),
        planned_files=None,
        unindexed_at_start=0,
        version="12.83.0",
        sent_counts=sent,
        owed_keys=len(sent),  # every key the healthy run sent was owed
    )
    defaults.update(kw)
    return RunRecord(**defaults)


def _healthy():
    """A fixed version's sequence: every check passes."""
    return [
        _run(
            "initial",
            "initial",
            {"a": 1, "b": 1, "c": 1},
            planned_files=3,
            unindexed_at_start=3,
        ),
        _run(
            "cycle-1",
            "sync",
            {"d": 1, "e": 1},
            interrupted=True,
            exit_code=-9,
            signal_name="SIGKILL",
            trigger="chunks",
            inflight_keys=frozenset({"e"}),
            planned_files=2,
            unindexed_at_start=2,
        ),
        _run(
            "cycle-2",
            "recovery",
            {"e": 1},
            planned_files=1,
            unindexed_at_start=1,
            content_missing_after=0,
        ),
        _run(
            "cycle-3",
            "sync",
            {"f": 1},
            interrupted=True,
            exit_code=75,
            signal_name="SIGTERM",
            trigger="inflight",
            held_keys=frozenset({"f"}),
            held_in_pending_after=frozenset({"f"}),
        ),
        _run(
            "final-1",
            "recovery",
            {},
            planned_files=0,
            unindexed_at_start=0,
            content_missing_after=0,
        ),
        _run("converge-reconcile", "converge", {}, planned_files=0, reconcile=True),
        _run("converge-incremental", "converge", {}, planned_files=0),
    ]


FINAL = FinalState(
    status="completed", content_missing=0, content_files=10, introduced_keys=6
)


def _with(runs, label, **kw):
    return [replace(r, **kw) if r.label == label else r for r in runs]


def test_healthy_sequence_passes_every_check():
    verdict = evaluate(_healthy(), FINAL)
    assert [c.name for c in verdict.checks if c.status != "PASS"] == []
    assert verdict.reproduced is False
    assert "limit 7" in verdict.check("A1").detail


def test_r_a_recovery_planning_more_than_unindexed_fails():
    verdict = evaluate(_with(_healthy(), "cycle-2", planned_files=805), FINAL)
    assert verdict.check("R-a").ok is False
    assert "cycle-2[12.83.0] planned 805 > 1 unindexed" in verdict.check("R-a").detail
    assert verdict.reproduced is True


def _killed(runs, label, trigger, plan_written):
    """``label`` interrupted by ``trigger`` with no observed plan."""
    return _with(
        runs,
        label,
        interrupted=True,
        exit_code=-9,
        signal_name="SIGKILL",
        trigger=trigger,
        planned_files=None,
        plan_written=plan_written,
    )


def test_r_a_early_kill_before_planning_is_na_and_not_a_skip():
    runs = _killed(_healthy(), "cycle-2", "early", plan_written=False)
    check = evaluate(runs, FINAL).check("R-a")
    assert check.status == "PASS"  # mixed: final-1 assessed and passing, cycle-2 N/A
    assert "cycle-2: N/A (killed before planning)" in check.detail


def test_r_a_early_kill_is_na_only_on_an_observed_unwritten_plan():
    for written in (True, None):  # metadata rewritten, or not observed at all
        runs = _killed(_healthy(), "cycle-2", "early", plan_written=written)
        check = evaluate(runs, FINAL).check("R-a")
        assert check.status == "SKIPPED-UNASSESSED", written


def test_r_a_post_plan_kill_with_an_unobserved_plan_is_a_skip():
    runs = _killed(_healthy(), "cycle-2", "chunks", plan_written=True)
    verdict = evaluate(runs, FINAL)
    check = verdict.check("R-a")
    assert (check.ok, check.status) == (False, "SKIPPED-UNASSESSED")
    assert "cycle-2: UNASSESSED (plan expected, not observed)" in check.detail
    assert (verdict.status, verdict.exit_code) == ("INCOMPLETE", 3)


def test_r_a_completed_recovery_with_an_unobserved_plan_is_a_skip():
    runs = _with(_healthy(), "cycle-2", planned_files=None)
    assert evaluate(runs, FINAL).check("R-a").status == "SKIPPED-UNASSESSED"


def test_r_a_with_every_recovery_na_has_nothing_to_assess():
    runs = _killed(_healthy(), "cycle-2", "early", plan_written=False)
    runs = _killed(runs, "final-1", "early", plan_written=False)
    check = evaluate(runs, FINAL).check("R-a")
    assert (check.ok, check.status) == (False, "SKIPPED-NOTHING-TO-ASSESS")


def test_r_a_violation_anywhere_fails_despite_na_and_unassessed_runs():
    runs = _killed(_healthy(), "cycle-2", "chunks", plan_written=True)
    runs = _with(runs, "final-1", planned_files=9, unindexed_at_start=1)
    runs.append(replace(runs[2], label="cycle-2b", trigger="early", plan_written=False))
    assert evaluate(runs, FINAL).check("R-a").status == "FAIL"


R_A_CONTENT = "R-a content"


def test_r_a_content_passes_when_every_run_sends_only_owed_keys():
    assert evaluate(_healthy(), FINAL).check(R_A_CONTENT).status == "PASS"


def test_r_a_content_fails_on_keys_durable_or_off_disk_at_run_start():
    runs = _with(
        _healthy(),
        "final-1",
        sent_counts={"a": 1},
        inputs_embedded=1,
        sent_not_owed=frozenset({"a"}),
    )
    check = evaluate(runs, FINAL).check(R_A_CONTENT)
    assert check.ok is False
    assert "final-1[12.83.0] sent 1 keys not owed" in check.detail


def test_r_a_content_fails_when_inputs_exceed_the_owed_keys():
    # 50 duplicate copies of one new content: one key owed, embedded 50 times.
    runs = _with(
        _healthy(), "cycle-2", sent_counts={"e": 50}, inputs_embedded=50, owed_keys=1
    )
    check = evaluate(runs, FINAL).check(R_A_CONTENT)
    assert check.ok is False
    assert "cycle-2[12.83.0] sent 50 inputs > 1 owed keys" in check.detail


def test_r_a_content_without_a_computed_bound_is_never_a_pass():
    runs = [replace(r, owed_keys=None) for r in _healthy()]
    check = evaluate(runs, FINAL).check(R_A_CONTENT)
    # Never N/A: the bound comes from disk and durable state, so it should
    # exist for every run, however early it was killed.
    assert check.status == "SKIPPED-UNASSESSED"
    partial = _with(_healthy(), "cycle-2", owed_keys=None)
    partial_check = evaluate(partial, FINAL).check(R_A_CONTENT)
    assert "cycle-2: UNASSESSED" in partial_check.detail
    assert partial_check.status == "SKIPPED-UNASSESSED"


def test_r_b_any_re_embedding_of_stored_content_fails():
    verdict = evaluate(
        _with(_healthy(), "cycle-2", reembedded_already_indexed=14), FINAL
    )
    assert verdict.check("R-b").ok is False
    assert "cycle-2[12.83.0] re_idx 14" in verdict.check("R-b").detail


def test_r_c_missing_content_fails_with_examples():
    final = replace(FINAL, content_missing=15, missing_examples=("sessions/sync/x.md",))
    check = evaluate(_healthy(), final).check("R-c")
    assert check.ok is False
    assert "15 of 10 files" in check.detail and "sessions/sync/x.md" in check.detail


def test_final_run_must_exit_zero_with_status_completed():
    assert (
        evaluate(_with(_healthy(), "final-1", exit_code=1), FINAL).check("final run").ok
        is False
    )
    bad_status = replace(FINAL, status="in_progress")
    assert evaluate(_healthy(), bad_status).check("final run").ok is False


def test_r_d_sigterm_that_does_not_yield_fails():
    runs = _with(
        _healthy(),
        "cycle-3",
        exit_code=-15,
        held_in_pending_after=frozenset(),
        hot_journal_after=True,
        inflight_keys=frozenset({"f"}),
    )
    check = evaluate(runs, FINAL).check("R-d")
    assert check.ok is False
    for text in (
        "exit -15 (want 75)",
        "1 of 1 held keys not in pending",
        "hot chunks.db-journal",
    ):
        assert text in check.detail


def test_r_d_held_key_sent_again_by_the_next_run_fails():
    runs = _with(_healthy(), "final-1", sent_counts={"f": 1}, inputs_embedded=1)
    check = evaluate(runs, FINAL).check("R-d")
    assert check.ok is False and "1 held keys re-sent by final-1" in check.detail


def test_r_d_without_held_requests_is_not_deterministic_and_fails():
    runs = _with(
        _healthy(), "cycle-3", held_keys=frozenset(), held_in_pending_after=frozenset()
    )
    assert evaluate(runs, FINAL).check("R-d").ok is False


def test_r_d_not_exercised_is_skipped_never_passed():
    runs = [r for r in _healthy() if r.label != "cycle-3"]
    check = evaluate(runs, FINAL).check("R-d")
    assert (check.ok, check.status) == (False, SKIPPED_NOT_EXERCISED)


def test_r_12_re_sending_durable_content_after_a_kill_fails():
    runs = _with(
        _healthy(),
        "cycle-2",
        sent_counts={"e": 1, "d": 1},
        inputs_embedded=2,
        sent_durable_before=frozenset({"d"}),
    )
    check = evaluate(runs, FINAL).check("R-12")
    assert check.ok is False
    assert "cycle-2[12.83.0] re-sent 1 keys durable at its start" in check.detail


def test_r_12_requires_complete_content_after_the_fresh_recovery_run():
    runs = _with(_healthy(), "cycle-2", content_missing_after=3)
    check = evaluate(runs, FINAL).check("R-12")
    assert (
        check.ok is False
        and "after cycle-2[12.83.0]: 3 files' content missing" in check.detail
    )


def test_r_12_kill_without_a_later_successful_run_fails():
    runs = _healthy()[:2]
    check = evaluate(runs, FINAL).check("R-12")
    assert (
        check.ok is False and "no successful recovery run after cycle-1" in check.detail
    )


def test_r_12_reports_each_failing_recovery_run_once():
    # The fixture already kills cycle-1 and cycle-3; killing cycle-2 too makes
    # three kills whose first successful recovery run is final-1.
    runs = _with(_healthy(), "cycle-2", interrupted=True, exit_code=-9)
    runs = _with(runs, "final-1", content_missing_after=3)
    assert [r.label for r in runs if r.interrupted] == ["cycle-1", "cycle-2", "cycle-3"]
    detail = evaluate(runs, FINAL).check("R-12").detail
    assert detail.count("after final-1[12.83.0]: 3 files' content missing") == 1


def test_a1_inputs_beyond_introduced_plus_inflight_fail():
    runs = _with(_healthy(), "cycle-2", sent_counts={"e": 50}, inputs_embedded=50)
    assert evaluate(runs, FINAL).check("A1").ok is False


def test_a2_key_re_sent_after_a_completed_run_fails():
    runs = _with(
        _healthy(),
        "final-1",
        sent_counts={"a": 1},
        inputs_embedded=1,
        sent_durable_before=frozenset({"a"}),
    )
    check = evaluate(runs, FINAL).check("A2")
    assert check.ok is False and "1 re-sends" in check.detail


def test_a3_same_run_duplicates_fail():
    assert (
        evaluate(_with(_healthy(), "cycle-2", dup_same_run=4), FINAL).check("A3").ok
        is False
    )


_REC, _INC = "converge-reconcile", "converge-incremental"


def _a5_case(case):
    runs = _healthy()
    reconcile_run = next(r for r in runs if r.label == _REC)
    builders = {
        "reconcile-sends-inputs": lambda: _with(
            runs, _REC, sent_counts={"x": 2}, inputs_embedded=2
        ),
        "reconcile-plans-files": lambda: _with(runs, _REC, planned_files=12),
        "reconcile-plan-unobserved": lambda: _with(runs, _REC, planned_files=None),
        "incremental-plan-unobserved": lambda: _with(runs, _INC, planned_files=None),
        "reconcile-child-failed": lambda: _with(runs, _REC, exit_code=1),
        "incremental-missing": lambda: [r for r in runs if r.label != _INC],
        "reconcile-missing": lambda: [r for r in runs if r.label != _REC],
        "second-reconcile": lambda: runs
        + [replace(reconcile_run, label="converge-reconcile-2", reconcile=True)],
        "no-convergence-runs": lambda: [r for r in runs if r.kind != "converge"],
    }
    return builders[case]()


def test_a5_passes_for_one_observed_zero_reconcile_and_incremental():
    assert evaluate(_healthy(), FINAL).check("A5").status == "PASS"


@pytest.mark.parametrize(
    "case",
    [
        "reconcile-sends-inputs",
        "reconcile-plans-files",
        "reconcile-plan-unobserved",
        "incremental-plan-unobserved",
        "reconcile-child-failed",
        "incremental-missing",
        "reconcile-missing",
        "second-reconcile",
        "no-convergence-runs",
    ],
)
def test_a5_false_greens_fail(case):
    assert evaluate(_a5_case(case), FINAL).check("A5").status == "FAIL"


def test_skipped_required_checks_make_the_verdict_incomplete_never_success():
    later = [Check("A10", False, "needs S15", skipped=SKIPPED_NOT_IMPLEMENTED)]
    verdict = evaluate(_healthy(), FINAL, later=later)
    assert verdict.check("A10").status == SKIPPED_NOT_IMPLEMENTED
    assert verdict.reproduced is False
    assert verdict.skipped == 1
    assert (verdict.status, verdict.exit_code) == ("INCOMPLETE", 3)


def test_verdict_exit_codes():
    passed = evaluate(_healthy(), FINAL)
    assert (passed.status, passed.exit_code) == ("NOT REPRODUCED", 0)
    later = [Check("A10", False, "needs S15", skipped=SKIPPED_NOT_IMPLEMENTED)]
    failed = evaluate(
        _with(_healthy(), "cycle-2", reembedded_already_indexed=1), FINAL, later=later
    )
    assert (failed.status, failed.exit_code) == ("REPRODUCED", 1)
