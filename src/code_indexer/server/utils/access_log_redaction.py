"""Keep sensitive query-parameter values out of the HTTP access log.

Invariant: no line written by the HTTP server's access logger contains, in
clear, the value of a sensitive query parameter:

  - any parameter whose name the shared ``is_secret_field`` rule classifies
    as secret (``confirmation_token``, ``password``, ``access_token``, ...);
  - ``source`` -- the repository discovery URL, which may carry
    credentials.

A parameter is recognised by its DECODED name, so a percent-encoded name
(``%63onfirmation_token``, ``pass%77ord``, ``sour%63e``) is redacted too.
Only the value is replaced; the rest of the line is kept as logged.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import unquote

from code_indexer.utils.credential_redaction import is_secret_field

ACCESS_LOGGER_NAME = "uvicorn.access"
REDACTED = "[REDACTED]"

# Not a secret name, but its value is a URL that may carry userinfo.
_URL_PARAM = "source"

# A query parameter: its name (possibly percent-encoded) and its value. The
# server splits a query only on '&', and a request target ends at
# whitespace, so the value runs to the next '&' or whitespace -- quotes,
# ';' and ',' are part of it.
_QUERY_PARAM_RE = re.compile(
    r"(?P<prefix>(?:^|[?&])(?P<name>[\w.%~+\-]+)=)(?P<value>[^&\s]*)"
)


def _is_sensitive_param(encoded_name: str) -> bool:
    name = unquote(encoded_name)
    return name.lower() == _URL_PARAM or is_secret_field(name)


def _mask_param(match: "re.Match[str]") -> str:
    if not _is_sensitive_param(match.group("name")):
        return match.group(0)
    return match.group("prefix") + REDACTED


def redact_sensitive_query_values(text: str) -> str:
    """Replace the value of every sensitive query parameter in `text`."""
    return _QUERY_PARAM_RE.sub(_mask_param, text)


class SensitiveQueryRedactionFilter(logging.Filter):
    """Redacts sensitive query values in a record's message and arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_sensitive_query_values(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_sensitive_query_values(arg) if isinstance(arg, str) else arg
                for arg in record.args
            )
        return True


def install_access_log_redaction() -> None:
    """Attach one redaction filter to the access logger (idempotent)."""
    access_logger = logging.getLogger(ACCESS_LOGGER_NAME)
    if not any(
        isinstance(existing, SensitiveQueryRedactionFilter)
        for existing in access_logger.filters
    ):
        access_logger.addFilter(SensitiveQueryRedactionFilter())
