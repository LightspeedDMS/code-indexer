"""Unit tests for LangfuseService facade."""

import pytest
import threading
from unittest.mock import Mock, patch, MagicMock

# Prevent real Langfuse SDK initialization at module level - MUST be before other imports
import sys

sys.modules["langfuse"] = MagicMock()

from code_indexer.server.services.langfuse_service import (
    LangfuseService,
    get_langfuse_service,
    reset_langfuse_service,
)
from code_indexer.server.utils.config_manager import ServerConfig, LangfuseConfig


@pytest.fixture
def mock_config_manager():
    """Create a mock config manager."""
    manager = Mock()
    return manager


@pytest.fixture
def langfuse_service(mock_config_manager):
    """Create a LangfuseService instance."""
    return LangfuseService(mock_config_manager)


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset the singleton before each test."""
    reset_langfuse_service()
    yield
    reset_langfuse_service()


class TestLangfuseServiceInit:
    """Tests for LangfuseService initialization."""

    def test_init_stores_config_manager(self, mock_config_manager):
        """Test that init stores the config manager."""
        service = LangfuseService(mock_config_manager)
        assert service._config_manager is mock_config_manager

    def test_init_components_are_none(self, langfuse_service):
        """Test that components start as None (lazy init)."""
        assert langfuse_service._client is None
        assert langfuse_service._trace_manager is None
        assert langfuse_service._span_logger is None


class TestIsEnabled:
    """Tests for is_enabled method."""

    def test_is_enabled_when_config_exists_and_enabled(
        self, langfuse_service, mock_config_manager
    ):
        """Test is_enabled returns True when config exists and enabled."""
        config = Mock(spec=ServerConfig)
        config.langfuse_config = LangfuseConfig(
            enabled=True,
            public_key="test-key",
            secret_key="test-secret",
            host="https://example.com",
        )
        mock_config_manager.load_config.return_value = config

        assert langfuse_service.is_enabled() is True

    def test_is_enabled_when_config_exists_but_disabled(
        self, langfuse_service, mock_config_manager
    ):
        """Test is_enabled returns False when config disabled."""
        config = Mock(spec=ServerConfig)
        config.langfuse_config = LangfuseConfig(
            enabled=False,
            public_key="test-key",
            secret_key="test-secret",
            host="https://example.com",
        )
        mock_config_manager.load_config.return_value = config

        assert langfuse_service.is_enabled() is False

    def test_is_enabled_when_no_langfuse_config(
        self, langfuse_service, mock_config_manager
    ):
        """Test is_enabled returns False when no langfuse config."""
        config = Mock(spec=ServerConfig)
        config.langfuse_config = None
        mock_config_manager.load_config.return_value = config

        assert langfuse_service.is_enabled() is False

    def test_is_enabled_when_no_config_at_all(
        self, langfuse_service, mock_config_manager
    ):
        """Test is_enabled returns False when config is None."""
        mock_config_manager.load_config.return_value = None

        assert langfuse_service.is_enabled() is False


class TestClientProperty:
    """Tests for client property (lazy initialization)."""

    @patch("code_indexer.server.services.langfuse_service.LangfuseClient")
    def test_client_lazy_initialization(
        self, mock_client_class, langfuse_service, mock_config_manager
    ):
        """Test that client is lazily initialized on first access."""
        config = Mock(spec=ServerConfig)
        langfuse_config = LangfuseConfig(
            enabled=True,
            public_key="test-key",
            secret_key="test-secret",
            host="https://example.com",
        )
        config.langfuse_config = langfuse_config
        mock_config_manager.load_config.return_value = config

        mock_client_instance = Mock()
        mock_client_class.return_value = mock_client_instance

        # First access
        client = langfuse_service.client

        assert client is mock_client_instance
        mock_client_class.assert_called_once_with(langfuse_config)

    @patch("code_indexer.server.services.langfuse_service.LangfuseClient")
    def test_client_returns_same_instance(
        self, mock_client_class, langfuse_service, mock_config_manager
    ):
        """Test that subsequent accesses return the same client instance."""
        config = Mock(spec=ServerConfig)
        # Use disabled config to prevent real Langfuse initialization
        config.langfuse_config = LangfuseConfig(
            enabled=False,
            public_key="test-key",
            secret_key="test-secret",
            host="https://example.com",
        )
        mock_config_manager.load_config.return_value = config

        mock_client_instance = Mock()
        mock_client_class.return_value = mock_client_instance

        # Multiple accesses
        client1 = langfuse_service.client
        client2 = langfuse_service.client

        assert client1 is client2
        mock_client_class.assert_called_once()  # Only initialized once


class TestTraceManagerProperty:
    """Tests for trace_manager property (lazy initialization)."""

    @patch("code_indexer.server.services.langfuse_service.TraceStateManager")
    @patch("code_indexer.server.services.langfuse_service.LangfuseClient")
    def test_trace_manager_lazy_initialization(
        self,
        mock_client_class,
        mock_trace_manager_class,
        langfuse_service,
        mock_config_manager,
    ):
        """Test that trace_manager is lazily initialized on first access."""
        config = Mock(spec=ServerConfig)
        config.langfuse_config = LangfuseConfig(
            enabled=False,
            public_key="test-key",
            secret_key="test-secret",
            host="https://example.com",
        )
        mock_config_manager.load_config.return_value = config

        mock_client = Mock()
        mock_client_class.return_value = mock_client

        mock_trace_manager = Mock()
        mock_trace_manager_class.return_value = mock_trace_manager

        # First access (triggers client init too)
        manager = langfuse_service.trace_manager

        assert manager is mock_trace_manager
        mock_trace_manager_class.assert_called_once_with(mock_client)

    @patch("code_indexer.server.services.langfuse_service.TraceStateManager")
    @patch("code_indexer.server.services.langfuse_service.LangfuseClient")
    def test_trace_manager_returns_same_instance(
        self,
        mock_client_class,
        mock_trace_manager_class,
        langfuse_service,
        mock_config_manager,
    ):
        """Test that subsequent accesses return same trace_manager."""
        config = Mock(spec=ServerConfig)
        config.langfuse_config = LangfuseConfig(
            enabled=False,
            public_key="test-key",
            secret_key="test-secret",
            host="https://example.com",
        )
        mock_config_manager.load_config.return_value = config

        mock_client_class.return_value = Mock()
        mock_trace_manager_class.return_value = Mock()

        # Multiple accesses
        manager1 = langfuse_service.trace_manager
        manager2 = langfuse_service.trace_manager

        assert manager1 is manager2
        mock_trace_manager_class.assert_called_once()


class TestSpanLoggerProperty:
    """Tests for span_logger property (lazy initialization)."""

    @patch("code_indexer.server.services.langfuse_service.AutoSpanLogger")
    @patch("code_indexer.server.services.langfuse_service.TraceStateManager")
    @patch("code_indexer.server.services.langfuse_service.LangfuseClient")
    def test_span_logger_lazy_initialization(
        self,
        mock_client_class,
        mock_trace_manager_class,
        mock_span_logger_class,
        langfuse_service,
        mock_config_manager,
    ):
        """Test that span_logger is lazily initialized on first access."""
        config = Mock(spec=ServerConfig)
        config.langfuse_config = LangfuseConfig(
            enabled=False,
            public_key="test-key",
            secret_key="test-secret",
            host="https://example.com",
        )
        mock_config_manager.load_config.return_value = config

        mock_client = Mock()
        mock_client_class.return_value = mock_client

        mock_trace_manager = Mock()
        mock_trace_manager_class.return_value = mock_trace_manager

        mock_span_logger = Mock()
        mock_span_logger_class.return_value = mock_span_logger

        # First access
        logger = langfuse_service.span_logger

        assert logger is mock_span_logger
        # Story #136 follow-up: AutoSpanLogger now takes config as 3rd parameter
        mock_span_logger_class.assert_called_once_with(
            mock_trace_manager, mock_client, config.langfuse_config
        )

    @patch("code_indexer.server.services.langfuse_service.AutoSpanLogger")
    @patch("code_indexer.server.services.langfuse_service.TraceStateManager")
    @patch("code_indexer.server.services.langfuse_service.LangfuseClient")
    def test_span_logger_returns_same_instance(
        self,
        mock_client_class,
        mock_trace_manager_class,
        mock_span_logger_class,
        langfuse_service,
        mock_config_manager,
    ):
        """Test that subsequent accesses return same span_logger."""
        config = Mock(spec=ServerConfig)
        config.langfuse_config = LangfuseConfig(
            enabled=False,
            public_key="test-key",
            secret_key="test-secret",
            host="https://example.com",
        )
        mock_config_manager.load_config.return_value = config

        mock_client_class.return_value = Mock()
        mock_trace_manager_class.return_value = Mock()
        mock_span_logger_class.return_value = Mock()

        # Multiple accesses
        logger1 = langfuse_service.span_logger
        logger2 = langfuse_service.span_logger

        assert logger1 is logger2
        mock_span_logger_class.assert_called_once()


class TestEagerInitialize:
    """Tests for eager_initialize method on LangfuseService."""

    @patch("code_indexer.server.services.langfuse_service.LangfuseClient")
    def test_eager_initialize_calls_client_eager_initialize(
        self, mock_client_class, langfuse_service, mock_config_manager
    ):
        """
        LangfuseService.eager_initialize() must call client.eager_initialize()
        to pre-initialize the Langfuse SDK during application startup.
        This moves the one-time SDK import + network I/O cost to startup
        rather than the first MCP request.
        """
        config = Mock(spec=ServerConfig)
        config.langfuse_config = LangfuseConfig(
            enabled=True,
            public_key="test-key",
            secret_key="test-secret",
            host="https://example.com",
        )
        mock_config_manager.load_config.return_value = config

        mock_client_instance = Mock()
        mock_client_class.return_value = mock_client_instance

        langfuse_service.eager_initialize()

        mock_client_instance.eager_initialize.assert_called_once_with()

    @patch("code_indexer.server.services.langfuse_service.LangfuseClient")
    def test_eager_initialize_does_not_raise_on_client_failure(
        self, mock_client_class, langfuse_service, mock_config_manager
    ):
        """
        If client.eager_initialize() raises, eager_initialize() must swallow
        the exception and log a warning. Server startup must not be blocked
        by Langfuse initialization failures.
        """
        config = Mock(spec=ServerConfig)
        config.langfuse_config = LangfuseConfig(
            enabled=True,
            public_key="bad-key",
            secret_key="bad-secret",
            host="https://example.com",
        )
        mock_config_manager.load_config.return_value = config

        mock_client_instance = Mock()
        mock_client_instance.eager_initialize.side_effect = RuntimeError(
            "Network unreachable"
        )
        mock_client_class.return_value = mock_client_instance

        # Must not raise
        langfuse_service.eager_initialize()

    @patch("code_indexer.server.services.langfuse_service.LangfuseClient")
    def test_eager_initialize_initializes_client_first(
        self, mock_client_class, langfuse_service, mock_config_manager
    ):
        """
        eager_initialize() must first ensure the client is created
        (by accessing the client property), then call eager_initialize on it.
        The client must not be None after eager_initialize() returns.
        """
        config = Mock(spec=ServerConfig)
        config.langfuse_config = LangfuseConfig(
            enabled=True,
            public_key="test-key",
            secret_key="test-secret",
            host="https://example.com",
        )
        mock_config_manager.load_config.return_value = config

        mock_client_instance = Mock()
        mock_client_class.return_value = mock_client_instance

        # Client must be None before eager_initialize
        assert langfuse_service._client is None

        langfuse_service.eager_initialize()

        # Client must be set after eager_initialize
        assert langfuse_service._client is not None
        assert langfuse_service._client is mock_client_instance


class TestCleanupSession:
    """Tests for cleanup_session method."""

    def test_cleanup_session_when_trace_manager_exists(self, langfuse_service):
        """Test cleanup_session calls trace_manager.cleanup_session."""
        mock_trace_manager = Mock()
        langfuse_service._trace_manager = mock_trace_manager

        langfuse_service.cleanup_session("session-123")

        mock_trace_manager.cleanup_session.assert_called_once_with("session-123")

    def test_cleanup_session_when_no_trace_manager(self, langfuse_service):
        """Test cleanup_session does nothing when trace_manager is None."""
        langfuse_service._trace_manager = None

        # Should not raise
        langfuse_service.cleanup_session("session-123")


class TestGetLangfuseService:
    """Tests for get_langfuse_service singleton."""

    @patch("code_indexer.server.services.langfuse_service.ServerConfigManager")
    @patch("code_indexer.server.services.langfuse_service.LangfuseService")
    def test_get_langfuse_service_creates_singleton(
        self, mock_service_class, mock_config_manager_class
    ):
        """Test that get_langfuse_service creates singleton on first call."""
        mock_config_manager = Mock()
        mock_config_manager_class.return_value = mock_config_manager

        mock_service_instance = Mock()
        mock_service_class.return_value = mock_service_instance

        service = get_langfuse_service()

        assert service is mock_service_instance
        mock_service_class.assert_called_once_with(mock_config_manager)

    @patch("code_indexer.server.services.langfuse_service.ServerConfigManager")
    @patch("code_indexer.server.services.langfuse_service.LangfuseClient")
    def test_get_langfuse_service_returns_same_instance(
        self, mock_client_class, mock_config_manager_class
    ):
        """Test that subsequent calls return the same singleton."""
        mock_config_manager = Mock()
        mock_config_manager_class.return_value = mock_config_manager
        config = Mock(spec=ServerConfig)
        config.langfuse_config = LangfuseConfig(
            enabled=False,
            public_key="test-key",
            secret_key="test-secret",
            host="https://example.com",
        )
        mock_config_manager.load_config.return_value = config

        mock_client_class.return_value = Mock()

        service1 = get_langfuse_service()
        service2 = get_langfuse_service()

        assert service1 is service2


class TestResetLangfuseService:
    """Tests for reset_langfuse_service."""

    @patch("code_indexer.server.services.langfuse_service.ServerConfigManager")
    @patch("code_indexer.server.services.langfuse_service.LangfuseClient")
    def test_reset_langfuse_service_clears_singleton(
        self, mock_client_class, mock_config_manager_class
    ):
        """Test that reset clears the singleton."""
        mock_config_manager = Mock()
        mock_config_manager_class.return_value = mock_config_manager
        config = Mock(spec=ServerConfig)
        config.langfuse_config = LangfuseConfig(
            enabled=False,
            public_key="test-key",
            secret_key="test-secret",
            host="https://example.com",
        )
        mock_config_manager.load_config.return_value = config

        mock_client_class.return_value = Mock()

        # Create singleton
        service1 = get_langfuse_service()

        # Reset
        reset_langfuse_service()

        # Next call should create new instance
        service2 = get_langfuse_service()
        assert service2 is not None
        assert service2 is not service1


class TestThreadSafety:
    """Tests for thread-safe lazy initialization."""

    @patch("code_indexer.server.services.langfuse_service.LangfuseClient")
    def test_concurrent_client_access_returns_same_instance(
        self, mock_client_class, mock_config_manager
    ):
        """Test that concurrent access to client property is thread-safe."""
        config = Mock(spec=ServerConfig)
        config.langfuse_config = LangfuseConfig(
            enabled=False,
            public_key="test-key",
            secret_key="test-secret",
            host="https://example.com",
        )
        mock_config_manager.load_config.return_value = config

        mock_client_instance = Mock()
        mock_client_class.return_value = mock_client_instance

        service = LangfuseService(mock_config_manager)

        # Collect client instances from multiple threads
        results = []

        def access_client():
            results.append(service.client)

        threads = [threading.Thread(target=access_client) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All threads should get the same instance
        assert all(client is mock_client_instance for client in results)
        # Client should only be initialized once despite concurrent access
        mock_client_class.assert_called_once()

    @patch("code_indexer.server.services.langfuse_service.TraceStateManager")
    @patch("code_indexer.server.services.langfuse_service.LangfuseClient")
    def test_concurrent_trace_manager_access_returns_same_instance(
        self, mock_client_class, mock_trace_manager_class, mock_config_manager
    ):
        """Test concurrent access to trace_manager is thread-safe."""
        config = Mock(spec=ServerConfig)
        config.langfuse_config = LangfuseConfig(
            enabled=False,
            public_key="test-key",
            secret_key="test-secret",
            host="https://example.com",
        )
        mock_config_manager.load_config.return_value = config

        mock_client_class.return_value = Mock()
        mock_trace_manager_instance = Mock()
        mock_trace_manager_class.return_value = mock_trace_manager_instance

        service = LangfuseService(mock_config_manager)

        results = []

        def access_trace_manager():
            results.append(service.trace_manager)

        threads = [threading.Thread(target=access_trace_manager) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All threads should get the same instance
        assert all(manager is mock_trace_manager_instance for manager in results)
        # TraceStateManager should only be initialized once
        mock_trace_manager_class.assert_called_once()

    @patch("code_indexer.server.services.langfuse_service.ServerConfigManager")
    @patch("code_indexer.server.services.langfuse_service.LangfuseClient")
    def test_concurrent_singleton_access_returns_same_instance(
        self, mock_client_class, mock_config_manager_class
    ):
        """Test concurrent access to singleton is thread-safe."""
        mock_config_manager = Mock()
        mock_config_manager_class.return_value = mock_config_manager
        config = Mock(spec=ServerConfig)
        config.langfuse_config = LangfuseConfig(
            enabled=False,
            public_key="test-key",
            secret_key="test-secret",
            host="https://example.com",
        )
        mock_config_manager.load_config.return_value = config

        mock_client_class.return_value = Mock()

        results = []

        def access_singleton():
            results.append(get_langfuse_service())

        threads = [threading.Thread(target=access_singleton) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All threads should get the same singleton instance
        first = results[0]
        assert all(service is first for service in results)
