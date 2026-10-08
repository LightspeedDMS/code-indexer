"""Properties of the fetch batch planner.

The planner exists because peak client memory tracks the biggest batch's BYTES.
Three things have to hold or it does not do its job:

* no batch exceeds the budget, except a single file that cannot fit anywhere;
* every path is fetched exactly once, in order, so results zip back;
* a repository of tiny files does not become one enormous request.

The first is the whole point; the second is what a caller relies on; the third
is the bound the byte budget alone does not give.
"""

from __future__ import annotations

import random
from typing import Dict, List, Mapping, Optional

import pytest

from code_indexer.indexing.fetch_planner import (
    DEFAULT_FETCH_BUDGET_BYTES,
    DEFAULT_FETCH_BUDGET_FILES,
    plan_fetch_batches,
)


def sizer(sizes: Mapping[str, Optional[int]]):
    return lambda path: sizes.get(path)


def plan(sizes: Mapping[str, Optional[int]], **kw) -> List[List[str]]:
    return list(plan_fetch_batches(list(sizes), sizer(sizes), **kw))


# ── the budget ─────────────────────────────────────────────────────────────


def test_a_batch_never_exceeds_the_byte_budget() -> None:
    sizes = {f"f{i}.py": 300 for i in range(20)}
    batches = plan(sizes, budget_bytes=1000)
    for batch in batches:
        assert sum(sizes[p] for p in batch) <= 1000


def test_files_are_packed_rather_than_sent_one_at_a_time() -> None:
    """A planner that emitted singletons would satisfy the budget and nothing else.

    Per-file fetching is what this exists to avoid: 500 files cost 501 TCP
    connections that way, against 21 when batched.
    """
    sizes = {f"f{i}.py": 100 for i in range(50)}
    batches = plan(sizes, budget_bytes=1000)
    assert len(batches) == 5, f"expected 5 full batches, got {len(batches)}"
    assert all(len(b) == 10 for b in batches)


def test_a_batch_that_would_exactly_fill_the_budget_is_allowed() -> None:
    """An off-by-one here silently halves the batch size everywhere."""
    sizes = {f"f{i}.py": 250 for i in range(4)}
    batches = plan(sizes, budget_bytes=1000)
    assert batches == [list(sizes)], "an exactly-full batch was split"


def test_one_byte_over_the_budget_starts_a_new_batch() -> None:
    sizes = {"a": 600, "b": 401}
    assert plan(sizes, budget_bytes=1000) == [["a"], ["b"]]


# ── files that cannot fit ──────────────────────────────────────────────────


def test_an_oversized_file_travels_alone_rather_than_being_dropped() -> None:
    """Bounding a batch is not the same decision as what is indexable.

    The server admits an oversized file on its own for the same reason: it makes
    progress instead of deadlocking against a budget it can never satisfy.
    """
    sizes = {"small.py": 10, "huge.bin": 99_000, "other.py": 10}
    batches = plan(sizes, budget_bytes=1000)
    assert ["huge.bin"] in batches
    assert all(len(b) == 1 for b in batches if "huge.bin" in b)


def test_an_oversized_file_does_not_swallow_the_batch_being_built() -> None:
    """The accumulated batch is flushed FIRST, so order survives."""
    sizes = {"a.py": 10, "b.py": 10, "huge.bin": 99_000, "c.py": 10}
    assert plan(sizes, budget_bytes=1000) == [["a.py", "b.py"], ["huge.bin"], ["c.py"]]


def test_a_file_of_unknown_size_travels_alone() -> None:
    """There is no safe way to add an unmeasurable file to a measured batch.

    A manifest MISS is the usual cause: the file appeared after the pass began.
    A lone request is still bounded -- by the server's own per-file cap.
    """
    sizes: Dict[str, Optional[int]] = {"a.py": 10, "mystery.py": None, "b.py": 10}
    assert plan(sizes, budget_bytes=1000) == [["a.py"], ["mystery.py"], ["b.py"]]


def test_a_file_exactly_at_the_budget_travels_alone() -> None:
    """At the boundary it cannot share with anything, so it does not pretend to."""
    sizes = {"a.py": 10, "exact.bin": 1000}
    assert plan(sizes, budget_bytes=1000) == [["a.py"], ["exact.bin"]]


