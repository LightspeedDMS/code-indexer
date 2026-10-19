"""Startup hands the group store to the MCP self-registration service and
runs the analysis agents' credential migration.

The service provisions the analysis agents' least-privilege principal (and
its powerusers-group membership) when it mints their MCP credential; without
the group store it refuses to mint, leaving the agents without MCP access.
"""

import inspect
from typing import Iterator

import pytest

from code_indexer.server.services.mcp_self_registration_service import (
    MCPSelfRegistrationService,
)
from code_indexer.server.startup import lifespan


@pytest.fixture(autouse=True)
def _clean_singleton() -> Iterator[None]:
    yield
    MCPSelfRegistrationService.set_instance(None)


def test_helper_wires_the_group_store_into_the_singleton() -> None:
    service = MCPSelfRegistrationService(
        config_manager=None, mcp_credential_manager=None
    )
    MCPSelfRegistrationService.set_instance(service)
    group_store = object()
    lifespan._wire_group_manager_into_mcp_self_registration(group_store)
    assert service._group_manager is group_store


def test_helper_without_a_singleton_is_a_noop() -> None:
    MCPSelfRegistrationService.set_instance(None)
    lifespan._wire_group_manager_into_mcp_self_registration(object())


def test_lifespan_calls_the_helper() -> None:
    source = inspect.getsource(lifespan)
    assert "_wire_group_manager_into_mcp_self_registration(group_manager)" in source


def test_startup_migration_replaces_an_admin_bound_credential(tmp_path) -> None:
    import os
    from unittest.mock import patch

    from tests.unit.server.services.test_analysis_agent_principal import (
        _World,
        _owner_of_header,
    )
    from code_indexer.server.services.analysis_agent_principal import (
        ANALYSIS_AGENT_USERNAME,
    )

    world = _World(tmp_path)
    legacy = world.credentials.generate_credential("admin", "cidx-local-auto")
    world.store(legacy["client_id"], legacy["client_secret"])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "claude.argv"
    fake = bin_dir / "claude"
    fake.write_text(f'#!/bin/bash\necho "$*" >> {log}\nexit 0\n')
    fake.chmod(0o755)

    with patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"}):
        assert world.service.migrate_at_startup() is True

    assert (
        world.credentials.verify_credential(
            legacy["client_id"], legacy["client_secret"]
        )
        is None
    )
    calls = log.read_text().splitlines()
    removed = calls.index("mcp remove cidx-local --scope user")
    added = calls[removed + 1]
    header = added.split("Authorization: ", 1)[1].split(" ")[0:2]
    assert _owner_of_header(world, " ".join(header)) == ANALYSIS_AGENT_USERNAME


class _MigrationProbe:
    def __init__(self, fail: bool) -> None:
        self.fail = fail
        self.thread_ids: list = []

    def migrate_at_startup(self) -> bool:
        import threading

        self.thread_ids.append(threading.get_ident())
        if self.fail:
            raise RuntimeError("example failure")
        return True


@pytest.mark.asyncio
async def test_the_startup_migration_runs_off_the_event_loop() -> None:
    import threading

    probe = _MigrationProbe(fail=False)
    MCPSelfRegistrationService.set_instance(probe)  # type: ignore[arg-type]
    await lifespan._migrate_analysis_agent_credential()
    assert probe.thread_ids and probe.thread_ids[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_a_failed_startup_migration_is_logged_and_not_fatal(caplog) -> None:
    MCPSelfRegistrationService.set_instance(_MigrationProbe(fail=True))  # type: ignore[arg-type]
    await lifespan._migrate_analysis_agent_credential()
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_lifespan_starts_the_migration_after_audit_is_bound() -> None:
    source = inspect.getsource(lifespan)
    audit_bound = source.index("group_manager.set_audit_service(audit_service)")
    start = source.index(
        "_start_analysis_agent_credential_migration(app, group_manager)"
    )
    assert start > audit_bound
    assert "await _migrate_analysis_agent_credential()" not in source
    running = source.index("yield  # Server is now running")
    assert source.index("await _stop_analysis_agent_credential_migration(app)") > (
        running
    )


def test_the_migration_writes_every_audit_row_once_audit_is_bound(tmp_path) -> None:
    import sqlite3
    from unittest.mock import patch

    from code_indexer.server.services import audit_capture
    from code_indexer.server.services.audit_log_service import AuditLogService
    from tests.unit.server.services.test_analysis_agent_principal import _World

    world = _World(tmp_path)
    legacy = world.credentials.generate_credential("admin", "cidx-local-auto")
    world.store(legacy["client_id"], legacy["client_secret"])
    audit = AuditLogService(tmp_path / "groups.db")
    audit.start()
    audit_capture.mark_server_process()
    audit_capture.bind_audit_service(audit, node_id=None)
    try:
        before = audit_capture.records_dropped_since_boot()
        with patch.object(world.service, "claude_cli_available", return_value=False):
            assert world.service.migrate_at_startup() is True
        audit.flush()
        assert audit_capture.records_dropped_since_boot() == before
        with sqlite3.connect(tmp_path / "groups.db") as conn:
            actions = {
                row[0] for row in conn.execute("SELECT action_type FROM audit_logs")
            }
        assert {
            "mcp_credential_revoked",
            "user_created",
            "mcp_credential_created",
        } <= actions, actions
    finally:
        audit_capture.clear_audit_service()
        audit_capture.reset_server_process_mark()
        audit.stop()


@pytest.mark.asyncio
async def test_the_startup_migration_never_delays_startup_and_stops_at_shutdown() -> (
    None
):
    import asyncio
    import threading
    import types

    release = threading.Event()

    class _Blocking:
        def set_group_manager(self, group_manager: object) -> None:
            self.group_manager = group_manager

        def migrate_at_startup(self) -> bool:
            release.wait(30)
            return True

    MCPSelfRegistrationService.set_instance(_Blocking())  # type: ignore[arg-type]
    app = types.SimpleNamespace(state=types.SimpleNamespace())
    try:
        lifespan._start_analysis_agent_credential_migration(app, object())
        task = app.state.analysis_agent_credential_migration_task
        await asyncio.sleep(0.05)
        assert not task.done()
        await asyncio.wait_for(
            lifespan._stop_analysis_agent_credential_migration(app), timeout=5
        )
        assert task.done()
    finally:
        release.set()
