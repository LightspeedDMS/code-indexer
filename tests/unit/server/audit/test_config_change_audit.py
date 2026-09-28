"""The audited configuration change: publish semantics and its one row.

Real ConfigService on its own temporary server directory, real bound audit
store.  The only substitution is a save that raises (to prove a failed
publish restores the previous live config).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator

import pytest

from _audit_accounts_support import (
    CAPTURE_LOGGER,
    AuditStore,
    bound_audit_store,
    capture_errors,
)

_ACTOR = "example-second-admin"


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[AuditStore]:
    yield from bound_audit_store(tmp_path / "audit.db")


@pytest.fixture(autouse=True)
def _no_capture_errors(caplog) -> Iterator[None]:
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    yield
    assert capture_errors(caplog, phases=["setup", "call"]) == []


@pytest.fixture()
def service(tmp_path: Path):
    from code_indexer.server.services.config_service import ConfigService

    server_dir = tmp_path / "server"
    (server_dir / "data").mkdir(parents=True)
    svc = ConfigService(server_dir_path=str(server_dir))
    svc.load_config()
    return svc


def _set_retention(hours: int):
    def _mutate(candidate) -> None:
        candidate.data_retention_config.audit_logs_retention_hours = hours

    return _mutate


def test_a_failed_publish_restores_the_live_config_and_records_a_failure(
    store, service, monkeypatch
) -> None:
    def _failing_save(config) -> None:
        service._config = config  # the real save swaps the cache first
        raise OSError("disk full")

    monkeypatch.setattr(service, "save_config", _failing_save)
    with pytest.raises(OSError):
        service.apply_audited_change(
            _set_retention(100), actor=_ACTOR, target_id="data_retention"
        )
    assert service.get_config().data_retention_config.audit_logs_retention_hours == (
        2160
    )
    (row,) = store.rows("config_changed")
    assert (row.actor, row.outcome, row.target_id) == (
        _ACTOR,
        "failure",
        "data_retention",
    )
    assert row.details == {
        "change_kind": "update",
        "attempted_keys": ["data_retention_config.audit_logs_retention_hours"],
    }


def test_a_bootstrap_file_failure_after_the_runtime_commit_is_published(
    store, service, tmp_path: Path, monkeypatch, caplog
) -> None:
    """Once the runtime DB row is committed the change IS published: the
    cache and the audit row must agree with the DB, and the caller still
    learns that config.json was not written."""
    from code_indexer.server.services.config_service import (
        BootstrapFileNotWritten,
        ConfigService,
    )
    from code_indexer.server.storage.database_manager import DatabaseSchema

    db_path = str(tmp_path / "server" / "data" / "cidx_server.db")
    DatabaseSchema(db_path).initialize_database()
    service.initialize_runtime_db(db_path)

    def _failing_file_write(_config_dict) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(service.config_manager, "save_config_dict", _failing_file_write)
    with pytest.raises(BootstrapFileNotWritten):
        service.apply_audited_change(
            _set_retention(100), actor=_ACTOR, target_id="data_retention"
        )

    assert service.get_config().data_retention_config.audit_logs_retention_hours == 100
    fresh = ConfigService(server_dir_path=str(tmp_path / "server"))
    fresh.load_config()
    fresh.initialize_runtime_db(db_path)
    persisted = fresh.get_config().data_retention_config
    assert persisted is not None
    assert persisted.audit_logs_retention_hours == 100
    (row,) = store.rows("config_changed")
    assert (row.outcome, row.target_id) == ("success", "data_retention")
    assert row.details["changed_keys"] == [
        "data_retention_config.audit_logs_retention_hours"
    ]
    assert any(
        "bootstrap config file" in r.getMessage() and r.levelno >= logging.ERROR
        for r in caplog.records
    )


def test_a_before_publish_failure_publishes_nothing(store, service) -> None:
    def _reject(candidate) -> None:
        assert candidate.data_retention_config.audit_logs_retention_hours == 100
        raise RuntimeError("rejected")

    with pytest.raises(RuntimeError):
        service.apply_audited_change(
            _set_retention(100),
            actor=_ACTOR,
            target_id="data_retention",
            before_publish=_reject,
        )
    assert service.get_config().data_retention_config.audit_logs_retention_hours == (
        2160
    )
    assert [r.outcome for r in store.rows("config_changed")] == ["failure"]


def test_an_audited_change_requires_an_actor(store, service) -> None:
    with pytest.raises(ValueError):
        service.apply_audited_change(
            _set_retention(100), actor="", target_id="data_retention"
        )
    assert service.get_config().data_retention_config.audit_logs_retention_hours == (
        2160
    )
    assert store.rows("") == []


def test_unaudited_primitives_write_no_row(store, service) -> None:
    service.update_setting("data_retention", "audit_logs_retention_hours", 100)
    service.update_totp_elevation_atomic(True, 600, 1800)
    service.update_setting("indexing", "voyage_ai_parallel_requests", 12)
    live = service.get_config()
    assert live.elevation_idle_timeout_seconds == 600
    assert live.indexing_config.voyage_ai_parallel_requests == 12
    assert store.rows("") == []


def test_indexing_keys_are_published_atomically_with_the_batch(service) -> None:
    """A rejected batch leaves the indexing key unpublished too."""
    with pytest.raises(ValueError):
        service.update_settings_atomic(
            [
                ("indexing", "voyage_ai_parallel_requests", 12),
                ("data_retention", "no_such_key", 1),
            ]
        )
    assert service.get_config().indexing_config.voyage_ai_parallel_requests == 8


def test_applied_updates_are_logged_by_name_never_by_value(
    store, service, caplog
) -> None:
    sentinel = "SENTINEL-7f3a-log"
    caplog.set_level(logging.DEBUG)
    service.update_settings_audited([("oidc", "client_secret", sentinel)], actor=_ACTOR)
    assert service.get_config().oidc_provider_config.client_secret == sentinel
    assert any("oidc.client_secret" in r.getMessage() for r in caplog.records)
    assert all(sentinel not in r.getMessage() for r in caplog.records)


def test_changed_keys_are_schema_field_paths_only() -> None:
    from code_indexer.server.services.config_change_audit import diff_config_keys
    from code_indexer.server.utils.config_manager import OIDCProviderConfig

    before = OIDCProviderConfig()
    after = OIDCProviderConfig(
        group_mappings=[{"external_group_id": "SENTINEL-key", "cidx_group": "x"}]
    )
    assert diff_config_keys(before, after) == ["group_mappings"]


def test_values_are_recorded_only_for_allowlisted_scalar_tokens() -> None:
    from code_indexer.server.services.config_change_audit import config_details
    from code_indexer.server.utils.config_manager import OIDCProviderConfig

    before = OIDCProviderConfig()
    after = OIDCProviderConfig(
        enabled=True, client_secret="SENTINEL-secret", issuer_url="https://x"
    )
    details = config_details(
        "config_changed",
        change_kind="update",
        before=before,
        after=after,
        outcome="success",
        provider=None,
    )
    assert details["changed_keys"] == ["client_secret", "enabled", "issuer_url"]
    # Bare dataclass paths are not in the allowlist: no value is recorded.
    assert details["values"] == {}


class _RecordingSessionManager:
    """Stands in for the live elevated-session manager (records hot reloads)."""

    def __init__(self) -> None:
        self.timeouts: list = []

    def update_timeouts(self, idle: int, max_age: int) -> None:
        self.timeouts.append((idle, max_age))


def test_totp_hot_reload_runs_when_only_the_bootstrap_file_fails(
    store, service, tmp_path: Path, monkeypatch
) -> None:
    from code_indexer.server.services.config_service import BootstrapFileNotWritten
    from code_indexer.server.storage.database_manager import DatabaseSchema

    db_path = str(tmp_path / "server" / "data" / "cidx_server.db")
    DatabaseSchema(db_path).initialize_database()
    service.initialize_runtime_db(db_path)

    def _failing_file_write(_config_dict) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(service.config_manager, "save_config_dict", _failing_file_write)
    sessions = _RecordingSessionManager()
    with pytest.raises(BootstrapFileNotWritten):
        service.update_totp_elevation_audited(
            True, 600, 1800, session_manager=sessions, actor=_ACTOR
        )
    assert sessions.timeouts == [(600, 1800)]
    assert service.get_config().elevation_idle_timeout_seconds == 600
    assert [r.outcome for r in store.rows("config_changed")] == ["success"]
