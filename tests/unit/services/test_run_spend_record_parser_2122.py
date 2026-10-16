"""Story #2122 parent side: ``parse_run_spend_record`` reads the indexing
child's final ``--progress-json`` run-spend record from its stdout lines."""

from __future__ import annotations

import json

import pytest

from code_indexer.services.progress_phase_allocator import (
    emit_run_spend_json,
    parse_run_spend_record,
)


def _record(**fields) -> str:
    return json.dumps({"record": "run_spend", **fields}) + "\n"


def _progress(current: int, total: int, info: str) -> str:
    return json.dumps({"current": current, "total": total, "info": info}) + "\n"


def test_valid_record_is_read() -> None:
    lines = [
        _progress(1, 2, "1/2 files"),
        "Indexing complete!\n",
        _record(inputs_sent=12, attempts=3),
    ]
    assert parse_run_spend_record(lines) == {"inputs_sent": 12, "attempts": 3}


def test_zero_is_a_record_not_an_absence() -> None:
    assert parse_run_spend_record([_record(inputs_sent=0, attempts=0)]) == {
        "inputs_sent": 0,
        "attempts": 0,
    }


def test_absent_record_is_none() -> None:
    """An older or signal-killed child prints none: "not metered"."""
    lines = [_progress(0, 0, "0/0 files (nothing to index)"), "not json\n", "\n"]
    assert parse_run_spend_record(lines) is None
    assert parse_run_spend_record([]) is None


def test_record_followed_by_other_output_is_still_read() -> None:
    """Output after the record (e.g. a later phase's progress lines in the
    same buffer) does not hide it."""
    lines = [
        _record(inputs_sent=5, attempts=2),
        _progress(1, 10, "1/10 commits"),
        _progress(10, 10, "10/10 commits"),
    ]
    assert parse_run_spend_record(lines) == {"inputs_sent": 5, "attempts": 2}


def test_last_record_wins() -> None:
    lines = [_record(inputs_sent=1, attempts=1), _record(inputs_sent=9, attempts=4)]
    assert parse_run_spend_record(lines) == {"inputs_sent": 9, "attempts": 4}


@pytest.mark.parametrize(
    "fields",
    [
        {"inputs_sent": 1},
        {"attempts": 1},
        {"inputs_sent": "1", "attempts": 1},
        {"inputs_sent": 1, "attempts": None},
        {"inputs_sent": True, "attempts": 1},
        {"inputs_sent": 1.5, "attempts": 1},
        {"inputs_sent": -1, "attempts": 0},
        {"inputs_sent": 0, "attempts": -3},
    ],
)
def test_malformed_record_raises(fields) -> None:
    with pytest.raises(ValueError, match="malformed run_spend record"):
        parse_run_spend_record([_record(**fields)])


def test_reads_what_the_child_emits(capsys) -> None:
    emit_run_spend_json(42, 6)
    out = capsys.readouterr().out
    assert parse_run_spend_record(out.splitlines(keepends=True)) == {
        "inputs_sent": 42,
        "attempts": 6,
    }
