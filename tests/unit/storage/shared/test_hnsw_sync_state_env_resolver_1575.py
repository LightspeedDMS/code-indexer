"""Bug #1575 Part C remediation (independent re-review): tests for the new
shared resolver ``resolve_hnsw_sync_epoch_env_var()``.

This function is the SINGLE shared source of truth every server-side `cidx
index` child-process spawn site must call to decide whether to set
``CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV`` on the child's environment. It
existing and being correct in isolation is NOT sufficient to prove the bug
is fixed -- the original gap was that most spawn sites never called any
such check at all. These tests cover the resolver itself; sibling test
files cover each individual call site actually invoking it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from code_indexer.storage.shared.hnsw_sync_state import (
    CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
    resolve_hnsw_sync_epoch_env_var,
)


class TestResolveHnswSyncEpochEnvVar:
    def test_returns_var_when_postgres_mode(self):
        fake_config = MagicMock()
        fake_config.storage_mode = "postgres"
        with patch(
            "code_indexer.server.services.config_service.get_config_service"
        ) as mock_get_cfg_svc:
            mock_get_cfg_svc.return_value.get_config.return_value = fake_config

            result = resolve_hnsw_sync_epoch_env_var()

        assert result == {CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV: "1"}

    def test_returns_empty_when_sqlite_mode(self):
        fake_config = MagicMock()
        fake_config.storage_mode = "sqlite"
        with patch(
            "code_indexer.server.services.config_service.get_config_service"
        ) as mock_get_cfg_svc:
            mock_get_cfg_svc.return_value.get_config.return_value = fake_config

            result = resolve_hnsw_sync_epoch_env_var()

        assert result == {}

    def test_returns_empty_and_does_not_raise_when_config_service_unavailable(self):
        with patch(
            "code_indexer.server.services.config_service.get_config_service",
            side_effect=RuntimeError("config service not initialized"),
        ):
            result = resolve_hnsw_sync_epoch_env_var()

        assert result == {}
