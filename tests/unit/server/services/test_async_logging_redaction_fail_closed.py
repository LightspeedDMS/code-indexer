"""Invariant: the redacting log filter never raises into the logging caller.

It is attached to handlers outside the queue (for example the HTTP server's
own console handlers), so any failure while redacting a record must be
contained: the line is still emitted, but only as a fixed marker -- the raw
message, its arguments and any traceback are withheld (fail closed).
"""

import io
import logging
import uuid
from typing import Iterator, Tuple

import pytest

from code_indexer.server.logging_utils import REDACTING_LOG_FILTER

WITHHELD = "[log message withheld: redaction failed]"
SECRET = "Fz8failClosedSecret31"


@pytest.fixture
def filtered_logger() -> Iterator[Tuple[logging.Logger, io.StringIO]]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.addFilter(REDACTING_LOG_FILTER)
    logger = logging.getLogger(f"test.redaction_fail_closed.{uuid.uuid4().hex}")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    yield logger, stream
    logger.removeHandler(handler)
    handler.close()


class _UnprintableArg:
    def __str__(self) -> str:
        raise RuntimeError(f"cannot render token={SECRET}")

    __repr__ = __str__


def test_malformed_format_does_not_raise_and_emits_only_marker(
    filtered_logger: Tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = filtered_logger
    try:
        raise ValueError(f"inner token={SECRET}")
    except ValueError:
        logger.exception("%s %s", f"only-one token={SECRET}")

    output = stream.getvalue()
    assert output == f"ERROR {WITHHELD}\n"
    assert SECRET not in output
    assert "Traceback" not in output


def test_argument_whose_str_raises_does_not_raise_and_emits_only_marker(
    filtered_logger: Tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = filtered_logger
    logger.warning("value %s", _UnprintableArg(), stack_info=True)

    output = stream.getvalue()
    assert output == f"WARNING {WITHHELD}\n"
    assert SECRET not in output
    assert "Stack (most recent call last)" not in output


def test_failed_record_is_scrubbed_of_raw_content() -> None:
    record = logging.LogRecord(
        "x", logging.ERROR, __file__, 1, "%s %s", (f"token={SECRET}",), None
    )
    record.exc_text = f"Traceback token={SECRET}"
    record.stack_info = f"Stack token={SECRET}"
    record.__dict__["color_message"] = f"%s %s token={SECRET}"

    assert REDACTING_LOG_FILTER.filter(record) is True

    assert record.msg == WITHHELD
    assert record.args == ()
    assert record.exc_info is None
    assert record.exc_text is None
    assert record.stack_info is None
    assert "color_message" not in record.__dict__
    assert record.getMessage() == WITHHELD


MALFORMED_TEMPLATE = "%s %s"  # two placeholders, one argument: redaction fails


def test_failed_record_masks_extra_fields_for_formatters() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s %(payload)s"))
    handler.addFilter(REDACTING_LOG_FILTER)
    logger = logging.getLogger(f"test.redaction_fail_closed.{uuid.uuid4().hex}")
    logger.propagate = False
    logger.addHandler(handler)
    try:
        logger.warning(
            MALFORMED_TEMPLATE, "only-one", extra={"payload": f"token={SECRET}"}
        )
    finally:
        logger.removeHandler(handler)
        handler.close()

    output = stream.getvalue()
    assert output == f"WARNING {WITHHELD} {WITHHELD}\n"
    assert SECRET not in output


def test_failed_record_keeps_extra_attribute_names_and_otel_context() -> None:
    from code_indexer.server.logging_utils import OTEL_CONTEXT_RECORD_ATTR

    context = object()
    record = logging.LogRecord(
        "x", logging.ERROR, __file__, 1, MALFORMED_TEMPLATE, ("only-one",), None
    )
    record.__dict__["payload"] = {"nested": f"token={SECRET}"}
    record.__dict__[OTEL_CONTEXT_RECORD_ATTR] = context

    assert REDACTING_LOG_FILTER.filter(record) is True

    assert record.__dict__["payload"] == WITHHELD
    assert record.__dict__[OTEL_CONTEXT_RECORD_ATTR] is context


def test_secret_bearing_record_is_still_masked(
    filtered_logger: Tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = filtered_logger
    logger.info("upstream rejected %s", f"token={SECRET}")

    output = stream.getvalue()
    assert output.startswith("INFO upstream rejected token=")
    assert SECRET not in output
    assert WITHHELD not in output
