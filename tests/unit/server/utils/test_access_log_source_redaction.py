"""The HTTP access log never holds a repository discovery ``source`` value:
it may carry a percent-encoded repository URL with credentials."""

import logging
from urllib.parse import quote

from code_indexer.server.utils.access_log_redaction import (
    ACCESS_LOGGER_NAME,
    REDACTED,
    SensitiveQueryRedactionFilter,
    redact_sensitive_query_values,
)

SECRET = "s3cr3t-value"
ENCODED_URL = quote(
    f"https://example-user:{SECRET}@git.example.com/example/repo.git", safe=""
)
REQUEST = f"/api/repos/discover?source={ENCODED_URL}&limit=5"


def test_source_query_value_is_redacted() -> None:
    redacted = redact_sensitive_query_values(f"GET {REQUEST} HTTP/1.1")

    assert SECRET not in redacted
    assert f"?source={REDACTED}&limit=5" in redacted


def test_confirmation_token_redaction_still_applies() -> None:
    redacted = redact_sensitive_query_values("/x?confirmation_token=abc&source=y")

    assert redacted == f"/x?confirmation_token={REDACTED}&source={REDACTED}"


def test_access_log_record_args_are_redacted() -> None:
    record = logging.LogRecord(
        ACCESS_LOGGER_NAME,
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", REQUEST, "1.1", 404),
        None,
    )

    SensitiveQueryRedactionFilter().filter(record)

    assert SECRET not in record.getMessage()
