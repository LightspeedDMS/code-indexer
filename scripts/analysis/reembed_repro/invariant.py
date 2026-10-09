"""The reproduction's verdict (pure; no I/O). Design 15.1, story S0.

Version-agnostic checks (each must FAIL on 12.83.0 and pass on the fix):

* R-a  every recovery plans no more files than were unindexed at its start;
* R-b  ``re_idx == 0`` per run: no chunk whose content was in the store at
       the run's start is sent to the provider;
* R-c  (A6-core) every non-empty disk file's content-addressed ids exist and
       are visible after the final run (duplicate-content safe);
* R-d  a SIGTERM while responses are held in flight yields: exit 75, the
       held keys are in ``pending_vectors``, no hot ``chunks.db-journal``,
       and the next run does not send them again;
* R-12 (A12-core) after every kill, the next run re-sends only keys that
       were in flight at a kill and not durable, and the first successful
       run after the kill leaves R-c satisfied.

A-assertions implementable today: A1 exact budget, A2 paid once, A3
single-flight, A5 convergence. "In flight at a kill" means sent during the
killed run and not durable (store or pending) after the child exited, so a
response the client received but never saved is in flight, never
"unanswered" (the pay-once bounded in-flight definition).

A check that a scenario does not exercise, or that depends on a mechanism
that does not exist yet, is SKIPPED and never counts as passed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

PASS, FAIL = "PASS", "FAIL"
SKIPPED_NOT_EXERCISED = "SKIPPED-NOT-EXERCISED"
SKIPPED_NOT_IMPLEMENTED = "SKIPPED-NOT-IMPLEMENTED"
#: Some runs could not be assessed and none failed: never a pass (INCOMPLETE).
SKIPPED_UNASSESSED = "SKIPPED-UNASSESSED"
#: Every candidate run was N/A (vacuously satisfied): nothing was checked.
SKIPPED_NOTHING_TO_ASSESS = "SKIPPED-NOTHING-TO-ASSESS"
YIELD_EXIT_CODE = 75


@dataclass(frozen=True)
class RunRecord:
    label: str
    kind: str  # "initial" | "sync" | "recovery" | "converge"
    interrupted: bool
    exit_code: Optional[int]
    inputs_embedded: int
    planned_files: Optional[int]
    unindexed_at_start: int
    version: str = "?"
    signal_name: Optional[str] = None
    trigger: Optional[str] = None
    dup_same_run: int = 0
    reembedded_already_indexed: int = 0
    sent_counts: Mapping[str, int] = field(default_factory=dict)
    sent_durable_before: FrozenSet[str] = frozenset()
    inflight_keys: FrozenSet[str] = frozenset()
    held_keys: FrozenSet[str] = frozenset()
    held_in_pending_after: FrozenSet[str] = frozenset()
    hot_journal_after: bool = False
    content_missing_after: Optional[int] = None
    reconcile: bool = False  # the run's command carried --reconcile
    #: |K_r - D_r|: chunk keys on disk at the run's start minus keys durable
    #: (store or pending) then; None when the harness could not compute it.
    owed_keys: Optional[int] = None
    sent_not_owed: FrozenSet[str] = frozenset()  # sent keys outside K_r - D_r
    #: Observed: did this run rewrite its metadata (its plan)? None = not observed.
    plan_written: Optional[bool] = None

    @property
    def tag(self) -> str:
        return f"{self.label}[{self.version}]"


@dataclass(frozen=True)
class FinalState:
    status: Optional[str]
    content_missing: int
    content_files: int
    missing_examples: Tuple[str, ...] = ()
    introduced_keys: int = 0


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    skipped: str = ""
    title: str = ""

    @property
    def status(self) -> str:
        return self.skipped or (PASS if self.ok else FAIL)


REPRODUCED, INCOMPLETE, NOT_REPRODUCED = "REPRODUCED", "INCOMPLETE", "NOT REPRODUCED"
#: Exit code per verdict status; a skipped required check is never a success.
EXIT_CODES = {NOT_REPRODUCED: 0, REPRODUCED: 1, INCOMPLETE: 3}


@dataclass
class Verdict:
    checks: List[Check] = field(default_factory=list)

    @property
    def reproduced(self) -> bool:
        return any(check.status == FAIL for check in self.checks)

    @property
    def skipped(self) -> int:
        return sum(1 for check in self.checks if check.skipped)

    @property
    def status(self) -> str:
        """REPRODUCED if any check failed; INCOMPLETE if none failed but some
        were skipped; NOT REPRODUCED only when every check passed."""
        if self.reproduced:
            return REPRODUCED
        if any(check.status != PASS for check in self.checks):
            return INCOMPLETE
        return NOT_REPRODUCED

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]

    def check(self, name: str) -> Check:
        for check in self.checks:
            if check.name == name:
                return check
        raise KeyError(name)


def _planning_check(runs: Sequence[RunRecord]) -> Check:
    """R-a per recovery run, then overall.

    * N/A (vacuously satisfied): killed by an ``early`` trigger, no plan
      observed, AND the harness observed that the run never rewrote its
      metadata (``plan_written is False``) -- it planned nothing. Never
      inferred from missing output alone.
    * UNASSESSED: a plan should exist (any other kill, or a completed run)
      but was not observed.
    * assessed: planned <= unindexed at the run's start, else a violation.

    Overall: any violation FAIL; else any UNASSESSED SKIPPED-UNASSESSED; else
    no assessed run SKIPPED-NOTHING-TO-ASSESS; else PASS.
    """
    title = "recovery plans only unindexed files"
    notes, failed, unassessed, assessed = [], False, 0, 0
    for run in runs:
        if run.kind != "recovery":
            continue
        if run.planned_files is None:
            killed_before_planning = (
                run.interrupted and run.trigger == "early" and run.plan_written is False
            )
            if killed_before_planning:
                notes.append(f"{run.label}: N/A (killed before planning)")
            else:
                unassessed += 1
                notes.append(f"{run.label}: UNASSESSED (plan expected, not observed)")
            continue
        assessed += 1
        if run.planned_files > run.unindexed_at_start:
            failed = True
            notes.append(
                f"{run.tag} planned {run.planned_files} > {run.unindexed_at_start} unindexed"
            )
        else:
            notes.append(
                f"{run.label} planned {run.planned_files} <= {run.unindexed_at_start}"
            )
    detail = "; ".join(notes) or "no recovery runs"
    if failed:
        return Check("R-a", False, detail, title=title)
    if unassessed:
        return Check("R-a", False, detail, skipped=SKIPPED_UNASSESSED, title=title)
    if not assessed:
        return Check(
            "R-a", False, detail, skipped=SKIPPED_NOTHING_TO_ASSESS, title=title
        )
    return Check("R-a", True, detail, title=title)


def _reindex_check(runs: Sequence[RunRecord]) -> Check:
    bad = [
        f"{r.tag} re_idx {r.reembedded_already_indexed}"
        for r in runs
        if r.reembedded_already_indexed
    ]
    return Check(
        "R-b",
        not bad,
        "; ".join(bad) or f"re_idx 0 on all {len(runs)} runs",
        title="no stored content re-embedded (re_idx == 0)",
    )


def _content_check(final: FinalState) -> Check:
    detail = f"{final.content_missing} of {final.content_files} files' content missing or hidden"
    if final.missing_examples:
        detail += f", e.g. {', '.join(final.missing_examples)}"
    return Check(
        "R-c",
        final.content_missing == 0,
        detail,
        title="final content complete (A6-core)",
    )


def _final_run_check(runs: Sequence[RunRecord], final: FinalState) -> Check:
    last = [r for r in runs if r.kind != "converge"][-1]
    ok = last.exit_code == 0 and final.status == "completed"
    return Check(
        "final run",
        ok,
        f"{last.tag} exit {last.exit_code}, status {final.status}",
        title="last refresh exits 0 with status completed",
    )


def _yield_check(runs: Sequence[RunRecord]) -> Check:
    title = "SIGTERM with responses in flight yields (exit 75, pending, no journal)"
    probes = [
        i
        for i, r in enumerate(runs)
        if r.trigger == "inflight" and r.signal_name == "SIGTERM"
    ]
    if not probes:
        return Check(
            "R-d",
            False,
            "no SIGTERM@inflight cycle in this scenario",
            skipped=SKIPPED_NOT_EXERCISED,
            title=title,
        )
    notes, ok = [], True
    for i in probes:
        run = runs[i]
        problems = []
        if not run.interrupted or not run.held_keys:
            problems.append("no request held at the SIGTERM (not deterministic)")
        if run.exit_code != YIELD_EXIT_CODE:
            problems.append(f"exit {run.exit_code} (want {YIELD_EXIT_CODE})")
        lost = run.held_keys - run.held_in_pending_after
        if lost:
            problems.append(
                f"{len(lost)} of {len(run.held_keys)} held keys not in pending"
            )
        if run.hot_journal_after:
            problems.append("hot chunks.db-journal left")
        if i + 1 >= len(runs):
            problems.append("no run after the yield")
        else:
            resent = run.held_keys & set(runs[i + 1].sent_counts)
            if resent:
                problems.append(
                    f"{len(resent)} held keys re-sent by {runs[i + 1].label}"
                )
        ok = ok and not problems
        notes.append(
            f"{run.tag}: "
            + (
                "; ".join(problems)
                if problems
                else f"exit 75, {len(run.held_keys)} held keys saved, not re-sent"
            )
        )
    return Check("R-d", ok, " | ".join(notes), title=title)


def _kill_check(runs: Sequence[RunRecord]) -> Check:
    notes, ok = [], True
    sent_before: set = set()
    lost: set = set()
    for i, run in enumerate(runs):
        if i > 0 and runs[i - 1].interrupted:
            resent = set(run.sent_counts) & sent_before
            if run.sent_durable_before:
                ok = False
                notes.append(
                    f"{run.tag} re-sent {len(run.sent_durable_before)} keys durable at its start"
                )
            unexplained = resent - lost
            if unexplained:
                ok = False
                notes.append(
                    f"{run.tag} re-sent {len(unexplained)} keys not in flight at any kill"
                )
        sent_before |= set(run.sent_counts)
        lost |= set(run.inflight_keys)
    judged: Set[str] = set()  # a recovery run shared by several kills is judged once
    for i, run in enumerate(runs):
        if not run.interrupted:
            continue
        recovery = next(
            (r for r in runs[i + 1 :] if not r.interrupted and r.exit_code == 0), None
        )
        if recovery is None:
            ok = False
            notes.append(f"no successful recovery run after {run.label}")
        elif recovery.label in judged:
            continue
        else:
            judged.add(recovery.label)
        if recovery is not None and recovery.content_missing_after != 0:
            ok = False
            notes.append(
                f"after {recovery.tag}: {recovery.content_missing_after} files' content missing"
            )
    kills = sum(1 for r in runs if r.interrupted)
    return Check(
        "R-12",
        ok,
        "; ".join(notes) or f"{kills} kills, re-sends within in-flight keys",
        title="after every kill: re-sends within in-flight keys, then R-c",
    )


def _budget_check(runs: Sequence[RunRecord], final: FinalState) -> Check:
    inflight_items = sum(
        sum(r.sent_counts.get(k, 0) for k in r.inflight_keys)
        for r in runs
        if r.interrupted
    )
    limit = final.introduced_keys + inflight_items
    embedded = sum(r.inputs_embedded for r in runs)
    detail = (
        f"embedded {embedded}, limit {limit} = introduced keys {final.introduced_keys}"
        f" + in-flight items {inflight_items}"
    )
    return Check("A1", embedded <= limit, detail, title="exact budget")


def _paid_once_check(runs: Sequence[RunRecord]) -> Check:
    lost: Dict[str, bool] = {}
    bad: Dict[str, int] = {}
    for run in runs:
        for key in run.sent_counts:
            if key in lost and (key in run.sent_durable_before or not lost[key]):
                bad[run.tag] = bad.get(run.tag, 0) + 1
        for key in run.sent_counts:
            lost[key] = run.interrupted and key in run.inflight_keys
    detail = "; ".join(f"{tag} {n} re-sends" for tag, n in bad.items())
    return Check(
        "A2",
        not bad,
        detail or "every re-send was in flight at a kill",
        title="paid once (re-sends only after in-flight loss)",
    )


def _single_flight_check(runs: Sequence[RunRecord]) -> Check:
    bad = [f"{r.tag} {r.dup_same_run}" for r in runs if r.dup_same_run]
    return Check(
        "A3",
        not bad,
        "; ".join(bad) or "dup_same_run 0 on every run",
        title="single-flight",
    )


def _convergence_check(runs: Sequence[RunRecord]) -> Check:
    title = "one more reconcile and incremental each exit 0, plan 0 (observed), send 0"
    converge = [r for r in runs if r.kind == "converge"]
    notes, ok = [], True
    for reconcile, kind in ((True, "reconcile"), (False, "incremental")):
        matching = [r for r in converge if r.reconcile is reconcile]
        if len(matching) != 1:
            ok = False
            notes.append(f"{len(matching)} {kind} convergence runs (want exactly 1)")
            continue
        run = matching[0]
        good = (
            run.exit_code == 0 and run.planned_files == 0 and run.inputs_embedded == 0
        )
        ok = ok and good
        plan = "unobserved" if run.planned_files is None else run.planned_files
        notes.append(
            f"{run.tag} exit {run.exit_code}, planned {plan}, sent {run.inputs_embedded}"
        )
    return Check("A5", ok, "; ".join(notes), title=title)


def _content_key_check(runs: Sequence[RunRecord]) -> Check:
    """R-a content: a run sends only keys on disk at its start and not
    durable then (K_r - D_r), and no more inputs than such keys (no provider
    faults are injected, so a key needs one send). Duplicate copies of
    stored content owe nothing, which the path-based R-a cannot see.

    Never N/A: the bound comes from the disk and the durable state at the
    run's start, which exist for every run however early it was killed, so a
    run without a bound is UNASSESSED (never a pass)."""
    title = "runs send only keys on disk and not durable at their start"
    notes, ok, assessed = [], True, 0
    for run in runs:
        if run.owed_keys is None:
            notes.append(f"{run.label}: UNASSESSED (no content bound)")
            continue
        assessed += 1
        if run.sent_not_owed:
            ok = False
            notes.append(f"{run.tag} sent {len(run.sent_not_owed)} keys not owed")
        if run.inputs_embedded > run.owed_keys:
            ok = False
            notes.append(
                f"{run.tag} sent {run.inputs_embedded} inputs > {run.owed_keys} owed keys"
            )
    detail = "; ".join(notes) or f"{assessed} runs sent only owed keys"
    if ok and assessed < len(runs):  # no violation, but some runs had no bound
        return Check(
            "R-a content", False, detail, skipped=SKIPPED_UNASSESSED, title=title
        )
    return Check("R-a content", ok, detail, title=title)


def evaluate(
    runs: Sequence[RunRecord], final: FinalState, later: Sequence[Check] = ()
) -> Verdict:
    if not runs:
        raise ValueError("no runs to evaluate")
    return Verdict(
        checks=[
            _planning_check(runs),
            _content_key_check(runs),
            _reindex_check(runs),
            _content_check(final),
            _final_run_check(runs, final),
            _yield_check(runs),
            _kill_check(runs),
            _budget_check(runs, final),
            _paid_once_check(runs),
            _single_flight_check(runs),
            _convergence_check(runs),
            *later,
        ]
    )
