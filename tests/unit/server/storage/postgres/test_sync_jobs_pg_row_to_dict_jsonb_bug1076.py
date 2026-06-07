"""
Unit tests for Bug #1076: SyncJobsPostgresBackend._row_to_dict() calls
json.loads() directly on JSONB columns instead of using a _json_col() helper.

In psycopg3, JSONB columns are automatically deserialized to Python dicts/lists.
Calling json.loads() on an already-deserialized dict raises:
    TypeError: the JSON object must be str, bytes or bytearray, not dict

The fix: replace json.loads(row[N]) with _json_col(row[N]) which handles
both dict/list (psycopg3 auto-deserialized) and str (migration/older path).

Latent falsy-guard bug: the original `if row[N]` coerces empty dicts {} and
empty lists [] to None.  _json_col() must pass them through unchanged.

JSONB column indices (from _SELECT_COLS in sync_jobs_backend.py):
  11 = phases
  12 = phase_weights
  14 = progress_history
  15 = recovery_checkpoint
  16 = analytics_data
"""

from __future__ import annotations

import pytest


def _make_row(
    phases=None,
    phase_weights=None,
    progress_history=None,
    recovery_checkpoint=None,
    analytics_data=None,
):
    """
    Build a 17-element row tuple matching _SELECT_COLS column order.

    Column indices:
     0  job_id
     1  username
     2  user_alias
     3  job_type
     4  status
     5  created_at
     6  started_at
     7  completed_at
     8  repository_url
     9  progress
    10  error_message
    11  phases
    12  phase_weights
    13  current_phase
    14  progress_history
    15  recovery_checkpoint
    16  analytics_data
    """
    return (
        "sync-job-1",  # 0  job_id
        "alice",  # 1  username
        "alice-alias",  # 2  user_alias
        "index",  # 3  job_type
        "completed",  # 4  status
        "2026-01-01T00:00:00+00:00",  # 5  created_at
        None,  # 6  started_at
        None,  # 7  completed_at
        "https://github.com/org/repo.git",  # 8  repository_url
        100,  # 9  progress
        None,  # 10 error_message
        phases,  # 11 phases
        phase_weights,  # 12 phase_weights
        None,  # 13 current_phase
        progress_history,  # 14 progress_history
        recovery_checkpoint,  # 15 recovery_checkpoint
        analytics_data,  # 16 analytics_data
    )


def _row_to_dict(row):
    from code_indexer.server.storage.postgres.sync_jobs_backend import (
        SyncJobsPostgresBackend,
    )

    return SyncJobsPostgresBackend._row_to_dict(row)


# ---------------------------------------------------------------------------
# Parametrized cases shared by both test classes
# ---------------------------------------------------------------------------

# (col, kwargs_overrides, expected_value)
_PRE_PARSED_CASES = [
    ("phases", {"phases": {"phase1": "indexing"}}, {"phase1": "indexing"}),
    ("phase_weights", {"phase_weights": {"indexing": 0.5}}, {"indexing": 0.5}),
    ("progress_history", {"progress_history": [{"p": 100}]}, [{"p": 100}]),
    ("recovery_checkpoint", {"recovery_checkpoint": {"offset": 42}}, {"offset": 42}),
    ("analytics_data", {"analytics_data": {"files": 10}}, {"files": 10}),
]

_STRING_CASES = [
    ("phases", {"phases": '{"phase1": "indexing"}'}, {"phase1": "indexing"}),
    ("phase_weights", {"phase_weights": '{"indexing": 0.5}'}, {"indexing": 0.5}),
    ("progress_history", {"progress_history": '[{"p": 100}]'}, [{"p": 100}]),
    ("recovery_checkpoint", {"recovery_checkpoint": '{"offset": 42}'}, {"offset": 42}),
    ("analytics_data", {"analytics_data": '{"files": 10}'}, {"files": 10}),
]

# (col, empty_container) — falsy-guard latent bug
_EMPTY_CASES: list[tuple[str, object]] = [
    ("phases", {}),
    ("phase_weights", {}),
    ("progress_history", []),
    ("recovery_checkpoint", {}),
    ("analytics_data", {}),
]


class TestRowToDictJsonbPreParsedAndString:
    """
    _row_to_dict() handles JSONB columns that arrive as pre-parsed Python
    objects (psycopg3) or as raw JSON strings (migration path).
    """

    @pytest.mark.parametrize("col,kwargs,expected", _PRE_PARSED_CASES)
    def test_jsonb_as_pre_parsed_dict_or_list(self, col, kwargs, expected):
        """
        psycopg3 case: JSONB column arrives as a Python dict/list.
        Must pass through unchanged without raising TypeError from json.loads.
        RED before the fix.
        """
        result = _row_to_dict(_make_row(**kwargs))
        assert result[col] == expected

    @pytest.mark.parametrize("col,kwargs,expected", _STRING_CASES)
    def test_jsonb_as_json_string(self, col, kwargs, expected):
        """Migration path: JSONB column arrives as a JSON string; must deserialize."""
        result = _row_to_dict(_make_row(**kwargs))
        assert result[col] == expected

    @pytest.mark.parametrize("col", [c for c, _, _ in _PRE_PARSED_CASES])
    def test_jsonb_none_returns_none(self, col):
        """NULL JSONB column must return None."""
        result = _row_to_dict(_make_row())  # all JSONB kwargs default to None
        assert result[col] is None


class TestRowToDictJsonbFalsyGuardBug:
    """
    _row_to_dict() must not coerce empty containers to None.

    The original `if row[N]` guard treats {} and [] as falsy, silently
    replacing them with None.  _json_col() must pass them through as-is.
    RED before the fix.
    """

    @pytest.mark.parametrize("col,empty", _EMPTY_CASES)
    def test_empty_container_not_coerced_to_none(self, col, empty):
        """Empty dict {} or list [] must survive round-trip as the same empty container."""
        result = _row_to_dict(_make_row(**{col: empty}))
        assert result[col] == empty
        assert result[col] is not None
