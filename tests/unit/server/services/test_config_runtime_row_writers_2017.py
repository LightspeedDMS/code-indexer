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

import pytest

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


def _restart_with_interleaving(
    a: ConfigService, b: SiemBackendHarness, tmp_path: Path
) -> ConfigService:
    """Worker R restarts: it reads the row, then -- before R decides on its
    startup migrations -- worker W boots (running them itself) and another
    process commits SIEM project X."""
    restarting = ConfigService(server_dir_path=str(tmp_path / "node-a"))
    original_merge = restarting._merge_runtime_config
    injected: List[bool] = []

    def _merge_after_others(
        runtime_dict: dict, base_config: Optional[ServerConfig] = None
    ) -> None:
        if not injected:
            injected.append(True)
            w_dir = tmp_path / "node-w"
            w_dir.mkdir()
            worker_w = ConfigService(
                server_dir_path=str(
                    tmp_path / "node-a" if b.name == "sqlite" else w_dir
                )
            )
            _attach(worker_w, b, tmp_path / "node-a" / "cidx_server.db")
            a.update_settings_atomic([("siem_delivery", "project_id", "example-x")])
        original_merge(runtime_dict, base_config=base_config)

    restarting._merge_runtime_config = _merge_after_others  # type: ignore[method-assign]
    _attach(restarting, b, tmp_path / "node-a" / "cidx_server.db")
    assert injected, "the interleaving was not exercised"
    return restarting


def _cached(service: ConfigService) -> ServerConfig:
    return service.get_config()


def test_rewrite_noop_publishes_the_committed_row_alias_promotion(
    two_processes: Tuple[ConfigService, ConfigService],
    siem_backend: SiemBackendHarness,  # noqa: F811
    tmp_path: Path,
) -> None:
    a, _b = two_processes

    def _pre_promotion(runtime: Dict[str, Any]) -> None:
        runtime["alias_lock_config"] = {"db_backed_enabled": False}

    _edit_committed_row(a, _pre_promotion)
    r = _restart_with_interleaving(a, siem_backend, tmp_path)

    _v, committed = a.read_committed_section("alias_lock_config")
    assert committed["db_backed_enabled"] is True  # W promoted the row
    lock = _cached(r).alias_lock_config
    assert lock is not None and lock.db_backed_enabled is True, "R kept a stale row"
    siem = _cached(r).siem_delivery_config
    assert siem is not None and siem.project_id == "example-x"


def test_rewrite_noop_publishes_the_committed_row_lifecycle(
    two_processes: Tuple[ConfigService, ConfigService],
    siem_backend: SiemBackendHarness,  # noqa: F811
    tmp_path: Path,
) -> None:
    if siem_backend.name != "sqlite":
        pytest.skip("the lifecycle backfill runs on the SQLite startup path only")
    a, _b = two_processes
    _edit_committed_row(a, lambda runtime: runtime.pop("lifecycle_analysis_config"))
    r = _restart_with_interleaving(a, siem_backend, tmp_path)

    siem = _cached(r).siem_delivery_config
    assert siem is not None and siem.project_id == "example-x", "R kept a stale row"


def test_pg_first_boot_seed_loser_adopts_the_winning_row(
    pg_pool: Any,  # noqa: F811  (the imported fixture; server_config emptied)
    tmp_path: Path,
) -> None:
    """Two nodes boot on an empty table; A wins the seed (and commits X)
    just before B's insert, which therefore inserts nothing."""
    dir_a, dir_b = tmp_path / "node-a", tmp_path / "node-b"
    dir_a.mkdir()
    dir_b.mkdir()
    node_b = ConfigService(server_dir_path=str(dir_b))
    node_b.load_config()
    original_seed = node_b._seed_runtime_row_pg
    node_a = ConfigService(server_dir_path=str(dir_a))

    def _seed_after_a_won(runtime_dict: dict) -> bool:
        node_a.load_config()
        node_a.set_connection_pool(pg_pool)  # A seeds first
        node_a.update_settings_atomic([("siem_delivery", "project_id", "example-x")])
        return original_seed(runtime_dict)

    node_b._seed_runtime_row_pg = _seed_after_a_won  # type: ignore[method-assign]
    node_b.set_connection_pool(pg_pool)

    version, _runtime = node_a._read_committed_runtime()
    siem = node_b.get_config().siem_delivery_config
    assert siem is not None and siem.project_id == "example-x", "B kept its own seed"
    assert node_b._db_config_version == version


def test_runtime_row_write_sql_lives_only_in_config_runtime_row() -> None:
    """Every write of server_config goes through the compare-and-set module."""
    import re

    import code_indexer

    package = Path(code_indexer.__file__).parent
    writes = re.compile(r"(UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+server_config\b")
    offenders = sorted(
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if path.name != "config_runtime_row.py" and writes.search(path.read_text())
    )
    assert offenders == []


def test_launch_restart_bump_during_a_settings_save_loses_nothing(
    two_processes: Tuple[ConfigService, ConfigService],
) -> None:
    a, b = two_processes
    version_before = a._db_config_version
    bumped: List[bool] = []

    def _bump_elsewhere(_candidate: Any) -> None:  # after B's read, before commit
        if not bumped:  # once: B's retry then starts from the bumped row
            bumped.append(True)
            a.bump_launch_restart_generation()

    b.apply_audited_change(
        lambda c: setattr(c.siem_delivery_config, "project_id", "example-x"),
        actor="admin",
        target_id="siem_delivery",
        before_publish=_bump_elsewhere,
    )
    _version, runtime = a._read_committed_runtime()
    assert runtime["launch_restart_generation"] == 1, "the bump was lost"
    assert runtime["siem_delivery_config"]["project_id"] == "example-x"
    assert a._db_config_version == version_before  # the restart poll still sees it


def _updated_by(service: ConfigService) -> str:
    """The committed row's updated_by column (read-only)."""
    if service._pool is not None:
        with service._pool.connection() as conn:
            row = conn.execute(
                "SELECT updated_by FROM server_config WHERE config_key = 'runtime'"
            ).fetchone()
        return str(row[0])
    assert service._sqlite_db_path is not None
    conn = sqlite3.connect(service._sqlite_db_path)
    try:
        found = conn.execute(
            "SELECT updated_by FROM server_config WHERE config_key = 'runtime'"
        ).fetchone()
    finally:
        conn.close()
    return str(found[0])


def test_writers_record_who_wrote_the_row(
    two_processes: Tuple[ConfigService, ConfigService],
) -> None:
    a, _b = two_processes
    a.update_settings_atomic([("siem_delivery", "project_id", "example-x")])
    assert _updated_by(a) == "web-ui"
    a.apply_system_change(
        lambda c: setattr(c.siem_delivery_config, "project_id", "example-y")
    )
    assert _updated_by(a) == "system"
    a.bump_launch_restart_generation()
    assert _updated_by(a) == "launch-restart"
    a._rewrite_committed_row(lambda _candidate, _raw: True)
    assert _updated_by(a) == "startup-migration"


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
