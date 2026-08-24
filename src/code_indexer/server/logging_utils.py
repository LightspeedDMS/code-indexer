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
import re
from typing import Any, Dict

# Matches the userinfo (``user:pass@`` / ``oauth2:TOKEN@`` / ``TOKEN@``) between a
# URL scheme separator and the host, so embedded credentials can be stripped
# before a repo URL is serialized into an API response or log line.
_URL_CREDENTIALS_RE = re.compile(r"(://)[^/@\s]+@")


def mask_url_credentials(url: Any) -> Any:
    """Strip embedded credentials from a git/HTTP URL for safe exposure.

    ``https://oauth2:glpat-XXXX@gitlab.com/org/repo.git`` ->
    ``https://***@gitlab.com/org/repo.git``. Non-string input, credential-free
    URLs, and scheme-only forms (e.g. ``local://alias``) are returned unchanged.
    Idempotent: masking an already-masked URL is a no-op.
    """
    if not isinstance(url, str):
        return url
    return _URL_CREDENTIALS_RE.sub(r"\1***@", url)


# Sensitive field names that should be redacted in logs
SENSITIVE_FIELDS = {
    "password",
    "token",
    "api_key",
    "secret",
    "access_token",
    "refresh_token",
    "authorization",
    "auth_token",
    "private_key",
    "client_secret",
}


def format_error_log(error_code: str, message: str, **context) -> str:
    """
    Format an error log message with error code and optional context.

    Args:
        error_code: Error code in format {SUBSYSTEM}-{CATEGORY}-{NUMBER}
        message: Human-readable error message
        **context: Additional context key-value pairs to include

    Returns:
        Formatted log message: "[{ERROR_CODE}] message key1=value1 key2=value2"

    Examples:
        >>> format_error_log("AUTH-OIDC-001", "Connection failed", issuer="https://example.com")
        '[AUTH-OIDC-001] Connection failed issuer=https://example.com'

        >>> format_error_log("MCP-TOOL-042", "Tool execution failed")
        '[MCP-TOOL-042] Tool execution failed'
    """
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


def sanitize_for_logging(data: Any) -> Any:
    """
    Sanitize data for logging by redacting sensitive information.

    Args:
        data: Data to sanitize (dict, string, or other type)

    Returns:
        Sanitized copy of data with sensitive fields redacted

    Examples:
        >>> sanitize_for_logging({"username": "admin", "password": "secret"})
        {'username': 'admin', 'password': '***REDACTED***'}

        >>> sanitize_for_logging("plain string")
        'plain string'
    """
    if data is None:
        return None

    if not isinstance(data, dict):
        # Non-dict types are returned as-is
        return data

    # Create sanitized copy of dictionary
    sanitized = {}
    for key, value in data.items():
        if key.lower() in SENSITIVE_FIELDS:
            sanitized[key] = "***REDACTED***"
        else:
            sanitized[key] = value

    return sanitized
