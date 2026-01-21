"""CLI utilities package for CIDX.

Provides reusable patterns for CLI command implementation including:
- JSON output formatting
- Remote command base utilities
"""

from .output_helpers import (
    format_json_success,
    format_json_error,
    json_output_handler,
)

__all__ = [
    "format_json_success",
    "format_json_error",
    "json_output_handler",
]
