"""
Unit tests for Bug #1075: BackgroundJobsPostgresBackend._row_to_dict() calls
json.loads() directly on metadata column instead of using _json_col() helper.

In psycopg3, JSONB columns are automatically deserialized to Python dicts.
Calling json.loads() on an already-deserialized dict raises:
    TypeError: the JSON object must be str, bytes or bytearray, not dict

The fix: replace json.loads(row[19]) with _json_col(row[19]) which handles
both dict (psycopg3 auto-deserialized) and str (migration/older path).
"""

from __future__ import annotations

import json


def _make_row(metadata_value):
    """
    Build a minimal 25-element row tuple matching _SELECT_COLS column order.

    Column indices (from _SELECT_COLS):
     0  job_id
     1  operation_type
     2  status
     3  created_at
     4  started_at
     5  completed_at
     6  result
     7  error
     8  progress
     9  username
    10  is_admin
    11  cancelled
    12  repo_alias
    13  resolution_attempts
    14  claude_actions
    15  failure_reason
    16  extended_error
    17  language_resolution_status
    18  progress_info
    19  metadata           <-- the value under test
    20  executing_node
    21  claimed_at
    22  current_phase
    23  phase_detail
    24  actor_username
    """
    return (
        "job-1",  # 0  job_id
        "xray_search",  # 1  operation_type
        "completed",  # 2  status
        "2026-01-01T00:00:00+00:00",  # 3  created_at
        None,  # 4  started_at
        None,  # 5  completed_at
        json.dumps({"ok": True}),  # 6  result  (str – _json_col handles it)
        None,  # 7  error
        100,  # 8  progress
        "alice",  # 9  username
        False,  # 10 is_admin
        False,  # 11 cancelled
        "my-repo",  # 12 repo_alias
        0,  # 13 resolution_attempts
        None,  # 14 claude_actions
        None,  # 15 failure_reason
        None,  # 16 extended_error
        None,  # 17 language_resolution_status
        None,  # 18 progress_info
        metadata_value,  # 19 metadata  <-- variable
        None,  # 20 executing_node
        None,  # 21 claimed_at
        None,  # 22 current_phase
        None,  # 23 phase_detail
        None,  # 24 actor_username
    )


class TestRowToDictMetadataBug1075:
    """
    Tests that _row_to_dict() correctly handles the metadata column (index 19)
    regardless of whether psycopg3 returned a pre-deserialized dict or a raw
    JSON string.
    """

    def test_metadata_as_dict_psycopg3(self):
        """
        When psycopg3 auto-deserializes JSONB to a Python dict, _row_to_dict
        must return that dict unchanged (not crash with TypeError from json.loads).
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        metadata_dict = {"repo_alias": "my-repo"}
        row = _make_row(metadata_dict)

        result = BackgroundJobsPostgresBackend._row_to_dict(row)

        assert result["metadata"] == {"repo_alias": "my-repo"}

    def test_metadata_as_json_string(self):
        """
        When metadata arrives as a JSON string (e.g. from migration or older rows),
        _row_to_dict must deserialize it and return the Python dict.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        metadata_json = '{"repo_alias": "my-repo"}'
        row = _make_row(metadata_json)

        result = BackgroundJobsPostgresBackend._row_to_dict(row)

        assert result["metadata"] == {"repo_alias": "my-repo"}

    def test_metadata_none(self):
        """
        When metadata is NULL (None), _row_to_dict must return None for metadata.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        row = _make_row(None)

        result = BackgroundJobsPostgresBackend._row_to_dict(row)

        assert result["metadata"] is None
