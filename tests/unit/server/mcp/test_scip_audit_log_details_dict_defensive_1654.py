"""
Unit tests for Bug #1654: mcp/handlers/scip.py's _parse_log_details() dict
handling for the audit_logs.details column.

CORRECTED PREMISE (post-review): the issue as originally filed claimed
`audit_logs.details` is JSONB on PostgreSQL and that a native dict value
(from psycopg's JSONB auto-deserialization) hit `_parse_log_details()`'s
unconditional `json.loads()` call, raising a `TypeError` that was caught
and silently discarded the payload. That premise is FALSE and was never
a live production bug:

- `storage/postgres/migrations/sql/001_initial_schema.sql:358-366` does
  define an EARLIER `audit_logs` table with a JSONB `details` column, but
- `storage/postgres/migrations/sql/002_groups_access_schema.sql:18` drops
  that table (`DROP TABLE IF EXISTS audit_logs CASCADE`, per its own
  header comment) and `:65-73` recreates it with the
  admin_id/action_type/target_type/target_id shape `scip.py` actually
  consumes, whose `details` column is `TEXT` -- not JSONB.
- `PostgresAuditLogBackend.log()`/`log_raw()`
  (`storage/postgres/audit_log_backend.py:84,114`) declare
  `details: Optional[str]` and always write either a `json.dumps()`
  string or `None`.

So `row["details"]` is a `str` or `None` on BOTH SQLite and PostgreSQL,
and the pre-fix `json.loads()` call never actually raised `TypeError` on
this column in production. There was no data loss and no live bug.

This file keeps the dict-handling behavior change (routing through the
shared `parse_json_column()` helper from `server/storage/json_column.py`,
same helper used by Bug #1622/#1652/#1655's genuinely-live JSONB
columns) as a DEFENSIVE-CONTRACT improvement: it makes `_parse_log_details`
consistent with `mcp/handlers/admin/__init__.py`'s
`_decode_audit_log_details()` (which already tolerates dict-or-str for
this same column) and removes a 4th independently-drifted copy of that
accept-dict-or-str normalization logic (Messi Rule #4 anti-duplication).
It also guards against a possible future migration of this column to
JSONB. These tests verify that defensive contract -- NOT a reproduction
of a real PostgreSQL JSONB scenario, which cannot occur for this column.
"""

import logging

from code_indexer.server.mcp.handlers.scip import _parse_log_details

_SCIP_MODULE = "code_indexer.server.mcp.handlers.scip"
_JSON_COLUMN_MODULE = "code_indexer.server.storage.json_column"


def _warnings(caplog):
    return [r for r in caplog.records if r.name in (_SCIP_MODULE, _JSON_COLUMN_MODULE)]


class TestParseLogDetailsAcceptsDictDefensively:
    """_parse_log_details() tolerates a dict `details` value defensively.

    A dict value cannot occur for this column in current production
    (it is TEXT on both backends), but the shared `parse_json_column()`
    helper accepts one without raising or dropping data, matching
    admin/__init__.py's established handling of the same column.
    """

    def test_preserves_dict_details_when_given_a_dict_defensively(self, caplog):
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


class TestParseLogDetailsTextColumnRegression:
    """Regression coverage: the real production shape (TEXT string) must keep working."""

    def test_still_parses_json_string_details_from_text_column(self):
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
