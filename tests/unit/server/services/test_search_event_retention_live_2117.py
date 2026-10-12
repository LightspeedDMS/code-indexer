"""Story #2117: the Query Analytics Export page states the LIVE retention.

A SQLite worker never reloads another worker's save, and a cluster node only
polls; the page therefore reads the COMMITTED runtime configuration (one
database read), never the process's cached copy. When that read fails the
page says the retention is unavailable (logged at WARNING) -- never a stale
or made-up number.

Two real ConfigService instances over ONE committed store stand for two
processes; real SQLite and real PostgreSQL, no mocks.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator, Tuple
from unittest.mock import patch

import pytest

from code_indexer.server.services import audit_capture
from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.services.search_event_health import search_event_log_window
from code_indexer.server.services.siem_delivery import capture
from tests.unit.server.siem.backends import (  # noqa: F401  (fixtures)
    SiemBackendHarness,
    _pg_session_pool,
    pg_pool,
    siem_backend,
)

_GET_CONFIG_SERVICE = "code_indexer.server.services.config_service.get_config_service"


def _attach(service: ConfigService, b: SiemBackendHarness, db_path: Path) -> None:
    service.load_config()
    if b.name == "postgres":
        service.set_connection_pool(b.pool)
    else:
        service.initialize_runtime_db(str(db_path))


@pytest.fixture()
def two_processes(
    siem_backend: SiemBackendHarness,  # noqa: F811  (the imported fixture)
    tmp_path: Path,
) -> Iterator[Tuple[ConfigService, ConfigService]]:
    """Two services over ONE committed store (two SQLite workers on one node,
    or two cluster nodes on PostgreSQL)."""
    from code_indexer.server.storage.database_manager import DatabaseSchema

    dir_a, dir_b = tmp_path / "node-a", tmp_path / "node-b"
    dir_a.mkdir()
    dir_b.mkdir()
    db_path = dir_a / "cidx_server.db"
    if siem_backend.name == "sqlite":
        DatabaseSchema(str(db_path)).initialize_database()
        dir_b = dir_a
    a = ConfigService(server_dir_path=str(dir_a))
    _attach(a, siem_backend, db_path)
    b = ConfigService(server_dir_path=str(dir_b))
    _attach(b, siem_backend, db_path)
    capture.reset_capture_state_for_tests()
    audit_capture.mark_server_process()
    audit_capture.bind_audit_service(siem_backend.audit, node_id=None)
    try:
        yield a, b
    finally:
        audit_capture.clear_audit_service()
        audit_capture.reset_server_process_mark()
        capture.reset_capture_state_for_tests()


def test_page_window_reads_the_committed_retention(
    two_processes: Tuple[ConfigService, ConfigService],
) -> None:
    a, b = two_processes
    cached = b.get_config().search_event_log_retention_days
    a.update_settings_atomic(
        [("search_event_log", "search_event_log_retention_days", cached + 7)]
    )
    # B's cached copy predates A's save.
    assert b.get_config().search_event_log_retention_days == cached

    with patch(_GET_CONFIG_SERVICE, return_value=b):
        window = search_event_log_window(None)

    assert window["retention_days"] == cached + 7
    assert window["retention_unavailable"] is False


def test_unreadable_retention_is_unavailable_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No committed runtime store attached: the read fails."""
    service = ConfigService(server_dir_path=str(tmp_path))
    service.load_config()

    with patch(_GET_CONFIG_SERVICE, return_value=service):
        with caplog.at_level(logging.WARNING):
            window = search_event_log_window(None)

    assert window["retention_days"] is None
    assert window["retention_unavailable"] is True
    assert any(
        r.levelno == logging.WARNING and "retention" in r.getMessage()
        for r in caplog.records
    )
