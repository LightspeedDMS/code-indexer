"""GlobalReposLifecycleManager wires the live AliasLockConfig.db_backed_enabled
getter and an AliasLockStoreFactory-backed resolver into its RefreshScheduler
(Issue #1546 Phase 2).

This is the anti-orphan-code guard (Messi Rule #12), mirroring
test_global_repos_lifecycle_golden_repo_metadata_wiring_1390.py's pattern:
RefreshScheduler's alias_lock_db_backed_enabled_getter/alias_lock_store_resolver
constructor params exist only to be wired here. If this wiring regresses, the
operator-controlled rollout flag (Web UI Config Screen) becomes permanently
inert -- flipping it would do nothing because RefreshScheduler's
AliasLockCoordinator would still be stuck reading its default always-False
getter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pytest

from code_indexer.server.lifecycle.global_repos_lifecycle import (
    GlobalReposLifecycleManager,
)
from code_indexer.server.services.alias_lock_store.sqlite_store import (
    SqliteAliasLockStore,
)
from code_indexer.server.services.config_service import (
    reset_config_service,
    set_config_service,
)
from code_indexer.server.utils.config_manager import AliasLockConfig


@dataclass
class _FakeServerConfig:
    alias_lock_config: AliasLockConfig = field(default_factory=AliasLockConfig)
    postgres_dsn: Optional[str] = None
    snapshot_min_retention_age_seconds: float = 0.0


class _FakeConfigService:
    def __init__(self, config: _FakeServerConfig) -> None:
        self._config = config

    def get_config(self) -> _FakeServerConfig:
        return self._config


@pytest.fixture(autouse=True)
def _reset_config_service_around_test():
    reset_config_service()
    yield
    reset_config_service()


class TestAliasLockGetterWiring:
    def test_getter_reflects_live_config_flag_true(self, tmp_path):
        fake_config = _FakeServerConfig(
            alias_lock_config=AliasLockConfig(db_backed_enabled=True)
        )
        set_config_service(_FakeConfigService(fake_config))

        lifecycle = GlobalReposLifecycleManager(
            golden_repos_dir=str(tmp_path / "golden-repos"),
        )

        getter = lifecycle.refresh_scheduler._alias_lock_db_backed_enabled_getter
        assert getter is not None
        assert getter() is True

    def test_getter_reflects_live_config_flag_true_by_default(self, tmp_path):
        fake_config = _FakeServerConfig(alias_lock_config=AliasLockConfig())
        set_config_service(_FakeConfigService(fake_config))

        lifecycle = GlobalReposLifecycleManager(
            golden_repos_dir=str(tmp_path / "golden-repos"),
        )

        getter = lifecycle.refresh_scheduler._alias_lock_db_backed_enabled_getter
        assert getter is not None
        assert getter() is True

    def test_getter_reflects_live_config_flag_false_when_explicitly_disabled(
        self, tmp_path
    ):
        """The emergency-rollback path: an operator explicitly disabling
        the flag (e.g. a fleet with nodes still mid-rollout to the new
        code) must still be observed correctly through the getter."""
        fake_config = _FakeServerConfig(
            alias_lock_config=AliasLockConfig(db_backed_enabled=False)
        )
        set_config_service(_FakeConfigService(fake_config))

        lifecycle = GlobalReposLifecycleManager(
            golden_repos_dir=str(tmp_path / "golden-repos"),
        )

        getter = lifecycle.refresh_scheduler._alias_lock_db_backed_enabled_getter
        assert getter is not None
        assert getter() is False

    def test_flag_flip_is_observed_live_no_restart_needed(self, tmp_path):
        """The getter must re-read the config on EVERY call, not cache a
        snapshot at construction time -- an operator flipping the Web UI
        toggle must take effect without a server restart."""
        fake_config = _FakeServerConfig(
            alias_lock_config=AliasLockConfig(db_backed_enabled=False)
        )
        set_config_service(_FakeConfigService(fake_config))

        lifecycle = GlobalReposLifecycleManager(
            golden_repos_dir=str(tmp_path / "golden-repos"),
        )
        getter = lifecycle.refresh_scheduler._alias_lock_db_backed_enabled_getter
        assert getter() is False

        fake_config.alias_lock_config.db_backed_enabled = True
        assert getter() is True


class TestAliasLockStoreResolverWiring:
    def test_resolver_returns_sqlite_store_when_enabled_in_solo_mode(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "code_indexer.server.services.alias_lock_store.factory."
            "is_postgres_storage_mode",
            lambda: False,
        )
        set_config_service(
            _FakeConfigService(
                _FakeServerConfig(
                    alias_lock_config=AliasLockConfig(db_backed_enabled=True),
                    postgres_dsn=None,
                )
            )
        )

        lifecycle = GlobalReposLifecycleManager(
            golden_repos_dir=str(tmp_path / "golden-repos"),
        )

        getter = lifecycle.refresh_scheduler._alias_lock_db_backed_enabled_getter
        resolver = lifecycle.refresh_scheduler._alias_lock_store_resolver
        assert getter is not None and getter() is True
        assert resolver is not None
        store = resolver()
        assert isinstance(store, SqliteAliasLockStore)

    def test_resolver_fails_loud_when_cidx_data_dir_overlaps_golden_repos_dir(
        self, tmp_path, monkeypatch
    ):
        """Codex Fix 1 (containment): the production resolver wiring must
        thread golden_repos_dir into AliasLockStoreFactory so an
        operator-misconfigured CIDX_DATA_DIR that overlaps the NFS-shared
        golden-repos directory fails loud here too -- not just when a
        test constructs AliasLockStoreFactory directly."""
        import pytest

        monkeypatch.setattr(
            "code_indexer.server.services.alias_lock_store.factory."
            "is_postgres_storage_mode",
            lambda: False,
        )
        golden_repos_dir = tmp_path / "golden-repos"
        monkeypatch.setenv("CIDX_DATA_DIR", str(golden_repos_dir))
        set_config_service(
            _FakeConfigService(
                _FakeServerConfig(
                    alias_lock_config=AliasLockConfig(db_backed_enabled=True),
                    postgres_dsn=None,
                )
            )
        )

        lifecycle = GlobalReposLifecycleManager(
            golden_repos_dir=str(golden_repos_dir),
        )

        resolver = lifecycle.refresh_scheduler._alias_lock_store_resolver
        assert resolver is not None

        with pytest.raises(RuntimeError, match="golden-repos"):
            resolver()
