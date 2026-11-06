"""The engine, not each policy, decides whether an existing index is still current.

Freshness is loop mechanics -- like the cooldown and the per-cycle bound -- so it
belongs beside them. A policy that had to remember the check is one that will
eventually forget, which is how FTS ended up never rebuilding.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

from code_indexer.server.services.golden_index_backfill import (
    GoldenIndexBackfillReconciler,
)


class _Policy:
    name = "test"
    attempt_marker = ".test_attempt"

    def __init__(self, *, present: bool, fresh: bool, raises: bool = False) -> None:
        self._present = present
        self._fresh = fresh
        self._raises = raises
        self.enqueued: List[str] = []

    def is_present(self, repo_path: Path) -> bool:
        return self._present

    def is_fresh(self, repo_path: Path) -> bool:
        if self._raises:
            raise RuntimeError("policy hook blew up")
        return self._fresh

    def eligibility(self, repo_path: Path) -> Optional[Any]:
        return {"alias": repo_path.name}

    def enqueue(self, alias: str, repo_path: Path, meta: Any) -> None:
        self.enqueued.append(alias)


def _root(tmp_path: Path, n: int = 1) -> Path:
    root = tmp_path / "golden"
    root.mkdir()
    for i in range(n):
        (root / f"repo{i}").mkdir()
    return root


def _run(root: Path, policy: _Policy):
    return GoldenIndexBackfillReconciler(root).run_cycle(
        policy, max_enqueue=10, cooldown_seconds=0
    )


def test_present_and_fresh_is_skipped(tmp_path: Path) -> None:
    p = _Policy(present=True, fresh=True)
    counts = _run(_root(tmp_path), p)
    assert counts.present == 1
    assert p.enqueued == []


def test_present_but_stale_is_rebuilt(tmp_path: Path) -> None:
    """The whole point: an index that exists but no longer matches its clone must be
    enqueued, not counted present."""
    p = _Policy(present=True, fresh=False)
    counts = _run(_root(tmp_path), p)
    assert counts.present == 0
    assert p.enqueued == ["repo0"]


def test_absent_is_rebuilt_regardless_of_freshness(tmp_path: Path) -> None:
    p = _Policy(present=False, fresh=True)
    _run(_root(tmp_path), p)
    assert p.enqueued == ["repo0"]


def test_a_raising_freshness_hook_never_aborts_the_sweep(tmp_path: Path) -> None:
    """A policy hook that throws must leave the repo counted present rather than
    take down the cycle for every other repo."""
    p = _Policy(present=True, fresh=True, raises=True)
    counts = _run(_root(tmp_path, n=3), p)
    assert counts.present == 3
    assert counts.errors == 0
    assert p.enqueued == []


def test_the_bound_still_applies_to_stale_repos(tmp_path: Path) -> None:
    """Staleness must not let a sweep fan out past max_enqueue -- that bound is what
    keeps a fleet-wide rebuild paced."""
    p = _Policy(present=True, fresh=False)
    counts = GoldenIndexBackfillReconciler(_root(tmp_path, n=5)).run_cycle(
        p, max_enqueue=2, cooldown_seconds=0
    )
    assert counts.enqueued == 2
    assert len(p.enqueued) == 2
