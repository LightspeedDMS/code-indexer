"""
Tests for Bug #545: Dual connection pool architecture.

Verifies:
1. ConnectionPool accepts name and timeout parameters
2. BackendRegistry has critical_connection_pool field (defaults to None)
3. Slow acquisition warning is logged when threshold exceeded
"""

import logging
from unittest.mock import MagicMock, patch

from code_indexer.server.storage.factory import BackendRegistry


def _make_mock_psycopg_instance():
    """Create a mock psycopg pool instance with connection context manager."""
    mock_conn = MagicMock()
    mock_instance = MagicMock()
    mock_instance.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
    mock_instance.connection.return_value.__exit__ = MagicMock(return_value=False)
    return mock_instance


class TestBackendRegistryCriticalPool:
    """Bug #545: BackendRegistry.critical_connection_pool field."""

    def test_critical_pool_field_exists_and_defaults_to_none(self):
        """critical_connection_pool field must exist and default to None."""
        fields = BackendRegistry.__dataclass_fields__
        assert "critical_connection_pool" in fields
        assert fields["critical_connection_pool"].default is None

    def test_critical_pool_can_be_set(self):
        """critical_connection_pool must accept a pool instance."""
        mock_pool = MagicMock()
        kwargs = {}
        for field_name in BackendRegistry.__dataclass_fields__:
            if field_name in ("connection_pool", "critical_connection_pool"):
                continue
            kwargs[field_name] = MagicMock()
        kwargs["connection_pool"] = MagicMock()
        kwargs["critical_connection_pool"] = mock_pool

        registry = BackendRegistry(**kwargs)
        assert registry.critical_connection_pool is mock_pool


class TestConnectionPoolParameters:
    """Bug #545: ConnectionPool name and timeout parameters."""

    def test_accepts_name_parameter(self):
        """ConnectionPool must accept a name parameter."""
        with patch("code_indexer.server.storage.postgres.connection_pool._PsycopgPool"):
            from code_indexer.server.storage.postgres.connection_pool import (
                ConnectionPool,
            )

            pool = ConnectionPool(
                "postgresql://localhost/test", name="critical", timeout=10.0
            )
            assert pool._name == "critical"

    def test_accepts_timeout_parameter(self):
        """ConnectionPool must accept a timeout parameter."""
        with patch("code_indexer.server.storage.postgres.connection_pool._PsycopgPool"):
            from code_indexer.server.storage.postgres.connection_pool import (
                ConnectionPool,
            )

            pool = ConnectionPool(
                "postgresql://localhost/test", timeout=5.0, name="test"
            )
            assert pool._timeout == 5.0

    def test_default_name_is_general(self):
        """Default pool name must be 'general'."""
        with patch("code_indexer.server.storage.postgres.connection_pool._PsycopgPool"):
            from code_indexer.server.storage.postgres.connection_pool import (
                ConnectionPool,
            )

            pool = ConnectionPool("postgresql://localhost/test")
            assert pool._name == "general"

    def test_timeout_passed_to_psycopg_pool(self):
        """Timeout must be passed to the underlying psycopg pool."""
        with patch(
            "code_indexer.server.storage.postgres.connection_pool._PsycopgPool"
        ) as mock_psycopg:
            from code_indexer.server.storage.postgres.connection_pool import (
                ConnectionPool,
            )

            ConnectionPool("postgresql://localhost/test", timeout=7.5)
            mock_psycopg.assert_called_once()
            call_kwargs = mock_psycopg.call_args[1]
            assert call_kwargs["timeout"] == 7.5


class TestSlowAcquisitionWarning:
    """Bug #545: Slow acquisition warning logging."""

    @patch("code_indexer.server.storage.postgres.connection_pool._time.monotonic")
    def test_logs_warning_when_acquisition_exceeds_threshold(
        self, mock_monotonic, caplog
    ):
        """Warning must be logged when acquisition time > 5s."""
        mock_monotonic.side_effect = [0.0, 6.0]

        with patch(
            "code_indexer.server.storage.postgres.connection_pool._PsycopgPool",
            return_value=_make_mock_psycopg_instance(),
        ):
            from code_indexer.server.storage.postgres.connection_pool import (
                ConnectionPool,
            )

            pool = ConnectionPool("postgresql://localhost/test", name="test-pool")
            with caplog.at_level(logging.WARNING):
                with pool.connection():
                    pass

            assert any(
                "Slow connection acquisition" in r.message for r in caplog.records
            )

    @patch("code_indexer.server.storage.postgres.connection_pool._time.monotonic")
    def test_warning_includes_pool_name(self, mock_monotonic, caplog):
        """Warning message must include the pool name."""
        mock_monotonic.side_effect = [0.0, 8.0]

        with patch(
            "code_indexer.server.storage.postgres.connection_pool._PsycopgPool",
            return_value=_make_mock_psycopg_instance(),
        ):
            from code_indexer.server.storage.postgres.connection_pool import (
                ConnectionPool,
            )

            pool = ConnectionPool(
                "postgresql://localhost/test", name="my-critical-pool"
            )
            with caplog.at_level(logging.WARNING):
                with pool.connection():
                    pass

            warning_msgs = [r.message for r in caplog.records if "Slow" in r.message]
            assert len(warning_msgs) >= 1
            assert "my-critical-pool" in warning_msgs[0]

    @patch("code_indexer.server.storage.postgres.connection_pool._time.monotonic")
    def test_no_warning_when_acquisition_fast(self, mock_monotonic, caplog):
        """No warning when acquisition is under threshold."""
        mock_monotonic.side_effect = [0.0, 0.1]

        with patch(
            "code_indexer.server.storage.postgres.connection_pool._PsycopgPool",
            return_value=_make_mock_psycopg_instance(),
        ):
            from code_indexer.server.storage.postgres.connection_pool import (
                ConnectionPool,
            )

            pool = ConnectionPool("postgresql://localhost/test")
            with caplog.at_level(logging.WARNING):
                with pool.connection():
                    pass

            slow_warnings = [r for r in caplog.records if "Slow" in r.message]
            assert len(slow_warnings) == 0
