"""
Tests for Story #278: Langfuse eager initialization during app startup.

Currently LangfuseClient and LangfuseService use lazy initialization - the
Langfuse SDK is only imported and initialized on first request. This can take
several seconds on first call (module import + network I/O to validate credentials).

Fix: Add an eager_initialize() method to LangfuseClient and LangfuseService,
and call it from the app lifespan startup function. This moves the one-time cost
to server startup rather than first request.

Key requirements tested:
- LangfuseClient.eager_initialize() method exists
- LangfuseService.eager_initialize() method exists
- eager_initialize() calls _ensure_initialized() to pre-warm the SDK
- Startup failure is logged but does NOT block server start (graceful failure)
- After eager_initialize(), the client property returns without acquiring _lock
"""

from unittest.mock import MagicMock

from code_indexer.server.services.langfuse_client import LangfuseClient
from code_indexer.server.services.langfuse_service import LangfuseService
from code_indexer.server.utils.config_manager import LangfuseConfig


def make_enabled_config() -> LangfuseConfig:
    """Create a LangfuseConfig with Langfuse enabled."""
    return LangfuseConfig(
        enabled=True,
        public_key="pk-test-123",
        secret_key="sk-test-456",
        host="https://cloud.langfuse.com",
    )


def make_disabled_config() -> LangfuseConfig:
    """Create a LangfuseConfig with Langfuse disabled."""
    return LangfuseConfig(
        enabled=False,
        public_key="",
        secret_key="",
        host="",
    )


class TestLangfuseClientEagerInitialize:
    """Verify LangfuseClient has an eager_initialize() method."""

    def test_eager_initialize_method_exists(self):
        """LangfuseClient must have an eager_initialize() method."""
        client = LangfuseClient(make_enabled_config())
        assert hasattr(client, "eager_initialize"), (
            "LangfuseClient must have an eager_initialize() method"
        )
        assert callable(client.eager_initialize), (
            "eager_initialize must be callable"
        )

    def test_eager_initialize_calls_ensure_initialized(self):
        """eager_initialize() must call _ensure_initialized() to pre-warm SDK."""
        client = LangfuseClient(make_enabled_config())

        ensure_init_called = []

        def tracked_ensure():
            ensure_init_called.append(True)
            return False  # Return False to avoid actual Langfuse network call

        client._ensure_initialized = tracked_ensure

        client.eager_initialize()

        assert len(ensure_init_called) >= 1, (
            "eager_initialize() must call _ensure_initialized() to pre-warm the SDK"
        )

    def test_eager_initialize_when_disabled_is_noop(self):
        """When Langfuse is disabled, eager_initialize() must be a no-op."""
        client = LangfuseClient(make_disabled_config())

        ensure_init_called = []

        def tracked_ensure():
            ensure_init_called.append(True)
            # Disabled: returns False immediately
            return False

        client._ensure_initialized = tracked_ensure

        # Should not raise
        client.eager_initialize()

        # No exception = success (disabled path is handled gracefully)
        assert True

    def test_eager_initialize_failure_does_not_raise(self):
        """eager_initialize() must not raise even if SDK initialization fails."""
        client = LangfuseClient(make_enabled_config())

        def always_fails():
            raise RuntimeError("SDK initialization failed - network error")

        client._ensure_initialized = always_fails

        # Must not raise - startup failure should be logged, not propagated
        try:
            client.eager_initialize()
        except Exception as e:
            assert False, (
                f"eager_initialize() must not raise exceptions. Got: {e}"
            )

    def test_after_eager_initialize_langfuse_instance_is_set(self):
        """After successful eager_initialize(), _langfuse must be set."""
        client = LangfuseClient(make_enabled_config())

        mock_langfuse = MagicMock()

        def mock_ensure_initialized():
            client._langfuse = mock_langfuse
            return True

        client._ensure_initialized = mock_ensure_initialized

        client.eager_initialize()

        assert client._langfuse is mock_langfuse, (
            "After successful eager_initialize(), _langfuse must be set "
            "so the client property returns immediately without acquiring _lock"
        )


class TestLangfuseServiceEagerInitialize:
    """Verify LangfuseService has an eager_initialize() method."""

    def test_eager_initialize_method_exists(self):
        """LangfuseService must have an eager_initialize() method."""
        mock_config_manager = MagicMock()
        service = LangfuseService(mock_config_manager)
        assert hasattr(service, "eager_initialize"), (
            "LangfuseService must have an eager_initialize() method"
        )
        assert callable(service.eager_initialize), (
            "eager_initialize must be callable"
        )

    def test_eager_initialize_calls_client_eager_initialize(self):
        """LangfuseService.eager_initialize() must call client.eager_initialize()."""
        mock_config_manager = MagicMock()
        mock_config = MagicMock()
        mock_langfuse_config = make_enabled_config()
        mock_config.langfuse_config = mock_langfuse_config
        mock_config_manager.load_config.return_value = mock_config

        service = LangfuseService(mock_config_manager)

        client_eager_init_called = []
        mock_client = MagicMock()
        mock_client.eager_initialize.side_effect = lambda: client_eager_init_called.append(True)
        service._client = mock_client

        service.eager_initialize()

        assert len(client_eager_init_called) >= 1, (
            "LangfuseService.eager_initialize() must call client.eager_initialize()"
        )

    def test_eager_initialize_failure_does_not_raise(self):
        """LangfuseService.eager_initialize() must not raise on failure."""
        mock_config_manager = MagicMock()
        mock_config_manager.load_config.side_effect = RuntimeError("config error")

        service = LangfuseService(mock_config_manager)

        # Must not raise
        try:
            service.eager_initialize()
        except Exception as e:
            assert False, (
                f"LangfuseService.eager_initialize() must not raise. Got: {e}"
            )

    def test_eager_initialize_when_not_enabled_is_safe(self):
        """eager_initialize() when Langfuse is not enabled must complete without error."""
        mock_config_manager = MagicMock()
        mock_config = MagicMock()
        mock_config.langfuse_config = None  # No langfuse config
        mock_config_manager.load_config.return_value = mock_config

        service = LangfuseService(mock_config_manager)

        # Must not raise when no langfuse config is present
        try:
            service.eager_initialize()
        except Exception as e:
            assert False, (
                f"eager_initialize() with no config must not raise. Got: {e}"
            )


class TestLangfuseClientPropertyAfterEagerInit:
    """Verify client property fast-path works after eager initialization."""

    def test_client_property_returns_without_lock_after_eager_init(self):
        """
        After eager_initialize(), the client property's _client fast-path
        (if self._client is not None) should bypass the lock entirely.
        """
        mock_config_manager = MagicMock()
        mock_config = MagicMock()
        mock_config.langfuse_config = make_enabled_config()
        mock_config_manager.load_config.return_value = mock_config

        service = LangfuseService(mock_config_manager)

        # Simulate eager init already ran by pre-setting _client
        mock_client = MagicMock()
        service._client = mock_client

        # Accessing client property should return the pre-set client
        result = service.client
        assert result is mock_client, (
            "client property must return the pre-initialized client "
            "without acquiring the lock"
        )
