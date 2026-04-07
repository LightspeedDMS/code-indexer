"""Tests for _make_offset_callback — dual-provider temporal indexing progress fix.

Bug #643: When two embedding providers run sequentially for temporal indexing,
the second provider's progress resets to 0, causing the server-side
ProgressPhaseAllocator to clamp progress at ~99% for the entire second run.

Fix: _make_offset_callback wraps any progress_callback to emit monotonically
increasing (offset_current, offset_total) pairs across N sequential providers.
"""

from __future__ import annotations

import sys
import os
import pytest

# Ensure src is on path for direct import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../src"))

from code_indexer.cli import _make_offset_callback  # type: ignore[attr-defined]

# Named constants — derive all expectations from these
TOTAL = 100
PROVIDERS = 2
OFFSET_TOTAL = PROVIDERS * TOTAL  # 200
STEPS = TOTAL + 1  # 101  (0..100 inclusive)


@pytest.fixture
def collector():
    """Return (base_cb, sink) where sink accumulates (current, total, kwargs) tuples."""
    sink: list[tuple[int, object, dict]] = []

    def base_cb(current, total, **kwargs):
        sink.append((current, total, kwargs))

    return base_cb, sink


class TestSingleProviderIsIdentity:
    """Single-provider case must be mathematically identical to no wrapper."""

    def test_start(self, collector):
        """current=0, total=100 → (0, 100)."""
        base_cb, sink = collector
        _make_offset_callback(base_cb, 0, 1)(0, TOTAL)
        assert sink[0][:2] == (0, TOTAL)

    def test_midpoint(self, collector):
        """_make_offset_callback(cb, 0, 1) with current=50, total=100 → cb(50, 100)."""
        base_cb, sink = collector
        _make_offset_callback(base_cb, 0, 1)(50, TOTAL)
        assert sink[0][:2] == (50, TOTAL)

    def test_end(self, collector):
        """current=100, total=100 → (100, 100)."""
        base_cb, sink = collector
        _make_offset_callback(base_cb, 0, 1)(TOTAL, TOTAL)
        assert sink[0][:2] == (TOTAL, TOTAL)


class TestProvider0Of2:
    """Provider 0 of 2: emits current into first half of offset range."""

    def test_start(self, collector):
        """current=0 → (0, 200)."""
        base_cb, sink = collector
        _make_offset_callback(base_cb, 0, PROVIDERS)(0, TOTAL)
        assert sink[0][:2] == (0, OFFSET_TOTAL)

    def test_midpoint(self, collector):
        """current=50 → (50, 200)."""
        base_cb, sink = collector
        _make_offset_callback(base_cb, 0, PROVIDERS)(50, TOTAL)
        assert sink[0][:2] == (50, OFFSET_TOTAL)

    def test_end(self, collector):
        """current=100 → (100, 200)."""
        base_cb, sink = collector
        _make_offset_callback(base_cb, 0, PROVIDERS)(TOTAL, TOTAL)
        assert sink[0][:2] == (TOTAL, OFFSET_TOTAL)


class TestProvider1Of2:
    """Provider 1 of 2: picks up exactly at midpoint and reaches 100%."""

    def test_start(self, collector):
        """current=0 → (100, 200) — second provider begins where first ended."""
        base_cb, sink = collector
        _make_offset_callback(base_cb, 1, PROVIDERS)(0, TOTAL)
        assert sink[0][:2] == (TOTAL, OFFSET_TOTAL)

    def test_midpoint(self, collector):
        """current=50 → (150, 200)."""
        base_cb, sink = collector
        _make_offset_callback(base_cb, 1, PROVIDERS)(50, TOTAL)
        assert sink[0][:2] == (TOTAL + 50, OFFSET_TOTAL)

    def test_end(self, collector):
        """current=100 → (200, 200) — reaches 100% of overall progress."""
        base_cb, sink = collector
        _make_offset_callback(base_cb, 1, PROVIDERS)(TOTAL, TOTAL)
        assert sink[0][:2] == (OFFSET_TOTAL, OFFSET_TOTAL)


class TestZeroTotalGuard:
    """Zero or None total must pass through without crashing or dividing by zero."""

    def test_zero_total(self, collector):
        """current=0, total=0 → (0, 0) — no crash."""
        base_cb, sink = collector
        _make_offset_callback(base_cb, 0, PROVIDERS)(0, 0)
        assert sink[0][:2] == (0, 0)

    def test_none_total(self, collector):
        """current=0, total=None → (0, None) — no crash."""
        base_cb, sink = collector
        _make_offset_callback(base_cb, 0, PROVIDERS)(0, None)
        assert sink[0][:2] == (0, None)


class TestMonotonicDualProviderSequence:
    """Full dual-provider sequence emits strictly non-decreasing offset_current."""

    def test_full_sequence_is_monotonic(self, collector):
        """Simulate provider 0 then provider 1 running 0→TOTAL each.

        Asserts:
        - offset_current is non-decreasing throughout entire sequence
        - Total emissions == STEPS * PROVIDERS
        - Final emission is (OFFSET_TOTAL, OFFSET_TOTAL)
        """
        base_cb, sink = collector
        cb0 = _make_offset_callback(base_cb, 0, PROVIDERS)
        cb1 = _make_offset_callback(base_cb, 1, PROVIDERS)

        for i in range(STEPS):
            cb0(i, TOTAL)
        for i in range(STEPS):
            cb1(i, TOTAL)

        assert len(sink) == STEPS * PROVIDERS, (
            f"Expected {STEPS * PROVIDERS} emissions, got {len(sink)}"
        )

        offset_currents = [r[0] for r in sink]
        for idx in range(1, len(offset_currents)):
            assert offset_currents[idx] >= offset_currents[idx - 1], (
                f"Progress went backwards at step {idx}: "
                f"{offset_currents[idx - 1]} → {offset_currents[idx]}"
            )

        assert sink[-1][:2] == (OFFSET_TOTAL, OFFSET_TOTAL), (
            f"Final emission must be ({OFFSET_TOTAL}, {OFFSET_TOTAL}), got {sink[-1][:2]}"
        )

    def test_no_gap_between_providers(self, collector):
        """Provider 0 last step and provider 1 first step both emit (TOTAL, OFFSET_TOTAL)."""
        base_cb, sink = collector
        cb0 = _make_offset_callback(base_cb, 0, PROVIDERS)
        cb1 = _make_offset_callback(base_cb, 1, PROVIDERS)

        cb0(TOTAL, TOTAL)  # Provider 0 last step
        cb1(0, TOTAL)  # Provider 1 first step — the bug scenario

        assert sink[0][:2] == (TOTAL, OFFSET_TOTAL), (
            f"Provider 0 end: expected ({TOTAL}, {OFFSET_TOTAL}), got {sink[0][:2]}"
        )
        assert sink[1][:2] == (TOTAL, OFFSET_TOTAL), (
            f"Provider 1 start: expected ({TOTAL}, {OFFSET_TOTAL}), got {sink[1][:2]}"
        )

    def test_kwargs_are_forwarded(self, collector):
        """Extra kwargs (info, path, etc.) must reach the base callback unchanged."""
        base_cb, sink = collector
        wrapped = _make_offset_callback(base_cb, 0, PROVIDERS)
        wrapped(10, TOTAL, info="processing commit abc123", item_type="commits")

        assert sink[0][2] == {
            "info": "processing commit abc123",
            "item_type": "commits",
        }, f"kwargs not forwarded correctly: {sink[0][2]}"
