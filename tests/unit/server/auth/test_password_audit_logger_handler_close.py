"""Flat-file reconfiguration never leaks an open FileHandler.

Invariant: whenever the password audit logger replaces a handler on its
(name-cached, per-path) logger, the replaced handler is closed, so repeated
unbinds and repeated configurations of one path hold exactly one open file.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List

from code_indexer.server.auth.audit_logger import PasswordChangeAuditLogger
from code_indexer.server.services.audit_log_service import AuditLogService


def _file_handlers(logger: logging.Logger) -> List[logging.FileHandler]:
    return [h for h in logger.handlers if isinstance(h, logging.FileHandler)]


def _is_open(handler: logging.FileHandler) -> bool:
    return handler.stream is not None


def _write_one_record(audit_logger: PasswordChangeAuditLogger) -> None:
    """The handler opens its file on the first record (Bug #1996)."""
    audit_logger.audit_logger.info("probe")


def test_configuring_a_path_closes_the_handler_it_replaces(tmp_path: Path) -> None:
    path = str(tmp_path / "password_audit.log")
    first = PasswordChangeAuditLogger(log_file_path=path)
    _write_one_record(first)
    replaced = _file_handlers(first.audit_logger)
    assert len(replaced) == 1 and _is_open(replaced[0])

    second = PasswordChangeAuditLogger(log_file_path=path)
    _write_one_record(second)

    assert not _is_open(replaced[0])
    live = _file_handlers(second.audit_logger)
    assert len(live) == 1 and _is_open(live[0])
    live[0].close()


def test_repeated_unbind_leaves_exactly_one_open_handler(tmp_path: Path) -> None:
    path = str(tmp_path / "password_audit.log")
    audit_logger = PasswordChangeAuditLogger(log_file_path=path)
    _write_one_record(audit_logger)
    seen: List[logging.FileHandler] = list(_file_handlers(audit_logger.audit_logger))
    audit_logger.set_audit_service(AuditLogService(tmp_path / "groups.db"))

    for _ in range(3):
        audit_logger.set_audit_service(None)
        _write_one_record(audit_logger)
        seen.extend(_file_handlers(audit_logger.audit_logger))

    open_handlers = {id(h) for h in seen if _is_open(h)}
    assert len(open_handlers) == 1
    assert audit_logger.log_file_path == path
    for handler in _file_handlers(audit_logger.audit_logger):
        handler.close()