# ── the file-count bound ───────────────────────────────────────────────────


def test_a_repository_of_tiny_files_does_not_become_one_huge_request() -> None:
    """The byte budget alone does not bound the REQUEST, which lists every path.

    Ten thousand one-byte files fit in any byte budget, and would put ten
    thousand paths in a single JSON body.
    """
    sizes = {f"f{i}.py": 1 for i in range(2500)}
    batches = plan(sizes, budget_bytes=DEFAULT_FETCH_BUDGET_BYTES, budget_files=1000)
    assert max(len(b) for b in batches) <= 1000
    assert len(batches) == 3


def test_both_bounds_apply_together() -> None:
    sizes = {f"f{i}.py": 100 for i in range(30)}
    batches = plan(sizes, budget_bytes=500, budget_files=3)
    for batch in batches:
        assert len(batch) <= 3
        assert sum(sizes[p] for p in batch) <= 500
    assert max(len(b) for b in batches) == 3, "the file bound never bit"


# ── what the caller relies on ──────────────────────────────────────────────


def test_every_path_appears_exactly_once_in_order() -> None:
    sizes = {f"f{i}.py": (i % 7) * 200 for i in range(60)}
    batches = plan(sizes, budget_bytes=1000)
    assert [p for b in batches for p in b] == list(sizes)


def test_no_batch_is_ever_empty() -> None:
    """An empty batch would be a request for nothing, which the plugin refuses."""
    sizes = {"huge1": 99_000, "huge2": 99_000, "a": 1}
    for batch in plan(sizes, budget_bytes=1000):
        assert batch


def test_an_empty_input_plans_nothing() -> None:
    assert plan({}) == []


def test_a_single_file_is_one_batch() -> None:
    assert plan({"a.py": 10}) == [["a.py"]]


# ── guard rails ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [0, -1])
def test_a_non_positive_byte_budget_is_rejected(bad: int) -> None:
    """Zero would make every file "oversized" and restore per-file fetching."""
    with pytest.raises(ValueError, match="budget_bytes"):
        list(plan_fetch_batches(["a"], lambda p: 1, budget_bytes=bad))


@pytest.mark.parametrize("bad", [0, -1])
def test_a_non_positive_file_budget_is_rejected(bad: int) -> None:
    with pytest.raises(ValueError, match="budget_files"):
        list(plan_fetch_batches(["a"], lambda p: 1, budget_files=bad))


def test_the_defaults_are_the_measured_ones() -> None:
    """8 MB per request is the figure the measurements landed on.

    Changing it is allowed, but it should be a decision rather than a drift, so
    the number is pinned where the reasoning is written down.
    """
    assert DEFAULT_FETCH_BUDGET_BYTES == 8 * 1024 * 1024
    assert DEFAULT_FETCH_BUDGET_FILES == 1000


# ── the property, over shapes nobody would think to write ──────────────────


@pytest.mark.parametrize("seed", range(25))
def test_the_invariants_hold_for_random_repository_shapes(seed: int) -> None:
    """Real repositories have a long tail: mostly small, a few enormous.

    Hand-written cases cover the boundaries that occurred to me. This covers the
    ones that did not.
    """
    rng = random.Random(seed)
    budget = rng.choice([1_000, 64_000, 8 * 1024 * 1024])
    sizes: Dict[str, Optional[int]] = {}
    for i in range(rng.randint(0, 200)):
        roll = rng.random()
        if roll < 0.05:
            value: Optional[int] = None
        elif roll < 0.10:
            value = rng.randint(budget, budget * 4)
        else:
            value = rng.randint(0, budget // 4)
        sizes[f"f{i:04d}"] = value

    batches = plan(sizes, budget_bytes=budget, budget_files=50)

    assert [p for b in batches for p in b] == list(sizes), "order or coverage lost"
    for batch in batches:
        assert batch, "empty batch"
        assert len(batch) <= 50
        known: List[int] = [s for s in (sizes[p] for p in batch) if s is not None]
        if len(batch) == 1:
            continue  # a lone file is allowed to exceed, by construction
        assert all(s is not None for s in [sizes[p] for p in batch]), (
            "an unknown-size file was batched with others"
        )
        assert sum(known) <= budget, f"batch of {len(batch)} exceeded {budget}"
