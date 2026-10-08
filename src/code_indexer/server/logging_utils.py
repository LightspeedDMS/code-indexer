"""
Logging utilities for CIDX server.

Provides helper functions for formatting log messages with error codes,
correlation IDs, and sanitized data.

Usage:
    from code_indexer.server.logging_utils import format_error_log, get_log_extra

    logger.error(
        format_error_log("APP-GENERAL-001", "AUTH-OIDC-001"),
        extra=get_log_extra("APP-GENERAL-001")
    )
"""

import logging
from typing import Any, Dict

# Bug #2012: the single, layer-neutral credential redaction implementation
# lives in code_indexer.utils (CLI-side utilities use it without importing
# server code); re-exported here for existing server importers.
from code_indexer.utils.credential_redaction import (  # noqa: F401 - re-export
    mask_url_credentials,
    redact_command,
    redact_command_output,
    redact_secret_fields,
)


def format_error_log(error_code: str, message: str, **context) -> str:
    """
    Format an error log message with error code and optional context.

    Args:
        error_code: Error code in format {SUBSYSTEM}-{CATEGORY}-{NUMBER}
        message: Human-readable error message
        **context: Additional context key-value pairs to include

    Returns:
        Formatted log message: "[{ERROR_CODE}] message key1=value1 key2=value2"

    Raises:
        TypeError: if ``extra`` is passed in ``**context``. This is a
            plain string-formatting helper, not ``logger.error()``/
            ``logger.warning()`` -- it has no real ``extra=`` mechanism.
            A caller intending to reach Python logging's real ``extra=``
            parameter must pass it to the ``logger.*()`` call itself
            (see ``get_log_extra()``), never to this function. Bug
            #1649/#1716: 693 call sites across the codebase previously
            passed ``extra={...}`` here, silently stringifying the dict
            verbatim into the log message instead of ever reaching
            structured logging.

    Examples:
        >>> format_error_log("AUTH-OIDC-001", "Connection failed", issuer="https://example.com")
        '[AUTH-OIDC-001] Connection failed issuer=https://example.com'

        >>> format_error_log("MCP-TOOL-042", "Tool execution failed")
        '[MCP-TOOL-042] Tool execution failed'
    """
    if "extra" in context:
        raise TypeError(
            "format_error_log() does not accept 'extra'; pass extra= to "
            "logger.error()/logger.warning() instead (Bug #1649/#1716)"
        )

    parts = [f"[{error_code}]", message]

    # Add context if provided
    if context:
        context_str = " ".join(f"{k}={v}" for k, v in context.items())
        parts.append(context_str)

    return " ".join(parts)


def get_log_extra(error_code: str) -> Dict[str, Any]:
    """
    Build the extra dict for logging with error_code and correlation_id.

    Args:
        error_code: Error code to include in extra dict

    Returns:
        Dictionary with error_code and correlation_id (if available)

    Examples:
        >>> extra = get_log_extra("AUTH-OIDC-001")
        logger.error(
            format_error_log("APP-GENERAL-002", "message"),
            extra=get_log_extra("APP-GENERAL-002")
        )
    """
    from code_indexer.server.middleware.correlation import get_correlation_id

    extra: Dict[str, Any] = {"error_code": error_code}

    # Add correlation_id if available
    correlation_id = get_correlation_id()
    if correlation_id:
        extra["correlation_id"] = correlation_id

    return extra


def inject_correlation_id(record: logging.LogRecord) -> None:
    """
    Populate ``record.correlation_id`` from the ambient request context,
    unless the call site already set one explicitly (Bug #1641).

    Background: get_correlation_id() (healed by #1631/#1632) correctly
    reads the correlation id for the CURRENT request/task context, but the
    log-store persistence handler (SQLiteLogHandler) only ever reads
    ``record.correlation_id`` -- an attribute that exists on a LogRecord
    ONLY when the logging call site explicitly passed
    ``extra={"correlation_id": ...}`` (e.g. via get_log_extra() above).
    The overwhelming majority of ``logger.info()/warning()/error()`` calls
    across the codebase pass no ``extra`` at all, so the record never
    carries the attribute and the log store's correlation_id COLUMN stays
    NULL even though the reader itself works correctly.

    This helper is the single, call-site-independent wiring point: it must
    be invoked as early as possible in the logging pipeline, on the
    ORIGINAL calling thread (before any hand-off to an async queue/listener
    thread), because ``get_correlation_id()`` resolves a ``contextvars``
    value that does NOT propagate across a plain ``threading.Thread``
    boundary. Callers: ``async_logging.IdentityQueueHandler.prepare()``
    (the real production wiring point -- runs on the request thread before
    the record is enqueued) and, defensively,
    ``SQLiteLogHandler.emit()`` (covers the non-queued direct-attach case).

    An explicitly-provided ``record.correlation_id`` (any truthy value) is
    NEVER overridden -- a call site that deliberately attributes a log line
    to a different correlation id (e.g. one propagated from an unrelated
    background job) must win over the ambient per-thread context.

    Args:
        record: The LogRecord to enrich in place. No-op if a correlation id
            is already present on the record, or if none is active in the
            current context (never fabricates a value).
    """
    if getattr(record, "correlation_id", None):
        return

    from code_indexer.server.middleware.correlation import get_correlation_id

    correlation_id = get_correlation_id()
    if correlation_id:
        record.correlation_id = correlation_id


