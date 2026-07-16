"""Bug #1415: `cidx health` must explicitly surface the new
`hnswlib_capability_available` degraded-capability signal in its
human-readable output -- mirroring the existing Orphan Count display
pattern from Story #1359 (test_health_command_1359_orphan_count.py) -- so
operators running against stock hnswlib see it directly, not just buried in
JSON.
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from code_indexer.cli import cli
from code_indexer.services.hnsw_health_service import HealthCheckResult


def _make_result(hnswlib_capability_available) -> HealthCheckResult:
    # mypy cannot see pydantic's Field()-generated __init__ kwargs here --
    # same pre-existing idiom as test_health_command_1359_orphan_count.py's
    # _make_result (that one omits the ignore only because none of its
    # fields trip mypy's positional/keyword inference the same way).
    return HealthCheckResult(  # type: ignore[call-arg]
        valid=True,
        file_exists=True,
        readable=True,
        loadable=True,
        orphan_count=None,
        hnswlib_capability_available=hnswlib_capability_available,
        index_path="/path/to/.code-indexer/index/hnsw.bin",
        file_size_bytes=1024,
        last_modified=datetime(2024, 1, 15, 10, 30, 45, tzinfo=timezone.utc),
        errors=[],
        check_duration_ms=42.0,
        from_cache=False,
    )


class TestHealthCommandDisplaysCapabilityAvailability:
    def test_missing_capability_shown_in_human_readable_output(self):
        runner = CliRunner()
        with patch(
            "code_indexer.services.hnsw_health_service.HNSWHealthService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.check_health.return_value = _make_result(
                hnswlib_capability_available=False
            )
            mock_service_class.return_value = mock_service

            result = runner.invoke(cli, ["health"])

            assert result.exit_code == 0
            assert "hnswlib fork capability" in result.output.lower()
            assert "unavailable" in result.output.lower()

    def test_present_capability_not_flagged_in_human_readable_output(self):
        runner = CliRunner()
        with patch(
            "code_indexer.services.hnsw_health_service.HNSWHealthService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.check_health.return_value = _make_result(
                hnswlib_capability_available=True
            )
            mock_service_class.return_value = mock_service

            result = runner.invoke(cli, ["health"])

            assert result.exit_code == 0
            assert "unavailable" not in result.output.lower()

    def test_json_output_includes_capability_field(self):
        runner = CliRunner()
        with patch(
            "code_indexer.services.hnsw_health_service.HNSWHealthService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.check_health.return_value = _make_result(
                hnswlib_capability_available=False
            )
            mock_service_class.return_value = mock_service

            result = runner.invoke(cli, ["health", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["hnswlib_capability_available"] is False
