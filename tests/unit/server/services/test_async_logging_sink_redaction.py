"""Every server log sink behind the async logging queue receives records
with credential values masked: the message, its args, the formatted
exception and stack, and extra attributes.

Real SQLiteLogHandler (real logs.db) and a real console StreamHandler,
installed exactly as the server installs them (install_queue_logging).
The secret is a neutral placeholder.
"""

from __future__ import annotations

import io
import logging
import sqlite3
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest

from code_indexer.server.services.async_logging import (
    install_queue_logging,
    register_additional_listener_handler,
    shutdown_queue_logging,
)
from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

SECRET = "FAKEsecret4242"


@pytest.fixture
def sinks(
    tmp_path: Path,
) -> Iterator[Tuple[logging.Logger, SQLiteLogHandler, io.StringIO, Path]]:
    target = logging.getLogger("test.sink_redaction")
    target.propagate = False
    target.setLevel(logging.DEBUG)
    db_path = tmp_path / "logs.db"
    sqlite_handler = SQLiteLogHandler(db_path)
    console_text = io.StringIO()
    console = logging.StreamHandler(console_text)
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    install_queue_logging([sqlite_handler, console], root=target)
    try:
        yield target, sqlite_handler, console_text, db_path
    finally:
        shutdown_queue_logging()
        sqlite_handler.close()
        for handler in list(target.handlers):
            target.removeHandler(handler)


def _stored_rows(db_path: Path) -> list:
    with sqlite3.connect(str(db_path)) as conn:
        return conn.execute("SELECT message, extra_data FROM logs").fetchall()


def test_logger_exception_reaches_no_sink_with_its_secret(
    sinks: Tuple[logging.Logger, SQLiteLogHandler, io.StringIO, Path],
) -> None:
    target, sqlite_handler, console_text, db_path = sinks
    try:
        raise RuntimeError(f"remote refused token={SECRET}")
    except RuntimeError:
        target.exception(
            "push failed token=%s",
            SECRET,
            stack_info=True,
            extra={"api_token": SECRET},
        )
    shutdown_queue_logging()
    sqlite_handler.flush()

    rows = _stored_rows(db_path)
    assert rows, "expected the record in the logs table"
    for message, extra_data in rows:
        assert SECRET not in message
        assert SECRET not in (extra_data or "")
        assert "RuntimeError" in message, "the traceback must still be stored"
    console = console_text.getvalue()
    assert "RuntimeError" in console, "the traceback must still be printed"
    assert SECRET not in console


class _Capturing(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_additional_listener_handler_receives_redacted_records(
    sinks: Tuple[logging.Logger, SQLiteLogHandler, io.StringIO, Path],
) -> None:
    target = sinks[0]
    added = _Capturing()
    assert register_additional_listener_handler(added) is True
    try:
        raise RuntimeError(f"remote refused password={SECRET}")
    except RuntimeError:
        target.exception(
            "failed %s", f"token={SECRET}", stack_info=True, extra={"pwd": SECRET}
        )
    shutdown_queue_logging()

    assert added.records
    for record in added.records:
        text = " ".join(
            str(part)
            for part in (
                record.getMessage(),
                record.exc_text,
                record.stack_info,
                record.__dict__.get("pwd"),
            )
        )
        assert SECRET not in text
        assert record.exc_info is None
        assert "RuntimeError" in str(record.exc_text)
