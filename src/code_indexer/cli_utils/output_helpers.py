"""Output helpers for CIDX CLI commands.

Provides JSON output formatting and decorator patterns for consistent
CLI output across remote commands.
"""

import functools
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def format_json_success(data: Any, metadata: Optional[Dict[str, Any]] = None) -> str:
    """Format successful result as JSON with standard structure.

    Args:
        data: The data to include in the response
        metadata: Optional additional metadata

    Returns:
        JSON string with format: {"success": true, "data": ..., "metadata": {...}}
    """
    result_metadata = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if metadata:
        result_metadata.update(metadata)

    result = {
        "success": True,
        "data": data,
        "metadata": result_metadata,
    }
    return json.dumps(result, indent=2, default=str)


def format_json_error(error_message: str, error_type: Optional[str] = None) -> str:
    """Format error result as JSON with standard structure.

    Args:
        error_message: The error message
        error_type: Optional error type/class name

    Returns:
        JSON string with format: {"success": false, "error": ..., "error_type": ...}
    """
    result = {
        "success": False,
        "error": error_message,
        "error_type": error_type or "Error",
    }
    return json.dumps(result, indent=2)


def json_output_handler(func):
    """Decorator that handles JSON output formatting for CLI commands.

    When the decorated function has json_output=True, the result is wrapped
    in the standard JSON success/error structure. When json_output=False,
    the result passes through unchanged.

    Usage:
        @json_output_handler
        def my_command(json_output: bool = False):
            return {"key": "value"}

    The decorated function should:
    - Accept a json_output parameter (bool)
    - Return a dict/data structure on success
    - Raise exceptions on error

    When json_output=True:
    - Success: Returns JSON string with {"success": true, "data": ...}
    - Error: Returns JSON string with {"success": false, "error": ...}

    When json_output=False:
    - Returns the function result directly
    - Exceptions propagate normally
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        json_output = kwargs.get("json_output", False)

        if not json_output:
            # Pass through mode - let exceptions propagate
            return func(*args, **kwargs)

        # JSON output mode - catch exceptions and format
        try:
            result = func(*args, **kwargs)
            return format_json_success(result)
        except Exception as e:
            error_type = type(e).__name__
            return format_json_error(str(e), error_type)

    return wrapper
