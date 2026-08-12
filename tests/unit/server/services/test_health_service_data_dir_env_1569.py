"""
Unit tests for Bug #1569: HealthCheckService hardcodes ~/.cidx-server/data
and ignores the CIDX_SERVER_DATA_DIR environment variable that the rest of
the server (ServerConfigManager) honors.

Real HealthCheckService construction, no mocking of the class under test --
only the environment variable is manipulated (monkeypatch, auto-restored).
"""

from pathlib import Path

from code_indexer.server.services.health_service import HealthCheckService


def test_data_dir_honors_cidx_server_data_dir_env_var(tmp_path, monkeypatch):
    """When CIDX_SERVER_DATA_DIR points at an isolated directory, the health
    service must resolve its data_dir/database_url under THAT directory's
    'data' subdirectory -- never the hardcoded ~/.cidx-server/data.

    This is the exact scenario from the bug's live reproduction: a second
    server instance with CIDX_SERVER_DATA_DIR set must never read a
    different (foreign) database's health signals.
    """
    isolated_server_dir = tmp_path / "isolated-cidx-server"
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(isolated_server_dir))

    service = HealthCheckService()

    expected_data_dir = isolated_server_dir / "data"
    assert service.data_dir == expected_data_dir, (
        f"data_dir must resolve under CIDX_SERVER_DATA_DIR "
        f"({isolated_server_dir}), got {service.data_dir}"
    )
    assert service.database_url == f"sqlite:///{expected_data_dir}/cidx_server.db"

    # The default (unrelated) home directory must NOT have been touched by
    # this isolated-instance construction.
    home_default_data_dir = Path.home() / ".cidx-server" / "data"
    assert service.data_dir != home_default_data_dir


def test_data_dir_defaults_to_home_cidx_server_when_env_absent(monkeypatch):
    """Regression guard: with CIDX_SERVER_DATA_DIR unset, behavior for the
    common (non-relocated) deployment must remain byte-identical to before
    the fix -- ~/.cidx-server/data, matching ServerConfigManager's own
    default (server/utils/config_manager.py)."""
    monkeypatch.delenv("CIDX_SERVER_DATA_DIR", raising=False)

    service = HealthCheckService()

    expected_data_dir = Path.home() / ".cidx-server" / "data"
    assert service.data_dir == expected_data_dir
    assert service.database_url == f"sqlite:///{expected_data_dir}/cidx_server.db"
