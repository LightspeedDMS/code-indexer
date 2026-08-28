"""
Regression tests for Bug #1663: ConnectionPool must pin the PostgreSQL
session TimeZone explicitly (to UTC) so naive and timezone-aware datetimes
written/read by application code can't silently diverge in interpretation
across the write and read paths.

This is the "additionally" half of the #1663 fix -- the primary fix made
diagnostics_service.py write run_at as a timezone-AWARE UTC value (so it no
longer depends on the session's configured timezone at all), but pinning the
session timezone is a clean, low-risk defense-in-depth addition at the one
shared choke point (ConnectionPool) every PostgreSQL backend in this project
already goes through.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

_TEST_DSN = "postgresql://test"


@pytest.fixture
def mock_psycopg_pool():
    """Patch the module-global psycopg_pool sentinel and construct a real
    ConnectionPool against it, yielding the mock so a test can inspect how
    it was called."""
    with patch(
        "code_indexer.server.storage.postgres.connection_pool._PsycopgPool"
    ) as mock_psycopg:
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )

        ConnectionPool(_TEST_DSN)
        yield mock_psycopg


class TestConnectionPoolPinsSessionTimezone1663:
    """ConnectionPool must configure every new connection to use UTC as its
    session TimeZone, via psycopg_pool's `configure` callback."""

    def test_psycopg_pool_constructed_with_configure_callback(
        self, mock_psycopg_pool
    ) -> None:
        """The underlying psycopg_pool.ConnectionPool must be constructed
        with a `configure` callback -- the standard psycopg_pool mechanism
        for running setup SQL on every new connection."""
        mock_psycopg_pool.assert_called_once()
        call_kwargs = mock_psycopg_pool.call_args[1]
        assert "configure" in call_kwargs, (
            "ConnectionPool must pass a `configure` callback to "
            "psycopg_pool.ConnectionPool to pin the session TimeZone"
        )
        assert callable(call_kwargs["configure"])

    def test_configure_callback_sets_session_timezone_to_utc(
        self, mock_psycopg_pool
    ) -> None:
        """Invoking the configure callback against a connection must issue
        a `SET TIME ZONE` (or equivalent) statement pinning UTC."""
        configure_callback = mock_psycopg_pool.call_args[1]["configure"]
        mock_conn = MagicMock()

        configure_callback(mock_conn)

        mock_conn.execute.assert_called_once()
        executed_sql = mock_conn.execute.call_args[0][0].upper()
        assert "TIME ZONE" in executed_sql
        assert "UTC" in executed_sql

    def test_configure_callback_commits_transaction_leaving_connection_idle(
        self, mock_psycopg_pool
    ) -> None:
        """psycopg_pool requires a connection returned from `configure` to
        be left IDLE, not mid-transaction (INTRANS) -- otherwise it
        discards the connection with "connection left in status INTRANS by
        configure function ...: discarded" and never successfully opens a
        pooled connection. Confirmed against a real local PostgreSQL
        instance: executing `SET TIME ZONE` without a following commit()
        left every new connection INTRANS and the pool never became
        usable."""
        configure_callback = mock_psycopg_pool.call_args[1]["configure"]
        mock_conn = MagicMock()

        configure_callback(mock_conn)

        mock_conn.commit.assert_called_once()
