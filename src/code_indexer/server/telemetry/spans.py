"""
Custom Spans for Key Operations (Story #700).

This module provides utilities for creating custom OTEL spans for key
operations beyond HTTP request boundaries. Provides the create_span()
context manager.

Usage:
    from code_indexer.server.telemetry.spans import create_span

    # Using context manager
    with create_span("cidx.git.clone", attributes={"repo": url}) as span:
        # Do work
        span.set_attribute("files_count", 100)
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Dict, Generator, Optional

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

logger = logging.getLogger(__name__)

# Module-level tracer cache
_tracer: Optional["Tracer"] = None
_tracing_enabled: bool = False


def get_tracer(name: str = "cidx.spans") -> Optional["Tracer"]:
    """
    Get or create a tracer for creating spans.

    Args:
        name: Tracer name (instrument name)

    Returns:
        Tracer instance or None if tracing unavailable
    """
    global _tracer, _tracing_enabled

    if _tracer is not None:
        return _tracer

    try:
        from opentelemetry import trace

        # Check if we have a real tracer provider (not NoOpTracerProvider)
        tracer = trace.get_tracer(name)
        _tracer = tracer
        _tracing_enabled = True
        return tracer
    except ImportError:
        logger.debug("OpenTelemetry not available")
        return None
    except Exception as e:
        logger.debug(f"Failed to get tracer: {e}")
        return None


def _get_correlation_id() -> Optional[str]:
    """Get current correlation ID from context."""
    try:
        from code_indexer.server.telemetry.correlation_bridge import (
            get_current_correlation_id,
        )

        correlation_id: Optional[str] = get_current_correlation_id()
        return correlation_id
    except ImportError:
        return None
    except Exception:
        return None


@contextmanager
def create_span(
    name: str,
    attributes: Optional[Dict[str, Any]] = None,
    record_exception: bool = True,
) -> Generator[Any, None, None]:
    """
    Context manager for creating a custom span.

    Args:
        name: Span name (e.g., "cidx.search.semantic")
        attributes: Optional attributes to set on span
        record_exception: Whether to record exceptions on span

    Yields:
        Span object (or NoOp span if tracing disabled)

    Example:
        with create_span("cidx.git.clone", attributes={"repo": url}) as span:
            # Do work
            span.set_attribute("files_count", 100)
    """
    tracer = get_tracer()

    if tracer is None:
        yield _NoOpSpan()
        return

    try:
        from opentelemetry import context
        from opentelemetry.trace import Status, StatusCode, set_span_in_context
    except ImportError:
        yield _NoOpSpan()
        return

    # Start span and manage context manually
    span = tracer.start_span(name)
    ctx = set_span_in_context(span)
    token = context.attach(ctx)

    try:
        # Add correlation ID if available
        correlation_id = _get_correlation_id()
        if correlation_id:
            span.set_attribute("correlation.id", correlation_id)

        # Add custom attributes
        if attributes:
            for key, value in attributes.items():
                if value is not None:
                    span.set_attribute(key, value)

        yield span

    except Exception as e:
        if record_exception:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
        raise

    finally:
        span.end()
        context.detach(token)


class _NoOpSpan:
    """No-op span for when tracing is disabled."""

    def set_attribute(self, key: str, value: Any) -> None:
        """No-op set attribute."""
        pass

    def add_event(self, name: str, attributes: Optional[Dict[str, Any]] = None) -> None:
        """No-op add event."""
        pass

    def record_exception(self, exception: Exception) -> None:
        """No-op record exception."""
        pass

    def set_status(self, status: Any) -> None:
        """No-op set status."""
        pass

    def is_recording(self) -> bool:
        """Return False for no-op span."""
        return False


def reset_spans_state() -> None:
    """Reset module state (for testing)."""
    global _tracer, _tracing_enabled
    _tracer = None
    _tracing_enabled = False
