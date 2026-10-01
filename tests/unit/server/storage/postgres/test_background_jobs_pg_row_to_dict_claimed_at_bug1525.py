"""
Unit tests for Bug #1525: BackgroundJobsPostgresBackend._row_to_dict() leaves
the claimed_at column (row index 21) as a raw value instead of normalizing it
through the _dt() helper already applied to created_at/started_at/completed_at.

claimed_at is a TIMESTAMPTZ column (001_initial_schema.sql), populated only by
cluster job-claim coordination. In PostgreSQL/psycopg mode this column comes
back as a native Python datetime object (never in solo/SQLite mode, where the
driver returns a string) -- so a claimed job's dict carries a raw datetime
straight through to callers.

MCP's get_job_details handler (server/mcp/handlers/admin/__init__.py) wraps its
result in _mcp_response(), which calls json.dumps(data) directly
(server/mcp/handlers/_utils.py). json.dumps cannot serialize a bare datetime
object, so this raises:
    TypeError: Object of type datetime is not JSON serializable

This reproduces that exact failure at the _row_to_dict() + json.dumps() layer,
without needing a live MCP/HTTP stack, mirroring the row-construction pattern
established by test_background_jobs_pg_row_to_dict_metadata_bug1075.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone


def _make_row(claimed_at_value):
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
    19  metadata
    20  executing_node
    21  claimed_at         <-- the value under test
    22  current_phase
    23  phase_detail
    24  actor_username
    """
    return (
        "job-1525",  # 0  job_id
        "xray_search",  # 1  operation_type
        "running",  # 2  status
        "2026-08-04T00:00:00+00:00",  # 3  created_at
        None,  # 4  started_at
        None,  # 5  completed_at
        None,  # 6  result
        None,  # 7  error
        50,  # 8  progress
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
        None,  # 19 metadata
        "node-2",  # 20 executing_node
        claimed_at_value,  # 21 claimed_at  <-- variable
        None,  # 22 current_phase
        None,  # 23 phase_detail
        None,  # 24 actor_username
    )


class TestRowToDictClaimedAtBug1525:
    """
    Tests that _row_to_dict() normalizes the claimed_at column (index 21)
    through the same _dt() helper applied to created_at/started_at/completed_at,
    regardless of whether the driver returned a native datetime (real
    psycopg/PostgreSQL behavior in cluster mode) or a string (SQLite/migration
    path).
    """

    def test_claimed_at_as_datetime_object_is_json_serializable_after_normalization(
        self,
    ):
        """
        When psycopg returns a native datetime for the TIMESTAMPTZ claimed_at
        column (the real cluster-mode behavior), _row_to_dict must convert it
        to an ISO string so the resulting job dict is JSON-serializable --
        exactly what MCP's _mcp_response() does via json.dumps(data) when
        returning get_job_details results.

        Before the fix: json.dumps raises
            TypeError: Object of type datetime is not JSON serializable
        reproducing the exact bug report symptom.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        claimed_at_dt = datetime(2026, 8, 4, 12, 30, 0, tzinfo=timezone.utc)
        row = _make_row(claimed_at_dt)

        result = BackgroundJobsPostgresBackend._row_to_dict(row)

        # The dict must be JSON-serializable end-to-end (mirrors _mcp_response).
        serialized = json.dumps(result)
        assert serialized is not None

        # claimed_at must be normalized to an ISO string, not a raw datetime.
        assert result["claimed_at"] == claimed_at_dt.isoformat()
        assert isinstance(result["claimed_at"], str)

    def test_claimed_at_as_string_passthrough_unchanged(self):
        """
        When claimed_at arrives already as a string (SQLite/migration path),
        _row_to_dict must return it unchanged.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        claimed_at_str = "2026-08-04T12:30:00+00:00"
        row = _make_row(claimed_at_str)

        result = BackgroundJobsPostgresBackend._row_to_dict(row)

        assert result["claimed_at"] == claimed_at_str
        json.dumps(result)  # must not raise

    def test_claimed_at_none_passthrough_unchanged(self):
        """
        When claimed_at is NULL (None, the default -- job never claimed by
        cluster coordination), _row_to_dict must return None.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        row = _make_row(None)

        result = BackgroundJobsPostgresBackend._row_to_dict(row)

        assert result["claimed_at"] is None
        json.dumps(result)  # must not raise
