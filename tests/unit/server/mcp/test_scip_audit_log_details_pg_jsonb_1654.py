"""
Unit tests for Bug #1654: mcp/handlers/scip.py silently drops
audit_logs.details on PostgreSQL JSONB (inconsistent with admin handler).

audit_logs.details is a JSONB column. psycopg deserializes it directly
into a native python dict before the row ever reaches application code,
while sqlite3 always returns the TEXT column as a str. scip.py's
_parse_log_details() called json.loads() unconditionally on this value
and caught the resulting TypeError ("the JSON object must be str, bytes
or bytearray, not dict") as if the JSON were malformed -- logging a
WARNING and silently discarding the entire details payload on every
PostgreSQL-backed deployment.

This is the same root-cause class as Bug #1622/#1652/#1655. The
established correct pattern already existed in this very codebase at
mcp/handlers/admin/__init__.py's _decode_audit_log_details(), and the
shared, reusable fix is `parse_json_column()`
(server/storage/json_column.py) -- this file proves _parse_log_details()
now reuses that helper instead of a 4th independently-drifted copy of
the same accept-dict-or-str logic.
"""

import logging

from code_indexer.server.mcp.handlers.scip import _parse_log_details

_SCIP_MODULE = "code_indexer.server.mcp.handlers.scip"
_JSON_COLUMN_MODULE = "code_indexer.server.storage.json_column"


def _warnings(caplog):
    return [r for r in caplog.records if r.name in (_SCIP_MODULE, _JSON_COLUMN_MODULE)]


class TestParseLogDetailsWithPostgresJsonbDict:
    """_parse_log_details() must accept a native dict `details` value."""

    def test_preserves_dict_details_from_postgres_jsonb_without_dropping_payload(
        self, caplog
    ):
        """
        Bug #1654 core reproduction: before the fix, a dict value for
        `details` (PostgreSQL JSONB, already deserialized by psycopg) hit
        json.loads()'s TypeError branch, logged a spurious WARNING, and
        the payload was silently dropped -- the flattened result never
        gained the pr_url/repo_alias keys nested inside `details`.
        """
        row = {
            "id": 42,
            "action_type": "pr_creation_success",
            "details": {
                "pr_url": "https://example.com/pr/1",
                "repo_alias": "foo",
            },
        }

        with caplog.at_level(logging.WARNING):
            result = _parse_log_details(row)

        assert result["pr_url"] == "https://example.com/pr/1"
        assert result["repo_alias"] == "foo"
        assert _warnings(caplog) == []

    def test_missing_details_defaults_to_empty_without_warning(self, caplog):
        row = {"id": 1, "action_type": "noop"}

        with caplog.at_level(logging.WARNING):
            result = _parse_log_details(row)

        assert result == row
        assert _warnings(caplog) == []


class TestParseLogDetailsSqliteRegression:
    """Regression coverage: SQLite TEXT-column string shape must keep working."""

    def test_still_parses_json_string_details_from_sqlite_text_column(self):
        row = {
            "id": 7,
            "action_type": "git_cleanup",
            "details": '{"repo_path": "/tmp/repo"}',
        }

        result = _parse_log_details(row)

        assert result["repo_path"] == "/tmp/repo"

    def test_malformed_json_string_details_logs_warning_and_does_not_crash(
        self, caplog
    ):
        row = {"id": 9, "action_type": "noop", "details": "{not valid json"}

        with caplog.at_level(logging.WARNING):
            result = _parse_log_details(row)

        assert result["id"] == 9
        assert len(_warnings(caplog)) >= 1
