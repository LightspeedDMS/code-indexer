"""DistributedJobClaimer metadata column + job_types/exclude_types filters."""

from unittest.mock import MagicMock

from code_indexer.server.services import memory_governor as mg
from code_indexer.server.services.distributed_job_claimer import (
    DistributedJobClaimer,
    _SELECT_COLS,
    _json_col,
    _row_to_dict,
)


class TestMetadataColumn:
    def test_select_cols_includes_metadata(self):
        assert "metadata" in _SELECT_COLS

    def test_json_col_handles_none_dict_str(self):
        assert _json_col(None) is None
        assert _json_col({"a": 1}) == {"a": 1}
        assert _json_col('{"a": 1}') == {"a": 1}

    def test_row_to_dict_includes_metadata_parsed(self):
        row = [None] * 21
        row[0] = "j1"
        row[20] = {"repo_url": "u", "alias": "a"}
        d = _row_to_dict(row)
        assert d["metadata"] == {"repo_url": "u", "alias": "a"}

    def test_row_to_dict_metadata_from_json_string(self):
        row = [None] * 21
        row[20] = '{"clear": false}'
        assert _row_to_dict(row)["metadata"] == {"clear": False}


def _claimer():
    pool = MagicMock()
    return DistributedJobClaimer(pool=pool, node_id="n1"), pool


def _capture_execute(pool):
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = None
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)
    return cur


class TestClaimFilters:
    def teardown_method(self):
        mg.clear_memory_governor()

    def test_job_types_builds_any_filter(self):
        c, pool = _claimer()
        cur = _capture_execute(pool)
        c.claim_next_job(job_types=["add_golden_repo", "sync_repository"])
        sql, params = cur.execute.call_args[0]
        assert "operation_type = ANY(%s)" in sql
        assert params == ["n1", ["add_golden_repo", "sync_repository"]]

    def test_exclude_types_builds_all_filter(self):
        c, pool = _claimer()
        cur = _capture_execute(pool)
        c.claim_next_job(exclude_types=["add_golden_repo"])
        sql, params = cur.execute.call_args[0]
        assert "operation_type <> ALL(%s)" in sql
        assert params == ["n1", ["add_golden_repo"]]

    def test_no_filter_has_no_type_clause(self):
        c, pool = _claimer()
        cur = _capture_execute(pool)
        c.claim_next_job()
        sql, params = cur.execute.call_args[0]
        # No type filter in the WHERE clause (operation_type still appears in the
        # RETURNING column list, so assert on the filter predicates specifically).
        assert "operation_type = %s" not in sql
        assert "ANY(%s)" not in sql
        assert "ALL(%s)" not in sql
        assert params == ["n1"]

    def test_single_job_type_backward_compat(self):
        c, pool = _claimer()
        cur = _capture_execute(pool)
        c.claim_next_job(job_type="refresh_golden_repo")
        sql, params = cur.execute.call_args[0]
        assert "operation_type = %s" in sql
        assert params == ["n1", "refresh_golden_repo"]
