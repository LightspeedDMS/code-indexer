"""Bug #2017 (review round 1): EVERY writer of the committed runtime row
applies its change to the latest committed row, never to a stale copy.

Each test lets another process commit X between the writer's read and its
write (the interleaving is injected by running the other process's real
save at that point), then asserts X survived.  Real SQLite and real
PostgreSQL; no mocks of the code under test.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.utils.config_manager import ServerConfig
from tests.unit.server.services.test_config_service_lost_update_2017 import (
    SIEM,
    _attach,
    two_processes,
)
from tests.unit.server.siem.backends import (  # noqa: F401  (fixtures)
    SiemBackendHarness,
    _pg_session_pool,
    pg_pool,
    siem_backend,
)

_ = two_processes  # the shared two-process fixture
DESTINATION = [
    ("siem_delivery", "region", "us"),
    ("siem_delivery", "project_id", "example-project"),
    ("siem_delivery", "location", "us"),
    ("siem_delivery", "instance_id", "example-instance"),
    ("siem_delivery", "source_instance_label", "example-label"),
]


def _edit_committed_row(
    service: ConfigService, edit: Callable[[Dict[str, Any]], None]
) -> None:
    """Rewrite the stored row the way an older release left it."""
    _version, runtime = service._read_committed_runtime()
    edit(runtime)
    if service._pool is not None:
        with service._pool.connection() as conn:
            conn.execute(
                "UPDATE server_config SET config_json = %s::jsonb "
                "WHERE config_key = 'runtime'",
                (json.dumps(runtime),),
            )
        return
    assert service._sqlite_db_path is not None
    conn = sqlite3.connect(service._sqlite_db_path)
    try:
        conn.execute(
            "UPDATE server_config SET config_json = ? WHERE config_key = 'runtime'",
            (json.dumps(runtime),),
        )
        conn.commit()
    finally:
        conn.close()


def _pre_upgrade(runtime: Dict[str, Any]) -> None:
    """A row written before the lifecycle section and the alias-lock
    promotion marker existed: both startup backfills fire on it."""
    runtime.pop("lifecycle_analysis_config", None)
    runtime.get("alias_lock_config", {}).pop("db_backed_enabled_promoted", None)


def test_startup_backfills_keep_a_save_committed_meanwhile(
    two_processes: Tuple[ConfigService, ConfigService],
    siem_backend: SiemBackendHarness,  # noqa: F811
    tmp_path: Path,
) -> None:
    """A worker restarting on a pre-upgrade row reads it, another worker
    commits X, then the restarting worker's startup backfills write."""
    a, _b = two_processes
    _edit_committed_row(a, _pre_upgrade)
    restarting = ConfigService(server_dir_path=str(tmp_path / "node-a"))
    original_merge = restarting._merge_runtime_config
    injected: List[bool] = []

    def _merge_after_another_commit(
        runtime_dict: dict, base_config: Optional[ServerConfig] = None
    ) -> None:
        if not injected:  # the other worker commits after this one's read
            injected.append(True)
            a.update_settings_atomic([("siem_delivery", "project_id", "example-x")])
        original_merge(runtime_dict, base_config=base_config)

    restarting._merge_runtime_config = _merge_after_another_commit  # type: ignore[method-assign]
    _attach(restarting, siem_backend, tmp_path / "node-a" / "cidx_server.db")

    assert injected, "the interleaving was not exercised"
    _v, siem = a.read_committed_section(SIEM)
    assert siem["project_id"] == "example-x", "a startup backfill reverted a save"
    _v, lifecycle = a.read_committed_section("lifecycle_analysis_config")
    assert lifecycle, "the lifecycle backfill must still be persisted"


class _CredentialManager:
    """The MCP credential store's two calls used here; *during* runs while
    the new credential is generated (the window between read and write)."""

    def __init__(self, during: Callable[[], None]) -> None:
        self._during = during

    def get_credential_by_client_id(self, client_id: str) -> Any:
        return None

    def generate_credential_audited(
        self, user: str, name: str, *, actor: Any
    ) -> Dict[str, str]:
        self._during()
        return {"client_id": "example-client", "client_secret": "example-secret"}


def test_mcp_self_registration_keeps_a_siem_disable_committed_meanwhile(
    two_processes: Tuple[ConfigService, ConfigService],
) -> None:
    from code_indexer.server.services.mcp_self_registration_service import (
        MCPSelfRegistrationService,
    )

    a, b = two_processes
    a.update_settings_atomic(DESTINATION + [("siem_delivery", "enabled", "true")])
    epochs: List[str] = []

    def _disable_elsewhere() -> None:
        a.update_settings_atomic([("siem_delivery", "enabled", "false")])
        epochs.append(a.read_committed_section(SIEM)[1]["arming_epoch"])

    service = MCPSelfRegistrationService(
        config_manager=b, mcp_credential_manager=_CredentialManager(_disable_elsewhere)
    )
    creds = service.get_or_create_credentials()

    assert creds == {"client_id": "example-client", "client_secret": "example-secret"}
    _v, siem = a.read_committed_section(SIEM)
    assert siem["enabled"] is False, "a stale self-registration write re-enabled SIEM"
    assert epochs and siem["arming_epoch"] == epochs[0], "the epoch was reverted"
    _v, mcp = a.read_committed_section("mcp_self_registration")
    assert mcp["client_id"] == "example-client"


def test_save_config_refuses_a_stale_whole_config(
    two_processes: Tuple[ConfigService, ConfigService],
) -> None:
    """save_config writes a WHOLE configuration: refused (nothing written)
    when the row moved on since this process loaded it."""
    from code_indexer.server.services.config_service import ConfigChangeConflict

    a, b = two_processes
    stale = b.get_config()
    a.update_settings_atomic([("siem_delivery", "project_id", "example-x")])
    stale.golden_repos_config.refresh_interval_seconds = 7200  # type: ignore[union-attr]
    try:
        b.save_config(stale)
        refused = False
    except ConfigChangeConflict:
        refused = True
    _v, siem = a.read_committed_section(SIEM)
    assert siem["project_id"] == "example-x", "a stale whole-config save reverted X"
    assert refused


def test_seeding_never_overwrites_an_existing_row(
    two_processes: Tuple[ConfigService, ConfigService],
) -> None:
    """The one unconditional write (first-boot seeding) only inserts."""
    a, _b = two_processes
    a.update_settings_atomic([("siem_delivery", "project_id", "example-x")])
    version, before = a._read_committed_runtime()
    stale = dict(before, siem_delivery_config={})
    if a._pool is not None:
        inserted = a._seed_runtime_row_pg(stale)
    else:
        inserted = a._seed_runtime_row_sqlite(stale)
    assert inserted is False
    assert a._read_committed_runtime() == (version, before)
