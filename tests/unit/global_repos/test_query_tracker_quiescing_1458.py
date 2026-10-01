"""QueryTracker quiescing gate -- Story #1458 round-6 Codex HIGH finding
#6b (the real admission barrier, not just drain-then-rename reordering).

Codex traced the exact residual race left by round-5's drain-then-rename
reordering: drain observes zero refcount -> a NEW query resolves the
still-active metadata and increments its OWN refcount -> rename proceeds
anyway (drain already returned) -> the query then reads a path that's
already gone.

Fix: mark the repo non-queryable FIRST (atomically, via this quiescing
gate), have query admission (track_activated_repo_query in
deactivation_query_drain.py) re-check that state when acquiring its
refcount, THEN drain, THEN rename. This module tests the QueryTracker-
level primitive; the admission-refusal wiring is tested separately in
test_deactivation_query_drain_1458.py.
"""

from code_indexer.global_repos.query_tracker import QueryTracker


class TestQueryTrackerQuiescingGate:
    def test_is_quiescing_false_by_default(self) -> None:
        tracker = QueryTracker()
        assert tracker.is_quiescing("/some/path") is False

    def test_mark_quiescing_makes_is_quiescing_true(self) -> None:
        tracker = QueryTracker()
        tracker.mark_quiescing("/some/path")
        assert tracker.is_quiescing("/some/path") is True

    def test_clear_quiescing_makes_is_quiescing_false_again(self) -> None:
        tracker = QueryTracker()
        tracker.mark_quiescing("/some/path")
        tracker.clear_quiescing("/some/path")
        assert tracker.is_quiescing("/some/path") is False

    def test_clear_quiescing_on_never_marked_path_is_a_noop(self) -> None:
        tracker = QueryTracker()
        tracker.clear_quiescing("/never/marked")  # must not raise
        assert tracker.is_quiescing("/never/marked") is False

    def test_quiescing_is_scoped_per_path(self) -> None:
        tracker = QueryTracker()
        tracker.mark_quiescing("/path/a")
        assert tracker.is_quiescing("/path/a") is True
        assert tracker.is_quiescing("/path/b") is False


class TestQueryTrackerAtomicAdmission:
    """Round-8 HIGH finding (Codex empirical reproduction): the previous
    admission barrier composed two SEPARATE lock-acquiring calls --
    is_quiescing() then increment_ref() -- leaving a TOCTOU window (proven
    in test_deactivation_query_drain_1458.py's
    TestTrackActivatedRepoQueryAtomicAdmissionRace). This single method
    performs the check and the increment inside ONE critical section."""

    def test_returns_true_and_increments_when_not_quiescing(self) -> None:
        tracker = QueryTracker()
        admitted = tracker.try_increment_ref_if_not_quiescing("/some/path")
        assert admitted is True
        assert tracker.get_ref_count("/some/path") == 1

    def test_returns_false_and_does_not_increment_when_quiescing(self) -> None:
        tracker = QueryTracker()
        tracker.mark_quiescing("/some/path")
        admitted = tracker.try_increment_ref_if_not_quiescing("/some/path")
        assert admitted is False
        assert tracker.get_ref_count("/some/path") == 0
