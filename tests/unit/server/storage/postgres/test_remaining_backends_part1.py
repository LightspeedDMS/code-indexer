"""
Unit tests for PostgreSQL backends: CITokens, DescriptionRefreshTracking, SSHKeys (Story #414).

Verifies that each backend:
  1. Satisfies its Protocol (isinstance check).
  2. Exposes all required method names.
  3. Uses %s (not ?) placeholders — correct for psycopg v3.

All tests use a MagicMock connection pool — no real PostgreSQL required.
"""

from __future__ import annotations

from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool() -> MagicMock:
    """Return a MagicMock mimicking a psycopg ConnectionPool context-manager."""
    pool = MagicMock()
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    cursor.fetchall.return_value = []
    cursor.rowcount = 1
    conn.execute.return_value = cursor
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)
    return pool


def _get_conn(pool: MagicMock) -> MagicMock:
    """Return the mock connection from the pool context manager."""
    return pool.connection.return_value.__enter__.return_value


# ---------------------------------------------------------------------------
# CITokensPostgresBackend
# ---------------------------------------------------------------------------


class TestCITokensPostgresBackend:
    def test_satisfies_protocol(self) -> None:
        """CITokensPostgresBackend must satisfy the CITokensBackend Protocol."""
        from code_indexer.server.storage.postgres.ci_tokens_backend import (
            CITokensPostgresBackend,
        )
        from code_indexer.server.storage.protocols import CITokensBackend

        backend = CITokensPostgresBackend(_make_pool())
        assert isinstance(backend, CITokensBackend)

    def test_required_methods_present(self) -> None:
        """All protocol methods must be present on CITokensPostgresBackend."""
        from code_indexer.server.storage.postgres.ci_tokens_backend import (
            CITokensPostgresBackend,
        )

        required = {"save_token", "get_token", "delete_token", "list_tokens", "close"}
        for method in required:
            assert hasattr(CITokensPostgresBackend, method), f"Missing method: {method}"

    def test_save_token_uses_percent_s_placeholder(self) -> None:
        """save_token must use %s placeholders (psycopg v3), not ? (sqlite3)."""
        from code_indexer.server.storage.postgres.ci_tokens_backend import (
            CITokensPostgresBackend,
        )

        pool = _make_pool()
        backend = CITokensPostgresBackend(pool)
        backend.save_token("github", "enc_tok", "https://github.com")

        conn = _get_conn(pool)
        sql = conn.execute.call_args[0][0]
        assert "%s" in sql
        assert "?" not in sql

    def test_get_token_returns_none_when_not_found(self) -> None:
        """get_token returns None when the row is absent."""
        from code_indexer.server.storage.postgres.ci_tokens_backend import (
            CITokensPostgresBackend,
        )

        pool = _make_pool()
        result = CITokensPostgresBackend(pool).get_token("nonexistent")
        assert result is None

    def test_delete_token_returns_bool(self) -> None:
        """delete_token must return a bool."""
        from code_indexer.server.storage.postgres.ci_tokens_backend import (
            CITokensPostgresBackend,
        )

        pool = _make_pool()
        result = CITokensPostgresBackend(pool).delete_token("github")
        assert isinstance(result, bool)

    def test_list_tokens_returns_dict(self) -> None:
        """list_tokens must return a dict."""
        from code_indexer.server.storage.postgres.ci_tokens_backend import (
            CITokensPostgresBackend,
        )

        pool = _make_pool()
        result = CITokensPostgresBackend(pool).list_tokens()
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# DescriptionRefreshTrackingPostgresBackend
# ---------------------------------------------------------------------------


