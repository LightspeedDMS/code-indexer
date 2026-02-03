"""Langfuse service facade - provides unified access to tracing components."""

from typing import Optional
import threading

from .langfuse_client import LangfuseClient
from .trace_state_manager import TraceStateManager
from .auto_span_logger import AutoSpanLogger
from ..utils.config_manager import ServerConfigManager

_service_instance: Optional["LangfuseService"] = None
_service_lock = threading.Lock()


class LangfuseService:
    """Facade providing access to all Langfuse tracing components."""

    def __init__(self, config_manager: ServerConfigManager):
        self._config_manager = config_manager
        self._client: Optional[LangfuseClient] = None
        self._trace_manager: Optional[TraceStateManager] = None
        self._span_logger: Optional[AutoSpanLogger] = None
        self._lock = threading.RLock()  # RLock allows nested acquisition (trace_manager -> client)

    def is_enabled(self) -> bool:
        """Check if Langfuse is enabled in config."""
        config = self._config_manager.load_config()
        if config and config.langfuse_config:
            return config.langfuse_config.enabled
        return False

    @property
    def client(self) -> LangfuseClient:
        """Get or create LangfuseClient (lazy init)."""
        if self._client is None:
            with self._lock:
                if self._client is None:
                    config = self._config_manager.load_config()
                    langfuse_config = config.langfuse_config if config else None
                    self._client = LangfuseClient(langfuse_config)
        return self._client

    @property
    def trace_manager(self) -> TraceStateManager:
        """Get or create TraceStateManager (lazy init)."""
        if self._trace_manager is None:
            with self._lock:
                if self._trace_manager is None:
                    self._trace_manager = TraceStateManager(self.client)
        return self._trace_manager

    @property
    def span_logger(self) -> AutoSpanLogger:
        """Get or create AutoSpanLogger (lazy init)."""
        if self._span_logger is None:
            with self._lock:
                if self._span_logger is None:
                    # Story #136 follow-up: Pass config for auto-trace functionality
                    config = self._config_manager.load_config()
                    langfuse_config = config.langfuse_config if config else None
                    self._span_logger = AutoSpanLogger(
                        self.trace_manager, self.client, langfuse_config
                    )
        return self._span_logger

    def cleanup_session(self, session_id: str) -> None:
        """Clean up trace state for a disconnected session."""
        if self._trace_manager:
            self._trace_manager.cleanup_session(session_id)


def get_langfuse_service() -> LangfuseService:
    """Get the global LangfuseService singleton."""
    global _service_instance
    if _service_instance is None:
        with _service_lock:
            if _service_instance is None:
                # Instantiate ServerConfigManager directly (standard pattern in codebase)
                _service_instance = LangfuseService(ServerConfigManager())
    return _service_instance


def reset_langfuse_service() -> None:
    """Reset the global service (for testing)."""
    global _service_instance
    with _service_lock:
        _service_instance = None
