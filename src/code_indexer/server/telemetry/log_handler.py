"""
Log Correlation with Trace Context (Story #701, extended by Story #1676 AC2).

This module provides ``get_trace_context()``, which reads the currently
active OTEL span (if any) and returns its trace/span IDs so they can be
attached to Python logging records.

Fields returned by get_trace_context():
- trace_id (32-char hex) - OTEL trace ID
- span_id (16-char hex) - OTEL span ID
- dd.trace_id - Datadog-compatible trace ID (decimal)
- dd.span_id - Datadog-compatible span ID (decimal)

Story #1676 AC2 note: the columnar-storage approach (dedicated trace_id/
span_id columns on the ``logs`` table in both the SQLite and PostgreSQL
backends, populated via ``logging_utils.inject_trace_context()`` at the
``IdentityQueueHandler.prepare()`` wiring point in ``async_logging.py``)
supersedes the format-string-injection approach this module used to also
provide via ``OTELLogFormatter``/``OTELLogHandler``. Those two classes were
never wired into the production logging pipeline -- no ``lifespan.py`` call
site (or anywhere else) ever attached either of them to a real logger -- so
they have been removed as orphaned code (Messi Rule #12: wire it or don't
write it). The real, wired mechanism for log/trace correlation is the
columnar one. Use ``get_trace_context()`` directly whenever trace/span IDs
are needed outside the logging pipeline (e.g. inside a formatter of your
own, or a one-off log line).

Usage:
    from code_indexer.server.telemetry.log_handler import get_trace_context

    context = get_trace_context()
    logger.info(
        "message trace_id=%s span_id=%s", context["trace_id"], context["span_id"]
    )
"""

from __future__ import annotations

import logging
from typing import Dict

logger = logging.getLogger(__name__)

# Zero values for when no trace context is available
ZERO_TRACE_ID = "0" * 32
ZERO_SPAN_ID = "0" * 16

# Mask for extracting lower 64 bits for Datadog compatibility
# Datadog uses 64-bit trace IDs while OTEL uses 128-bit
DATADOG_64BIT_MASK = 0xFFFFFFFFFFFFFFFF


def get_trace_context() -> Dict[str, str]:
    """
    Get current trace context from active OTEL span.

    Returns:
        Dictionary with trace_id (32-char hex), span_id (16-char hex),
        and Datadog-compatible fields (dd.trace_id, dd.span_id).
    """
    trace_id = ZERO_TRACE_ID
    span_id = ZERO_SPAN_ID
    dd_trace_id = "0"
    dd_span_id = "0"

    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span and span.is_recording():
            span_context = span.get_span_context()
            if span_context and span_context.is_valid:
                # Format as 32-char hex for trace_id, 16-char hex for span_id
                trace_id = format(span_context.trace_id, "032x")
                span_id = format(span_context.span_id, "016x")

                # Datadog expects decimal representation with lower 64 bits
                dd_trace_id = str(span_context.trace_id & DATADOG_64BIT_MASK)
                dd_span_id = str(span_context.span_id)

    except ImportError:
        # OpenTelemetry not available
        pass
    except Exception as e:
        # Log at debug level to avoid noise
        logger.debug(f"Failed to get trace context: {e}")

    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "dd.trace_id": dd_trace_id,
        "dd.span_id": dd_span_id,
    }
