"""Tests for the shared per-process budget helper.

A node budget applied per uvicorn worker process is a budget multiplied by the
worker count. On tools-* that is x2 per pod and x12 fleet-wide, which is how a
"5 concurrent jobs" setting becomes 60 threads against one NFS mount.

The division itself is three characters of arithmetic; everything interesting is
in the edges -- an unknown worker count, a budget smaller than the worker count,
and a detector that must never raise at import time.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_indexer.utils.worker_budget import (
    coerce_worker_count,
    detect_worker_count,
    per_process_budget,
    read_config_json_workers,
)


# ──────────────────────────── per_process_budget ─────────────────────────────


@pytest.mark.parametrize(
    "budget,workers,expected",
    [
        (4, 2, 2),
        (4, 4, 1),
        (8, 2, 4),
        (4, 1, 4),
        (5, 2, 2),
    ],
)
def test_divides_the_node_budget_across_workers(budget, workers, expected) -> None:
    assert per_process_budget(budget, workers) == expected


@pytest.mark.parametrize("workers", [8, 16, 100])
def test_floors_at_one_so_a_pool_is_never_empty(workers: int) -> None:
    """A bound of 0 is not a smaller bound, it is a deadlock.

    A zero-size worker pool accepts jobs and runs none of them; a zero refresh
    budget means no repo is ever refreshed. Both look like a hang, not a limit.
    """
    assert per_process_budget(4, workers) == 1


@pytest.mark.parametrize("workers", [0, -1, -100])
def test_a_nonsensical_worker_count_divides_by_nothing(workers: int) -> None:
    """Unknown or absurd worker counts must not shrink the budget.

    The detector answers 1 when it cannot tell, but a caller passing a stored
    value could still hand us 0. Dividing by it would raise; treating it as
    "many" would silently gut the pool. Treat it as one.
    """
    assert per_process_budget(4, workers) == 4


def test_a_budget_below_the_worker_count_still_yields_one() -> None:
    assert per_process_budget(1, 12) == 1
    assert per_process_budget(0, 2) == 1


# ──────────────────────────── coerce_worker_count ────────────────────────────


def test_bool_is_rejected_even_though_it_is_an_int() -> None:
    """`True` would otherwise resolve to "1 worker" and divide nothing."""
    assert coerce_worker_count(True) is None
    assert coerce_worker_count(False) is None


@pytest.mark.parametrize("value", ["2", 2.0, None, [], {}])
def test_non_int_values_are_not_worker_counts(value) -> None:
    assert coerce_worker_count(value) is None


@pytest.mark.parametrize("value,expected", [(1, 1), (2, 2), (64, 64)])
def test_positive_ints_pass_through(value: int, expected: int) -> None:
    assert coerce_worker_count(value) == expected


@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_ints_are_rejected(value: int) -> None:
    assert coerce_worker_count(value) is None


# ──────────────────────────── detect_worker_count ────────────────────────────


def test_takes_the_larger_when_sources_disagree() -> None:
    """The larger count yields the smaller per-process bound.

    Guessing high costs a queued job; guessing low costs the oversubscription
    this helper exists to remove. Only one of those is recoverable.
    """
    assert detect_worker_count(sources=(lambda: 2, lambda: 4)) == 4
    assert detect_worker_count(sources=(lambda: 4, lambda: 2)) == 4


def test_defaults_to_one_when_every_source_is_silent() -> None:
    """Unknown means "do not divide" -- i.e. exactly today's behaviour."""
    assert detect_worker_count(sources=(lambda: None, lambda: None)) == 1


def test_survives_a_raising_source() -> None:
    """This runs at import in at least one caller; it must never raise."""

    def _boom():
        raise RuntimeError("no systemd here")

    assert detect_worker_count(sources=(_boom, lambda: 3)) == 3
    assert detect_worker_count(sources=(_boom, _boom)) == 1


def test_a_source_returning_garbage_is_ignored_not_trusted() -> None:
    assert detect_worker_count(sources=(lambda: "4", lambda: 2)) == 2
    assert detect_worker_count(sources=(lambda: True, lambda: None)) == 1


# ─────────────────────────── read_config_json_workers ────────────────────────


def test_reads_workers_from_the_server_data_dir(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.json").write_text(json.dumps({"workers": 2}))
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    assert read_config_json_workers() == 2


def test_extra_dir_envs_are_searched_too(tmp_path: Path, monkeypatch) -> None:
    """xray passes its own cache-dir env; the shared helper must not hardcode it."""
    monkeypatch.delenv("CIDX_SERVER_DATA_DIR", raising=False)
    (tmp_path / "config.json").write_text(json.dumps({"workers": 3}))
    monkeypatch.setenv("SOME_OTHER_DIR", str(tmp_path))
    assert read_config_json_workers(extra_dir_envs=("SOME_OTHER_DIR",)) == 3


@pytest.mark.parametrize("body", ["{not json", json.dumps({"workers": 0}), "{}"])
def test_malformed_or_useless_config_is_unknown_not_fatal(
    tmp_path: Path, monkeypatch, body: str
) -> None:
    (tmp_path / "config.json").write_text(body)
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "nonexistent-home"))
    assert read_config_json_workers() is None


def test_missing_file_is_unknown(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "nonexistent-home"))
    assert read_config_json_workers() is None
