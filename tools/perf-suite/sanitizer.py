"""
Information sanitization for the CIDX performance report.

Story #335: Performance Report with Hardware Profile
AC6: Server URLs/IPs replaced, no passwords or tokens in report.

Provides:
- sanitize_report_content(): Replace IPs and sensitive literals
- sanitize_reproduction_command(): Scrub CLI command for public sharing
- sanitize_url_in_content(): Replace HTTP URLs containing IPs
- post_generation_scan(): Detect remaining sensitive patterns and warn
"""

from __future__ import annotations

import re
from typing import List

# IPv4 pattern: matches octets like 192.168.1.100
_IP_PATTERN = re.compile(
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b"
)

# HTTP/HTTPS URL containing an IP address (not a placeholder)
_URL_WITH_IP_PATTERN = re.compile(
    r"https?://(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?:/\S*)?"
)

# Bearer token: "Bearer " followed by non-whitespace chars
_BEARER_PATTERN = re.compile(
    r"Bearer\s+\S+",
    re.IGNORECASE,
)

# Password argument in CLI: --password=VALUE or --password VALUE
_PASSWORD_ARG_PATTERN = re.compile(
    r"(--password[=\s]+)\S+",
    re.IGNORECASE,
)

# Token argument in CLI: --token=VALUE or --token VALUE
_TOKEN_ARG_PATTERN = re.compile(
    r"(--token[=\s]+)\S+",
    re.IGNORECASE,
)

# Username argument in CLI: --username=VALUE or --username VALUE
_USERNAME_ARG_PATTERN = re.compile(
    r"(--username[=\s]+)\S+",
    re.IGNORECASE,
)

# Server URL argument in CLI: --server-url=VALUE or --server-url VALUE
_SERVER_URL_ARG_PATTERN = re.compile(
    r"(--server-url[=\s]+)\S+",
    re.IGNORECASE,
)

# Patterns that indicate sensitive data remaining in the report
_SCAN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("IP address detected", _IP_PATTERN),
    ("Bearer token detected", _BEARER_PATTERN),
    ("Password keyword detected", re.compile(r"\bpassword\s*[=:]\s*\S+", re.IGNORECASE)),
]


def sanitize_report_content(content: str) -> str:
    """
    Replace IP addresses and sensitive literals in report content.

    Replaces:
    - IPv4 addresses → <staging-server>
    - Bearer tokens → Bearer <token>

    Args:
        content: Raw report content string.

    Returns:
        Sanitized content string.
    """
    content = _IP_PATTERN.sub("<staging-server>", content)
    content = _BEARER_PATTERN.sub("Bearer <token>", content)
    content = _PASSWORD_ARG_PATTERN.sub(r"\g<1><password>", content)
    content = _TOKEN_ARG_PATTERN.sub(r"\g<1><token>", content)
    return content


def sanitize_url_in_content(content: str) -> str:
    """
    Replace HTTP/HTTPS URLs containing IP addresses with sanitized placeholders.

    Replaces the entire URL (scheme + IP + port + path) with
    http://<staging-server>/...

    Args:
        content: Raw content string.

    Returns:
        Content with IP-based URLs replaced.
    """
    def _replace_url(match: re.Match) -> str:  # type: ignore[type-arg]
        original = match.group(0)
        # Preserve the path portion if present
        url_pattern = re.compile(r"(https?://)(?:\d{1,3}\.){3}\d{1,3}(:\d+)?(.*)")
        m = url_pattern.match(original)
        if m:
            scheme = m.group(1)
            port = m.group(2) or ""
            path = m.group(3) or ""
            return f"{scheme}<staging-server>{port}{path}"
        return "<staging-server>"

    return _URL_WITH_IP_PATTERN.sub(_replace_url, content)


def sanitize_reproduction_command(cmd: str) -> str:
    """
    Sanitize a CLI reproduction command for safe inclusion in public reports.

    Replaces:
    - --password VALUE → --password <password>
    - --token VALUE → --token <token>
    - --server-url http://IP:PORT → --server-url http://<staging-server>:PORT
    - Bare IPv4 addresses → <staging-server>

    Args:
        cmd: CLI command string (e.g., "python run_perf_suite.py --password secret ...").

    Returns:
        Sanitized command string.
    """
    cmd = _PASSWORD_ARG_PATTERN.sub(r"\g<1><password>", cmd)
    cmd = _TOKEN_ARG_PATTERN.sub(r"\g<1><token>", cmd)
    cmd = _USERNAME_ARG_PATTERN.sub(r"\g<1><admin-user>", cmd)
    cmd = sanitize_url_in_content(cmd)
    # Catch any remaining bare IPs
    cmd = _IP_PATTERN.sub("<staging-server>", cmd)
    return cmd


def post_generation_scan(content: str) -> List[str]:
    """
    Scan generated report content for remaining sensitive patterns.

    This is a safety net applied after report generation. If patterns are found,
    warnings are returned. The caller decides whether to write the report
    or abort (per AC6: warn but still write).

    Args:
        content: Final report content to scan.

    Returns:
        List of warning strings (empty list = clean).
    """
    warnings: List[str] = []
    for description, pattern in _SCAN_PATTERNS:
        matches = pattern.findall(content)
        if matches:
            warnings.append(f"WARNING: {description} ({len(matches)} occurrence(s))")
    return warnings
