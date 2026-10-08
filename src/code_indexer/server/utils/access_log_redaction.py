"""Keep sensitive query-parameter values out of the HTTP access log.

Invariant: no line written by the HTTP server's access logger contains, in
clear, the value of a sensitive query parameter:

  - any parameter whose name the shared ``is_secret_field`` rule classifies
    as secret (``confirmation_token``, ``password``, ``access_token``, ...);
  - ``source`` -- the repository discovery URL, which may carry
    credentials.

A parameter is recognised by its DECODED name, so a percent-encoded name
(``%63onfirmation_token``, ``pass%77ord``, ``sour%63e``) is redacted too.
Only the value is replaced; the rest of the line is kept as logged. The
decode-and-classify rule is the shared ``mask_secret_query_values``.
"""

from __future__ import annotations

import logging

from code_indexer.utils.credential_redaction import mask_secret_query_values

ACCESS_LOGGER_NAME = "uvicorn.access"
REDACTED = "[REDACTED]"

# Not a secret name, but its value is a URL that may carry userinfo.
_URL_PARAMS = frozenset({"source"})


def redact_sensitive_query_values(text: str) -> str:
    """Replace the value of every sensitive query parameter in `text`."""
    return mask_secret_query_values(text, REDACTED, also_names=_URL_PARAMS)


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
