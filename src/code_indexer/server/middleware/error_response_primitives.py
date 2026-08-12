"""
Error response formatting primitives for CIDX Server.

Timestamp/correlation-id helpers, JSON-serialization normalization, and
Pydantic-message humanization used by error_formatters.py's response
builders. Extracted from error_formatters.py to keep that module under
CLAUDE.md Foundation #6 (Bug #1568).

Bug #1468 constraint: this module MUST NOT import fastapi/starlette at
module level, directly or transitively -- middleware/correlation.py reaches
generate_correlation_id() via error_formatters.py on a hot import path, and
importing FilesystemVectorStore (the CLI/solo storage class) must not
eagerly pull in fastapi through that chain.
"""

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


# CLAUDE.md Foundation #8 Pattern #7: Named constants for security fix
MINIMUM_RETRY_SECONDS = 5
MAXIMUM_RETRY_SECONDS = 60
RETRY_MULTIPLIER = 10


def generate_correlation_id() -> str:
    """Generate unique correlation ID for error tracking."""
    return str(uuid.uuid4())


def get_current_timestamp() -> datetime:
    """Get current timestamp in UTC."""
    return datetime.now(timezone.utc)


def format_timestamp(timestamp: datetime) -> str:
    """Format timestamp in ISO 8601 format."""
    return timestamp.isoformat().replace("+00:00", "Z")


def _serialize_value_for_json(value: Any) -> Any:
    """
    Serialize value to ensure JSON compatibility.

    Handles datetime, Path, bytes, callable objects, nested dicts, and lists recursively.

    Args:
        value: Value to serialize

    Returns:
        JSON-serializable value
    """
    if isinstance(value, datetime):
        return value.isoformat()
    elif isinstance(value, Path):
        return str(value)
    elif isinstance(value, bytes):
        return f"<bytes:{len(value)} bytes>"
    elif callable(value):
        return f"<function:{getattr(value, '__name__', 'unknown')}>"
    elif isinstance(value, dict):
        return {k: _serialize_value_for_json(v) for k, v in value.items()}
    elif isinstance(value, (list, tuple)):
        return [_serialize_value_for_json(item) for item in value]
    else:
        return value


def humanize_validation_message(pydantic_error: Dict[str, Any]) -> str:
    """Convert Pydantic error messages to human-readable format."""
    error_type = pydantic_error["type"]
    message = pydantic_error.get("msg", "")

    # Map common error types to friendly messages
    friendly_messages = {
        "missing": "This field is required",
        "string_too_short": "This field is too short",
        "string_too_long": "This field is too long",
        "string_pattern_mismatch": "This field has an invalid format",
        "value_error.missing": "This field is required",
        "type_error.integer": "This field must be a number",
        "type_error.str": "This field must be text",
        "type_error.bool": "This field must be true or false",
        "value_error.number.not_gt": "This field must be greater than the minimum value",
        "value_error.number.not_lt": "This field must be less than the maximum value",
        "value_error.email": "This field must be a valid email address",
    }

    return friendly_messages.get(error_type, message or "Invalid value")
