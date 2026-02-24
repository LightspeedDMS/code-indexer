"""
Langfuse Client Service for CIDX Server (Story #136).

Provides a wrapper around the Langfuse Python SDK with:
- Lazy initialization (SDK only imported/initialized when first used)
- Graceful disabled mode (all operations no-op when disabled)
- Thread-safe singleton pattern for SDK instance (double-check locking)
- Comprehensive error handling
- Trace, span, and scoring operations
"""

import logging
import threading
from typing import Optional, Dict, Any

from code_indexer.server.utils.config_manager import LangfuseConfig

logger = logging.getLogger(__name__)


class LangfuseClient:
    """
    Wrapper around Langfuse Python SDK for research session tracing.

    Provides lazy initialization - the Langfuse SDK is only imported and
    initialized on first use. When disabled, all operations are no-ops.

    Thread-safe singleton pattern with double-check locking ensures only
    one SDK instance per client even in concurrent scenarios.

    Example usage:
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        # Lazy init happens on first call
        trace = client.create_trace(name="research", session_id="session-1")
        if trace:
            span = client.create_span(trace_id=trace.id, name="search_code")
            client.score(trace_id=trace.id, name="quality", value=0.9)
            client.flush()
    """

    def __init__(self, config: LangfuseConfig):
        """
        Initialize LangfuseClient with configuration.

        Args:
            config: LangfuseConfig object containing credentials and settings
        """
        self._config = config
        self._langfuse = None  # Lazy initialization
        self._lock = threading.Lock()  # Thread-safe initialization

    def is_enabled(self) -> bool:
        """Check if Langfuse tracing is enabled."""
        return self._config.enabled

    def eager_initialize(self) -> None:
        """
        Pre-initialize the Langfuse SDK during application startup.

        Calling this during the lifespan startup function moves the one-time
        SDK initialization cost (module import + network I/O) to server startup
        rather than the first request. After eager_initialize() completes, the
        _langfuse instance is set and subsequent calls use the fast-path in
        _ensure_initialized() without acquiring the lock.

        Failure is logged but does NOT raise - startup must continue even if
        Langfuse credentials are invalid or the network is unavailable.
        """
        try:
            self._ensure_initialized()
        except Exception as e:
            logger.warning(f"Langfuse eager initialization failed (non-fatal): {e}")

    def _ensure_initialized(self) -> bool:
        """
        Ensure Langfuse SDK is initialized (lazy init with thread safety).

        Uses double-check locking pattern to avoid race conditions while
        minimizing lock contention.

        Returns:
            True if initialized successfully, False if disabled or error
        """
        if not self._config.enabled:
            return False

        # Fast path: already initialized
        if self._langfuse is not None:
            return True

        # Slow path: initialize with lock
        with self._lock:
            # Double-check: another thread may have initialized while we waited
            if self._langfuse is not None:
                return True

            try:
                # Lazy import - only import Langfuse when actually needed
                from langfuse import Langfuse

                self._langfuse = Langfuse(
                    public_key=self._config.public_key,
                    secret_key=self._config.secret_key,
                    host=self._config.host,
                )
                logger.info("Langfuse SDK initialized successfully")
                return True

            except Exception as e:
                logger.error(f"Failed to initialize Langfuse SDK: {e}")
                return False

    def create_trace(
        self,
        name: str,
        session_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
        input: Optional[str] = None,
        tags: Optional[list] = None,
    ) -> Optional[Any]:
        """
        Create a new trace in Langfuse.

        Langfuse 3.7.0 API creates traces implicitly when starting root spans.
        We use start_as_current_span() to create the root span (which creates
        the trace), then update_current_trace() to set session_id/user_id/tags.

        The span is stored in the returned TraceObject and must be ended via
        end_trace() before flush() will send data to Langfuse.

        Args:
            name: Name of the trace (e.g., "Authentication Investigation")
            session_id: MCP session ID
            metadata: Optional metadata dict (strategy, intel_*, etc.)
            user_id: Optional user identifier
            input: Optional user prompt text (Story #185)
            tags: Optional list of tags for categorization (Story #185)

        Returns:
            TraceObject with .id, .trace_id, and .span properties if successful, None otherwise
        """
        if not self._ensure_initialized():
            return None

        try:
            # Create root span with context (creates trace implicitly)
            # Use end_on_exit=False so we control lifecycle manually via end_trace()
            span_cm = self._langfuse.start_as_current_span(
                name=name,
                metadata=metadata,
                input=input,
                end_on_exit=False,
            )

            # Enter context to activate span
            span = span_cm.__enter__()

            # Update trace with session_id, user_id, and tags
            update_kwargs = {
                "session_id": session_id,
                "user_id": user_id,
            }
            if tags is not None:
                update_kwargs["tags"] = tags

            self._langfuse.update_current_trace(**update_kwargs)

            # Exit context but keep span active - it will be ended in end_trace()
            span_cm.__exit__(None, None, None)

            logger.debug(f"Created trace: {name} (session={session_id}, trace_id={span.trace_id})")

            # Return object with span stored for later ending
            # TraceStateManager accesses trace.id and we store span for end_trace()
            class TraceObject:
                def __init__(self, trace_id: str, span_obj: Any):
                    self.id = trace_id  # TraceStateManager expects .id
                    self.trace_id = trace_id  # Also provide .trace_id for consistency
                    self.span = span_obj  # Store span for end_trace() to call .end()

            return TraceObject(span.trace_id, span)

        except Exception as e:
            logger.error(f"Failed to create trace '{name}': {e}")
            return None

    def update_current_trace_in_context(
        self,
        span: Any,
        output: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[list] = None,
    ) -> bool:
        """
        Update a trace by re-entering its span context and calling update_current_trace.

        Story #185: Langfuse SDK 3.7.0 only has update_current_trace(), which updates
        the currently active trace in the context. To update a trace that's no longer
        current, we must re-enter the span's context.

        Args:
            span: The span object returned from start_as_current_span (stored in TraceObject)
            output: Optional output text (Claude's response)
            metadata: Optional metadata to merge into trace
            tags: Optional list of tags to add

        Returns:
            True if update succeeded, False otherwise
        """
        if not self._ensure_initialized():
            return False

        try:
            # Re-enter the span's context to make it current
            # The span was created with end_on_exit=False, so we can safely enter/exit multiple times
            span_cm = span  # The span object IS the context manager
            span_cm.__enter__()
            try:
                # Now update_current_trace will work because span is active
                update_kwargs = {}
                if output is not None:
                    update_kwargs["output"] = output
                if metadata is not None:
                    update_kwargs["metadata"] = metadata
                if tags is not None:
                    update_kwargs["tags"] = tags

                self._langfuse.update_current_trace(**update_kwargs)
            finally:
                # Always exit context (but don't end span - that happens in end_trace)
                span_cm.__exit__(None, None, None)

            logger.debug(f"Updated trace {span.trace_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to update trace: {e}")
            return False

    def end_trace(self, trace_obj: Any) -> bool:
        """
        End a trace by ending its root span.

        This MUST be called before flush() to ensure trace data is sent to Langfuse.
        The Langfuse SDK only sends data for completed (ended) spans.

        Args:
            trace_obj: TraceObject returned from create_trace() containing the span

        Returns:
            True if span was ended successfully, False otherwise
        """
        if not self._config.enabled:
            return False

        if trace_obj is None:
            return False

        try:
            # End the span - this marks it as complete so flush() will send it
            if hasattr(trace_obj, 'span') and trace_obj.span is not None:
                trace_obj.span.end()
                logger.debug(f"Ended trace span: {trace_obj.trace_id}")
                return True
            else:
                logger.warning(f"TraceObject has no span to end: {trace_obj.trace_id}")
                return False

        except Exception as e:
            logger.error(f"Failed to end trace {getattr(trace_obj, 'trace_id', 'unknown')}: {e}")
            return False

    def create_span(
        self,
        trace_id: str,
        name: str,
        metadata: Optional[Dict[str, Any]] = None,
        input_data: Optional[Dict[str, Any]] = None,
        output_data: Optional[Dict[str, Any]] = None,
    ) -> Optional[Any]:
        """
        Create a new span within a trace.

        Langfuse 3.7.0 API uses start_span() with a TraceContext parameter
        to attach spans to specific traces.

        Args:
            trace_id: ID of parent trace
            name: Name of the span (e.g., "search_code", "list_files")
            metadata: Optional metadata dict
            input_data: Optional input data (tool arguments)
            output_data: Optional output data (tool results)

        Returns:
            Langfuse span object if successful, None otherwise
        """
        if not self._ensure_initialized():
            return None

        try:
            # Import TraceContext for type hints
            from langfuse.types import TraceContext

            # Create TraceContext to attach span to existing trace
            trace_context = TraceContext(trace_id=trace_id)

            # Create span attached to the trace
            span = self._langfuse.start_span(
                trace_context=trace_context,
                name=name,
                metadata=metadata,
                input=input_data,
                output=output_data,
            )
            logger.debug(f"Created span: {name} (trace={trace_id})")
            return span

        except Exception as e:
            logger.error(f"Failed to create span '{name}': {e}")
            return None

    def score(
        self, trace_id: str, name: str, value: float, comment: Optional[str] = None
    ) -> Optional[Any]:
        """
        Add a score to a trace (for user feedback, quality assessment).

        Langfuse 3.7.0 API uses create_score() method.

        Args:
            trace_id: ID of the trace to score
            name: Name of the score (e.g., "user-feedback", "quality")
            value: Score value (typically 0.0 to 1.0)
            comment: Optional comment or feedback text

        Returns:
            Langfuse score object if successful, None otherwise
        """
        if not self._ensure_initialized():
            return None

        try:
            # create_score() is the correct method name in Langfuse 3.7.0
            score = self._langfuse.create_score(
                trace_id=trace_id, name=name, value=value, comment=comment
            )
            logger.debug(f"Added score to trace {trace_id}: {name}={value}")
            return score

        except Exception as e:
            logger.error(f"Failed to add score to trace {trace_id}: {e}")
            return None

    def flush(self) -> None:
        """
        Flush all pending traces/spans to Langfuse.

        Should be called when ending a trace or at shutdown to ensure
        all data is sent before the process exits.
        """
        if not self._config.enabled:
            return

        if self._langfuse is None:
            return

        try:
            self._langfuse.flush()
            logger.debug("Flushed Langfuse traces")

        except Exception as e:
            logger.error(f"Failed to flush Langfuse: {e}")
