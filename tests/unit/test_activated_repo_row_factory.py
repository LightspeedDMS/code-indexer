"""Regression guard: cluster-mode activated-repo reads must use a dict_row cursor.

The connection pool's default row_factory is tuple_row (psycopg's default, which
the majority of backends rely on via positional row[N] access). Readers that
access rows by column name MUST opt into dict rows explicitly with
`conn.cursor(row_factory=dict_row)`. `_pg_row_to_metadata` reads by name, so its
callers must request dict rows -- relying on a leaked dict_row from a previous
borrower is exactly the bug that broke cluster-mode repo listing + search.
"""

import contextlib

from psycopg.rows import dict_row

from code_indexer.server.repositories.activated_repo_manager import ActivatedRepoManager


class _FakeCursor:
    def __init__(self, rows, rec):
        self._rows = rows
        self._rec = rec

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._rec["sql"] = sql

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, rows, rec):
        self._rows = rows
        self._rec = rec

    def cursor(self, row_factory=None):
        self._rec["row_factory"] = row_factory
        return _FakeCursor(self._rows, self._rec)


class _FakePool:
    def __init__(self, rows, rec):
        self._rows = rows
        self._rec = rec

    @contextlib.contextmanager
    def connection(self):
        yield _FakeConn(self._rows, self._rec)


def _mgr(rows, rec):
    # Bypass __init__ (heavy deps); we only exercise the PG read helpers.
    mgr = ActivatedRepoManager.__new__(ActivatedRepoManager)
    mgr._pool = _FakePool(rows, rec)
    return mgr


def test_list_user_repos_pg_requests_dict_row_cursor():
    rec = {}
    out = _mgr([], rec)._list_user_repos_pg("alice")
    assert out == []
    assert rec["row_factory"] is dict_row


def test_list_all_repos_pg_requests_dict_row_cursor():
    rec = {}
    out = _mgr([], rec)._list_all_repos_pg()
    assert out == []
    assert rec["row_factory"] is dict_row


def test_load_metadata_pg_requests_dict_row_cursor():
    rec = {}
    out = _mgr([], rec)._load_metadata_pg("alice", "myrepo")
    assert out is None
    assert rec["row_factory"] is dict_row
