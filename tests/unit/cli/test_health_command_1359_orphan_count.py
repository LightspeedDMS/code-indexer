"""
Story #1359 (Epic #1333, S2) AC4: `cidx health` exposes orphan_count.

Zero-tolerance binary design: orphan_count == 0 is OK, any orphan_count > 0
is ERROR (already reflected in exit code via `valid`, unchanged by this
story). This test suite locks in that the CLI human-readable output surfaces
the orphan_count explicitly (not just buried in the generic errors list),
and that JSON output includes it.
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from code_indexer.cli import cli
from code_indexer.services.hnsw_health_service import HealthCheckResult


def _make_result(orphan_count: int, valid: bool) -> HealthCheckResult:
    errors = [
        f"Element {i} has no inbound connections (orphan)" for i in range(orphan_count)
    ]
    return HealthCheckResult(
        valid=valid,
        file_exists=True,
        readable=True,
        loadable=True,
        element_count=1000,
        connections_checked=5000,
        min_inbound=0 if orphan_count else 2,
        max_inbound=10,
        orphan_count=orphan_count,
        index_path="/path/to/.code-indexer/index/hnsw.bin",
        file_size_bytes=1024,
        last_modified=datetime(2024, 1, 15, 10, 30, 45, tzinfo=timezone.utc),
        errors=errors,
        check_duration_ms=42.0,
        from_cache=False,
    )


class TestHealthCommandDisplaysOrphanCount:
    def test_healthy_index_shows_zero_orphan_count(self):
        runner = CliRunner()
        with patch(
            "code_indexer.services.hnsw_health_service.HNSWHealthService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.check_health.return_value = _make_result(
                orphan_count=0, valid=True
            )
            mock_service_class.return_value = mock_service

            result = runner.invoke(cli, ["health"])

            assert result.exit_code == 0
            assert "Orphan Count: 0" in result.output

    def test_broken_index_shows_nonzero_orphan_count_and_nonzero_exit(self):
        runner = CliRunner()
        with patch(
            "code_indexer.services.hnsw_health_service.HNSWHealthService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.check_health.return_value = _make_result(
                orphan_count=4, valid=False
            )
            mock_service_class.return_value = mock_service

            result = runner.invoke(cli, ["health"])

            assert result.exit_code != 0
            assert "Orphan Count: 4" in result.output

    def test_json_output_includes_orphan_count(self):
        runner = CliRunner()
        with patch(
            "code_indexer.services.hnsw_health_service.HNSWHealthService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.check_health.return_value = _make_result(
                orphan_count=3, valid=False
            )
            mock_service_class.return_value = mock_service

            result = runner.invoke(cli, ["health", "--json"])

            data = json.loads(result.output)
            assert data["orphan_count"] == 3