def inject_trace_context(record: logging.LogRecord) -> None:
    """
    Populate ``record.trace_id`` / ``record.span_id`` from the currently
    active OTEL span (Story #1676 AC2), unless the call site already set
    both explicitly.

    Mirrors ``inject_correlation_id()``'s exact pattern and rationale: this
    must run on the ORIGINAL calling thread, before the record crosses into
    async_logging's queue/listener thread, because OTEL's "current span" is
    resolved via ``contextvars`` (inside ``get_trace_context()``), which does
    NOT propagate across a plain ``threading.Thread`` boundary. Callers:
    ``async_logging.IdentityQueueHandler.prepare()`` (the real production
    wiring point -- runs on the request thread before the record is
    enqueued) and, defensively, ``SQLiteLogHandler.emit()`` (covers the
    non-queued direct-attach case).

    Unlike ``inject_correlation_id()`` (which leaves ``correlation_id``
    unset/None when there is no active correlation id -- "never fabricate a
    value"), ``get_trace_context()`` ALWAYS returns a value: the documented
    zero-values ("0"*32 / "0"*16) when no span is active, or when telemetry
    is disabled. This function therefore ALWAYS leaves both
    ``record.trace_id`` and ``record.span_id`` populated as strings -- never
    None/absent -- so every stored log row has non-NULL trace_id/span_id
    columns in both storage backends, exactly per the AC2 contract.

    Args:
        record: The LogRecord to enrich in place. No-op if the record
            already carries BOTH ``trace_id`` and ``span_id`` (an explicitly
            provided pair, e.g. propagated from a different context, is
            never overridden).
    """
    if getattr(record, "trace_id", None) and getattr(record, "span_id", None):
        return

    from code_indexer.server.telemetry.log_handler import get_trace_context

    context = get_trace_context()
    record.trace_id = context["trace_id"]
    record.span_id = context["span_id"]


# Story #1676 AC3: private LogRecord attribute name carrying the full OTEL
# Context object captured by inject_otel_context() below. Deliberately named
# so it reads as "private" (leading underscore) even though it must be a
# public importable constant -- every module that needs to read, filter, or
# strip this attribute (the context-aware log bridge handler in
# async_logging.py, SQLiteLogHandler's extra_data JSON serialization, and any
# future OTLP-attribute translation exclusion) must agree on the EXACT same
# string, so this is the single source of truth rather than each site
# hardcoding its own copy.
OTEL_CONTEXT_RECORD_ATTR = "_otel_captured_context"


def inject_otel_context(record: logging.LogRecord) -> None:
    """
    Capture the full OTEL ``Context`` object active on the calling thread
    and attach it to ``record`` as a private attribute (Story #1676 AC3),
    unless the record already carries one.

    Background: unlike ``inject_trace_context()`` (which extracts just the
    trace_id/span_id STRINGS -- safe to store in any JSON/DB column),
    exporting a LogRecord to OTLP with correct trace/span correlation
    requires reattaching the FULL ``Context`` object at export time so the
    OTEL logging bridge handler's internal ``context.get_current()`` call
    resolves to the correct span. A raw ``Context`` object is NOT
    serializable (SQLite/PostgreSQL JSON storage, or OTLP attribute
    translation) -- callers on the export path are responsible for popping
    this attribute off the record before it reaches either serialization
    step (see async_logging.py's context-aware wrapper handler).

    Must run on the ORIGINAL calling thread, before the record crosses into
    async_logging's queue/listener thread -- OTEL's "current context" is
    resolved via ``contextvars``, which does NOT propagate across a plain
    ``threading.Thread`` boundary (same rationale as
    ``inject_correlation_id()``/``inject_trace_context()``). The real
    production wiring point is
    ``async_logging.IdentityQueueHandler.prepare()``.

    Args:
        record: The LogRecord to enrich in place. No-op if the record
            already carries the attribute (an explicitly captured/propagated
            context is never overridden).
    """
    if hasattr(record, OTEL_CONTEXT_RECORD_ATTR):
        return

    from opentelemetry import context as otel_context

    setattr(record, OTEL_CONTEXT_RECORD_ATTR, otel_context.get_current())