class TestDescriptionRefreshTrackingPostgresBackend:
    def test_satisfies_protocol(self) -> None:
        """DescriptionRefreshTrackingPostgresBackend must satisfy the Protocol."""
        from code_indexer.server.storage.postgres.description_refresh_tracking_backend import (
            DescriptionRefreshTrackingPostgresBackend,
        )
        from code_indexer.server.storage.protocols import (
            DescriptionRefreshTrackingBackend,
        )

        backend = DescriptionRefreshTrackingPostgresBackend(_make_pool())
        assert isinstance(backend, DescriptionRefreshTrackingBackend)

    def test_required_methods_present(self) -> None:
        """All protocol methods must be present."""
        from code_indexer.server.storage.postgres.description_refresh_tracking_backend import (
            DescriptionRefreshTrackingPostgresBackend,
        )

        required = {
            "get_tracking_record",
            "get_stale_repos",
            "upsert_tracking",
            "delete_tracking",
            "get_all_tracking",
            "close",
        }
        for method in required:
            assert hasattr(DescriptionRefreshTrackingPostgresBackend, method), (
                f"Missing method: {method}"
            )

    def test_upsert_tracking_uses_percent_s(self) -> None:
        """upsert_tracking must use %s placeholders."""
        from code_indexer.server.storage.postgres.description_refresh_tracking_backend import (
            DescriptionRefreshTrackingPostgresBackend,
        )

        pool = _make_pool()
        backend = DescriptionRefreshTrackingPostgresBackend(pool)
        backend.upsert_tracking("my-repo", status="queued", last_run="2026-01-01")

        conn = _get_conn(pool)
        sql = conn.execute.call_args[0][0]
        assert "%s" in sql
        assert "?" not in sql

    def test_upsert_tracking_noop_on_empty_fields(self) -> None:
        """upsert_tracking with no valid fields must not execute any SQL."""
        from code_indexer.server.storage.postgres.description_refresh_tracking_backend import (
            DescriptionRefreshTrackingPostgresBackend,
        )

        pool = _make_pool()
        backend = DescriptionRefreshTrackingPostgresBackend(pool)
        backend.upsert_tracking("my-repo")  # no fields

        conn = _get_conn(pool)
        conn.execute.assert_not_called()

    def test_get_stale_repos_returns_list(self) -> None:
        """get_stale_repos must return a list."""
        from code_indexer.server.storage.postgres.description_refresh_tracking_backend import (
            DescriptionRefreshTrackingPostgresBackend,
        )

        pool = _make_pool()
        result = DescriptionRefreshTrackingPostgresBackend(pool).get_stale_repos(
            "2026-01-01T00:00:00"
        )
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# SSHKeysPostgresBackend
# ---------------------------------------------------------------------------


class TestSSHKeysPostgresBackend:
    def test_satisfies_protocol(self) -> None:
        """SSHKeysPostgresBackend must satisfy the SSHKeysBackend Protocol."""
        from code_indexer.server.storage.postgres.ssh_keys_backend import (
            SSHKeysPostgresBackend,
        )
        from code_indexer.server.storage.protocols import SSHKeysBackend

        backend = SSHKeysPostgresBackend(_make_pool())
        assert isinstance(backend, SSHKeysBackend)

    def test_required_methods_present(self) -> None:
        """All protocol methods must be present."""
        from code_indexer.server.storage.postgres.ssh_keys_backend import (
            SSHKeysPostgresBackend,
        )

        required = {
            "create_key",
            "get_key",
            "assign_host",
            "remove_host",
            "delete_key",
            "list_keys",
            "close",
        }
        for method in required:
            assert hasattr(SSHKeysPostgresBackend, method), f"Missing method: {method}"

    def test_create_key_uses_percent_s(self) -> None:
        """create_key must use %s placeholders."""
        from code_indexer.server.storage.postgres.ssh_keys_backend import (
            SSHKeysPostgresBackend,
        )

        pool = _make_pool()
        backend = SSHKeysPostgresBackend(pool)
        backend.create_key(
            name="my-key",
            fingerprint="AA:BB",
            key_type="ed25519",
            private_path="/path/priv",
            public_path="/path/pub",
        )

        conn = _get_conn(pool)
        sql = conn.execute.call_args[0][0]
        assert "%s" in sql
        assert "?" not in sql

    def test_assign_host_idempotent(self) -> None:
        """assign_host must use ON CONFLICT DO NOTHING for idempotency."""
        from code_indexer.server.storage.postgres.ssh_keys_backend import (
            SSHKeysPostgresBackend,
        )

        pool = _make_pool()
        backend = SSHKeysPostgresBackend(pool)
        backend.assign_host("my-key", "github.com")

        conn = _get_conn(pool)
        sql = conn.execute.call_args[0][0].upper()
        assert "ON CONFLICT" in sql
        assert "DO NOTHING" in sql

    def test_get_key_returns_none_when_not_found(self) -> None:
        """get_key returns None when the key is absent."""
        from code_indexer.server.storage.postgres.ssh_keys_backend import (
            SSHKeysPostgresBackend,
        )

        pool = _make_pool()
        result = SSHKeysPostgresBackend(pool).get_key("nonexistent")
        assert result is None

    def test_delete_key_returns_bool(self) -> None:
        """delete_key must return a bool."""
        from code_indexer.server.storage.postgres.ssh_keys_backend import (
            SSHKeysPostgresBackend,
        )

        pool = _make_pool()
        result = SSHKeysPostgresBackend(pool).delete_key("my-key")
        assert isinstance(result, bool)
