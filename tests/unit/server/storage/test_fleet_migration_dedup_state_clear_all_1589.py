"""
Unit tests for Story #1589's bulk "Clear All Dedup Warnings" persistence
method, `GoldenRepoMetadataSqliteBackend.clear_all_dedup_states(reason)`.

Mirrors test_fleet_migration_dedup_state_1560.py's exact fixture pattern --
a REAL SQLite database file, no mocking of the backend.

Story #1589 AC1/AC4/AC5: clears EVERY currently-active (cleared_at IS NULL)
row in one shot, leaves already-cleared rows untouched, is a no-op when
nothing is active, and returns the number of rows actually cleared.
"""

import os
import tempfile
from contextlib import closing
from typing import Any, Dict

import pytest

from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend

_DEFAULT_OUTCOME: Dict[str, Any] = {
    "duplicate_groups": 3,
    "records_before": 500,
    "records_deleted": 4,
    "winner_kept_groups": 2,
    "whole_group_deleted_groups": 1,
    "collection_total": 500,
}

_CLEAR_ALL_REASON = "manually acknowledged via Diagnostics tab"


def _record(backend, golden_alias: str, **overrides: Any) -> Dict[str, Any]:
    kwargs = {**_DEFAULT_OUTCOME, **overrides}
    return backend.record_dedup_outcome(golden_alias, **kwargs)  # type: ignore[no-any-return]


@pytest.fixture
def backend():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = os.path.join(temp_dir, "test.db")
        with closing(GoldenRepoMetadataSqliteBackend(db_path)) as be:
            be.ensure_table_exists()
            yield be


class TestClearAllDedupStatesHappyPath:
    def test_clears_every_active_row_and_returns_count(self, backend):
        _record(backend, "repo-a")
        _record(backend, "repo-b")
        _record(backend, "repo-c")

        cleared_count = backend.clear_all_dedup_states(_CLEAR_ALL_REASON)

        assert cleared_count == 3
        for alias in ("repo-a", "repo-b", "repo-c"):
            state = backend.get_dedup_state(alias)
            assert state is not None
            assert state["cleared_at"] is not None
            assert state["cleared_reason"] == _CLEAR_ALL_REASON

    def test_counts_are_not_erased_only_marked_cleared(self, backend):
        _record(backend, "click", duplicate_groups=33, records_deleted=43)

        backend.clear_all_dedup_states(_CLEAR_ALL_REASON)

        state = backend.get_dedup_state("click")
        assert state is not None
        assert state["duplicate_groups"] == 33
        assert state["records_deleted"] == 43


class TestClearAllDedupStatesSkipsAlreadyCleared:
    def test_already_cleared_row_is_not_double_counted(self, backend):
        _record(backend, "repo-a")
        _record(backend, "repo-b")
        backend.clear_dedup_state("repo-a", "successful full re-index")

        cleared_count = backend.clear_all_dedup_states(_CLEAR_ALL_REASON)

        # Only repo-b was active; repo-a was already cleared beforehand.
        assert cleared_count == 1

    def test_already_cleared_rows_reason_and_timestamp_are_unchanged(self, backend):
        _record(backend, "repo-a")
        backend.clear_dedup_state("repo-a", "successful full re-index")
        original_state = backend.get_dedup_state("repo-a")
        assert original_state is not None

        _record(backend, "repo-b")
        backend.clear_all_dedup_states(_CLEAR_ALL_REASON)

        state = backend.get_dedup_state("repo-a")
        assert state is not None
        assert state["cleared_at"] == original_state["cleared_at"]
        assert state["cleared_reason"] == "successful full re-index"


class TestClearAllDedupStatesNoOp:
    def test_returns_zero_when_nothing_active(self, backend):
        assert backend.clear_all_dedup_states(_CLEAR_ALL_REASON) == 0

    def test_does_not_raise_when_table_is_empty(self, backend):
        # Must not raise -- an empty table is a legitimate steady state.
        backend.clear_all_dedup_states(_CLEAR_ALL_REASON)


class TestClearAllDedupStatesIdempotent:
    def test_second_consecutive_call_clears_nothing(self, backend):
        _record(backend, "repo-a")
        _record(backend, "repo-b")

        first = backend.clear_all_dedup_states(_CLEAR_ALL_REASON)
        second = backend.clear_all_dedup_states(_CLEAR_ALL_REASON)

        assert first == 2
        assert second == 0


class TestClearAllDedupStatesInputValidation:
    def test_rejects_empty_reason(self, backend):
        with pytest.raises(ValueError):
            backend.clear_all_dedup_states("")
