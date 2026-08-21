"""EVO-66445: unit tests for the DRY golden-repo index backfill engine."""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

from code_indexer.server.services.golden_index_backfill import (
    BackfillCounts,
    GoldenIndexBackfillReconciler,
)


class _FakePolicy:
    """Records enqueues; present/eligible driven by per-alias sets."""

    name = "fake"
    attempt_marker = ".fake_backfill_attempt"

    def __init__(self, present: set[str], eligible: dict[str, Any]) -> None:
        self._present = present
        self._eligible = eligible
        self.enqueued: List[str] = []

    def is_present(self, repo_path: Path) -> bool:
        return repo_path.name in self._present

    def eligibility(self, repo_path: Path) -> Optional[Any]:
        return self._eligible.get(repo_path.name)

    def enqueue(self, alias: str, repo_path: Path, meta: Any) -> None:
        self.enqueued.append(alias)


def _golden(tmp_path: Path, names: List[str]) -> Path:
    root = tmp_path / "golden"
    root.mkdir()
    for n in names:
        (root / n).mkdir()
    return root


def test_enqueues_only_eligible_missing_repos(tmp_path: Path) -> None:
    root = _golden(tmp_path, ["a", "b", "c", "d"])
    policy = _FakePolicy(
        present={"b"}, eligible={"a": {"m": 1}, "c": {"m": 1}}
    )  # d not eligible
    counts = GoldenIndexBackfillReconciler(root).run_cycle(
        policy, max_enqueue=10, cooldown_seconds=0
    )
    assert sorted(policy.enqueued) == ["a", "c"]
    assert counts.scanned == 4
    assert counts.present == 1  # b
    assert counts.not_eligible == 1  # d
    assert counts.enqueued == 2


def test_respects_max_enqueue_bound(tmp_path: Path) -> None:
    root = _golden(tmp_path, ["a", "b", "c", "d"])
    policy = _FakePolicy(present=set(), eligible={x: {} for x in ("a", "b", "c", "d")})
    counts = GoldenIndexBackfillReconciler(root).run_cycle(
        policy, max_enqueue=2, cooldown_seconds=0
    )
    assert counts.enqueued == 2
    assert len(policy.enqueued) == 2  # capped, remaining drain next cycle


def test_writes_attempt_marker_and_honors_cooldown(tmp_path: Path) -> None:
    root = _golden(tmp_path, ["a"])
    policy = _FakePolicy(present=set(), eligible={"a": {}})
    eng = GoldenIndexBackfillReconciler(root)
    c1 = eng.run_cycle(policy, max_enqueue=10, cooldown_seconds=3600)
    assert c1.enqueued == 1
    assert (
        root / "a" / ".code-indexer" / ".fake_backfill_attempt"
    ).exists()  # marker written
    # second cycle within cooldown: skipped, not re-enqueued
    c2 = eng.run_cycle(policy, max_enqueue=10, cooldown_seconds=3600)
    assert c2.enqueued == 0
    assert c2.cooldown_skipped == 1
    assert policy.enqueued == ["a"]  # still just the one


def test_cooldown_zero_disables_throttle(tmp_path: Path) -> None:
    root = _golden(tmp_path, ["a"])
    policy = _FakePolicy(present=set(), eligible={"a": {}})
    eng = GoldenIndexBackfillReconciler(root)
    eng.run_cycle(policy, max_enqueue=10, cooldown_seconds=0)
    eng.run_cycle(policy, max_enqueue=10, cooldown_seconds=0)
    assert policy.enqueued == ["a", "a"]  # no cooldown -> re-enqueued each cycle


def test_a_failing_repo_never_aborts_the_cycle(tmp_path: Path) -> None:
    root = _golden(tmp_path, ["boom", "ok"])

    class _Boom(_FakePolicy):
        def enqueue(self, alias: str, repo_path: Path, meta: Any) -> None:
            if alias == "boom":
                raise RuntimeError("enqueue exploded")
            super().enqueue(alias, repo_path, meta)

    policy = _Boom(present=set(), eligible={"boom": {}, "ok": {}})
    counts = GoldenIndexBackfillReconciler(root).run_cycle(
        policy, max_enqueue=10, cooldown_seconds=0
    )
    assert counts.errors == 1
    assert policy.enqueued == ["ok"]  # the good repo still enqueued
    assert counts.enqueued == 1


def test_missing_golden_dir_is_a_noop(tmp_path: Path) -> None:
    counts = GoldenIndexBackfillReconciler(tmp_path / "nope").run_cycle(
        _FakePolicy(set(), {}), max_enqueue=10, cooldown_seconds=0
    )
    assert counts == BackfillCounts()
