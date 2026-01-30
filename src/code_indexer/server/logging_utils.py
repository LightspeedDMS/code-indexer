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

from typing import Any, Dict
from code_indexer.server.middleware.correlation import get_correlation_id


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
    extra: Dict[str, Any] = {"error_code": error_code}

    # Add correlation_id if available
    correlation_id = get_correlation_id()
    if correlation_id:
        extra["correlation_id"] = correlation_id

    return extra


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