def sanitize_for_logging(data: Any) -> Any:
    """
    Sanitize data for logging by redacting sensitive information.

    Delegates to the shared redact_secret_fields(): the value of every
    secret-named key, at any depth, becomes "***REDACTED***"; None and plain
    strings without embedded credentials pass through unchanged.

    Args:
        data: Data to sanitize (dict, list, string, or other type)

    Returns:
        Sanitized copy of data with sensitive fields redacted

    Examples:
        >>> sanitize_for_logging({"username": "admin", "password": "secret"})
        {'username': 'admin', 'password': '***REDACTED***'}

        >>> sanitize_for_logging("plain string")
        'plain string'
    """
    return redact_secret_fields(data)


# Attributes every LogRecord carries; anything else came from ``extra=``
# (or an injector above).
_STANDARD_RECORD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime"}
_TRACEBACK_FORMATTER = logging.Formatter()


def _redact_message(record: logging.LogRecord) -> None:
    """Mask credentials in the record's formatted message, in place.

    A message with nothing to mask keeps its ``msg``/``args``. Otherwise a
    tuple of args is kept -- template and each text argument masked
    separately -- only when the recomposed message formats and holds
    nothing left to mask (formatters such as the HTTP access formatter
    read ``args``); else the message is flattened (``args`` cleared) and
    the colour template dropped so it cannot be printed unformatted."""
    message = record.getMessage()
    redacted = redact_secret_fields(message)
    if redacted == message:
        return
    if isinstance(record.args, tuple) and isinstance(record.msg, str):
        template = redact_secret_fields(record.msg)
        args = tuple(
            redact_secret_fields(arg) if isinstance(arg, str) else arg
            for arg in record.args
        )
        try:
            recomposed = template % args
        except (TypeError, ValueError, KeyError):
            recomposed = None
        if recomposed is not None and redact_secret_fields(recomposed) == recomposed:
            record.msg, record.args = template, args
            return
    record.msg = redacted
    record.args = None
    record.__dict__.pop("color_message", None)


REDACTION_FAILED_MESSAGE = "[log message withheld: redaction failed]"


def _withhold_record(record: logging.LogRecord) -> None:
    """Replace everything that could carry raw text with a fixed marker.

    Every extra attribute keeps its name (a formatter such as
    ``%(payload)s`` still resolves) but its value becomes the marker. The
    private OTEL context object is kept: it holds no text and the log
    bridge needs it as a context."""
    record.msg = REDACTION_FAILED_MESSAGE
    record.args = ()
    record.exc_info = None
    record.exc_text = None
    record.stack_info = None
    record.__dict__.pop("color_message", None)
    for key in list(record.__dict__):
        if key not in _STANDARD_RECORD_ATTRS and key != OTEL_CONTEXT_RECORD_ATTR:
            record.__dict__[key] = REDACTION_FAILED_MESSAGE


def redact_log_record(record: logging.LogRecord) -> logging.LogRecord:
    """Mask credential values in ``record`` IN PLACE and return it: the
    message (``_redact_message``), the traceback (``exc_info`` formatted
    into ``exc_text``, ``exc_info`` cleared), ``stack_info`` and every
    extra attribute. Idempotent. The private OTEL context object is left
    as is (it holds no text).

    Never raises: the filter runs on handlers outside the queue, so a
    failure here (a malformed ``msg % args``, an argument whose ``__str__``
    raises, ...) would reach application code. On any failure the record
    fails closed -- its message becomes ``REDACTION_FAILED_MESSAGE`` and
    its args, traceback, stack and colour template are dropped -- so the
    line is still emitted without its unredacted content."""
    try:
        _redact_record_fields(record)
    except Exception:
        _withhold_record(record)
    return record


def _redact_record_fields(record: logging.LogRecord) -> None:
    _redact_message(record)
    if record.exc_info:
        record.exc_text = _TRACEBACK_FORMATTER.formatException(record.exc_info)
    record.exc_info = None
    if record.exc_text:
        record.exc_text = redact_secret_fields(record.exc_text)
    if record.stack_info:
        record.stack_info = redact_secret_fields(record.stack_info)
    extras = {
        key: value
        for key, value in record.__dict__.items()
        if key not in _STANDARD_RECORD_ATTRS and key != OTEL_CONTEXT_RECORD_ATTR
    }
    record.__dict__.update(redact_secret_fields(extras))


class RedactingLogFilter(logging.Filter):
    """Redacts each record (``redact_log_record``) before its handler
    formats or exports it. Attached to every server log handler where it is
    installed (``async_logging``)."""

    def filter(self, record: logging.LogRecord) -> bool:
        redact_log_record(record)
        return True


REDACTING_LOG_FILTER = RedactingLogFilter()
