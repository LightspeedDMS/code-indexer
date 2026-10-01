"""
Code-review remediation (F3, on the #1652/#1655 commit): ActivatedRepoManager
._pg_row_to_metadata's inline `isinstance(metadata_json, dict) else
json.loads(metadata_json)` logic (lines ~507-512) was the third near-identical
copy of the "accept dict as-is, else json.loads() str/bytes" pattern
(alongside dependency_map_routes.py's since-deleted _parse_phase_timings_value
and wiki_cache.py's since-deleted _parse_json_cache_value). It is folded into
the shared `parse_json_column()` helper (server/storage/json_column.py).

This also fixes a genuine behavior gap: the old inline logic had NO
malformed-value handling at all — a corrupt `metadata_json` string raised
`json.JSONDecodeError` out of `_pg_row_to_metadata` (and therefore out of
every list/get call site), unlike the other two call sites which always
degraded gracefully. Folding in `parse_json_column()` makes this call site
fail-soft too (WARNING + skip the extra fields), consistent with the
established contract.

`_pg_row_to_metadata` does not touch `self`, so it is exercised via
`object.__new__(ActivatedRepoManager)` to avoid the heavyweight
`__init__` (golden repo manager, background job manager, etc.).
"""

import logging


def _make_manager():
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )

    return object.__new__(ActivatedRepoManager)


_BASE_ROW = {
    "user_alias": "my-ws",
    "username": "alice",
    "golden_repo_alias": "my-repo",
    "repo_path": "/data/alice/my-ws",
    "current_branch": "main",
    "activated_at": None,
    "last_accessed": None,
    "git_committer_email": None,
    "ssh_key_used": None,
    "is_composite": False,
    "wiki_enabled": False,
    "activation_id": None,
}


class TestPgRowToMetadataNativeDict:
    """Bug #1652 root cause: a native dict (PostgreSQL JSONB) must merge in
    directly without going through json.loads()."""

    def test_dict_metadata_json_merges_extra_fields(self):
        manager = _make_manager()
        row = dict(_BASE_ROW, metadata_json={"custom_field": "custom_value"})

        result = manager._pg_row_to_metadata(row)

        assert result["custom_field"] == "custom_value"

    def test_string_metadata_json_still_parses_and_merges(self):
        """Regression: SQLite's TEXT column (JSON string) must still work."""
        manager = _make_manager()
        row = dict(_BASE_ROW, metadata_json='{"custom_field": "from_string"}')

        result = manager._pg_row_to_metadata(row)

        assert result["custom_field"] == "from_string"

    def test_none_metadata_json_leaves_base_fields_only(self):
        manager = _make_manager()
        row = dict(_BASE_ROW, metadata_json=None)

        result = manager._pg_row_to_metadata(row)

        assert "custom_field" not in result
        assert result["username"] == "alice"


class TestPgRowToMetadataMalformedIsFailSoft:
    """F3 fold-in fixes a genuine gap: malformed metadata_json used to raise
    (json.JSONDecodeError propagating out of _pg_row_to_metadata); it must
    now degrade gracefully like the other two call sites."""

    def test_malformed_metadata_json_no_longer_raises(self, caplog):
        manager = _make_manager()
        row = dict(_BASE_ROW, metadata_json="not-valid-json{")

        with caplog.at_level(logging.WARNING):
            result = manager._pg_row_to_metadata(row)

        assert result["username"] == "alice"
        assert "custom_field" not in result

    def test_malformed_metadata_json_logs_a_warning(self, caplog):
        manager = _make_manager()
        row = dict(_BASE_ROW, metadata_json="not-valid-json{")

        with caplog.at_level(
            logging.WARNING, logger="code_indexer.server.storage.json_column"
        ):
            manager._pg_row_to_metadata(row)

        warnings = [
            r
            for r in caplog.records
            if r.name == "code_indexer.server.storage.json_column"
        ]
        assert len(warnings) == 1
