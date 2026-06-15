"""
Tests for ApiMetricsBackend Protocol and storage backends (Story #502).

Dead-code cleanup: removed AC1/AC2/AC4 tests for the legacy api_metrics table
(insert_metric, get_metrics, cleanup_old). Those methods were deleted from the
postgres backend and protocol in Story #1083 dead-code cleanup — the live path
now writes exclusively to api_metrics_buckets via upsert_buckets_batch().

Remaining coverage:
- AC3: BackendRegistry has api_metrics field; StorageFactory creates it in SQLite mode
- AC5: PostgreSQL backend has all methods required for the active bucket path
"""

import os

import pytest


# ---------------------------------------------------------------------------
# AC3: BackendRegistry and StorageFactory
# ---------------------------------------------------------------------------


class TestBackendRegistryApiMetricsField:
    """Tests for BackendRegistry.api_metrics field (AC3)."""

    def test_backend_registry_has_api_metrics_field(self):
        """BackendRegistry dataclass must have an 'api_metrics' field."""
        from code_indexer.server.storage.factory import BackendRegistry
        import dataclasses

        fields = {f.name for f in dataclasses.fields(BackendRegistry)}
        assert "api_metrics" in fields, (
            "BackendRegistry must have an 'api_metrics' field. "
            f"Current fields: {fields}"
        )

    def test_storage_factory_creates_api_metrics_backend_sqlite_mode(self, tmp_path):
        """StorageFactory._create_sqlite_backends must produce a valid ApiMetricsBackend."""
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.storage.protocols import ApiMetricsBackend

        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir, exist_ok=True)

        registry = StorageFactory._create_sqlite_backends(data_dir)

        assert hasattr(registry, "api_metrics"), (
            "BackendRegistry must have .api_metrics attribute"
        )
        assert isinstance(registry.api_metrics, ApiMetricsBackend), (
            f"registry.api_metrics must satisfy ApiMetricsBackend protocol, "
            f"got {type(registry.api_metrics)}"
        )

    def test_storage_factory_api_metrics_backend_is_functional(self, tmp_path):
        """The api_metrics backend from StorageFactory must support bucket writes and reads."""
        from code_indexer.server.storage.factory import StorageFactory

        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir, exist_ok=True)

        registry = StorageFactory._create_sqlite_backends(data_dir)
        api_metrics = registry.api_metrics

        # Write via the live bucket path (upsert_buckets_batch)
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        events = [
            {
                "username": "_anonymous",
                "metric_type": "semantic",
                "buckets": {
                    "min1": now.replace(second=0, microsecond=0).isoformat(),
                    "min5": now.replace(
                        minute=(now.minute // 5) * 5, second=0, microsecond=0
                    ).isoformat(),
                    "hour1": now.replace(minute=0, second=0, microsecond=0).isoformat(),
                    "day1": now.replace(
                        hour=0, minute=0, second=0, microsecond=0
                    ).isoformat(),
                },
            }
        ]
        api_metrics.upsert_buckets_batch(events, node_id="")

        # Read via the live bucket read path
        result = api_metrics.get_metrics_bucketed(900)
        assert result["semantic_searches"] >= 1


# ---------------------------------------------------------------------------
# AC5: PostgreSQL backend satisfies protocol (structural check, no live DB)
# ---------------------------------------------------------------------------


class TestApiMetricsPostgresBackendProtocolSatisfaction:
    """Structural test: ApiMetricsPostgresBackend must have all active-path methods."""

    def test_postgres_backend_has_all_required_methods(self):
        """ApiMetricsPostgresBackend must declare all methods for the active bucket path."""
        from code_indexer.server.storage.postgres.api_metrics_backend import (
            ApiMetricsPostgresBackend,
        )

        # Active-path methods (bucket writes/reads) — must be present
        for method_name in [
            "upsert_bucket",
            "upsert_buckets_batch",
            "get_metrics_bucketed",
            "get_metrics_by_user",
            "get_metrics_timeseries",
            "cleanup_expired_buckets",
            "reset",
            "close",
        ]:
            assert hasattr(ApiMetricsPostgresBackend, method_name), (
                f"ApiMetricsPostgresBackend must have method '{method_name}'"
            )

    def test_postgres_backend_module_importable(self):
        """The postgres api_metrics_backend module must be importable without a live DB."""
        try:
            from code_indexer.server.storage.postgres import (
                api_metrics_backend,  # noqa: F401
            )
        except ImportError as exc:
            pytest.fail(f"Failed to import api_metrics_backend module: {exc}")
