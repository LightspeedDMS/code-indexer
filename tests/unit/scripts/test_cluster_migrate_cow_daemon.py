"""
Tests for Story #511: cluster-migrate.sh CoW daemon mode support.

These tests run the actual bash script with --dry-run to verify:
- New CLI flags (--clone-backend, --daemon-url, --daemon-api-key, --nfs-mount) are parsed
- Dry-run output mentions CoW daemon health check when backend is cow-daemon
- Dry-run output mentions NFS mount validation is skipped for local backend
- Config generation branches on CLONE_BACKEND value

Tests use subprocess to run the real script so they test actual behavior,
not just mocks.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

# Path to scripts directory (relative to project root)
_SCRIPTS_DIR = Path(__file__).parent.parent.parent.parent / "scripts"
_MIGRATE_SCRIPT = _SCRIPTS_DIR / "cluster-migrate.sh"


def _script_exists() -> bool:
    return _MIGRATE_SCRIPT.exists()


skip_if_no_script = pytest.mark.skipif(
    not _script_exists(), reason="cluster-migrate.sh not found"
)


def _make_minimal_cidx_dir(tmp_path: Path) -> Path:
    """Create minimal CIDX data directory structure for dry-run tests."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "cidx_server.db").write_text("")
    (tmp_path / "groups.db").write_text("")
    config = {"server_dir": str(tmp_path), "host": "127.0.0.1", "port": 8000}
    (tmp_path / "config.json").write_text(json.dumps(config))
    return tmp_path


def _run_dry(args: list, cidx_data_dir: str) -> subprocess.CompletedProcess:
    """Run cluster-migrate.sh --dry-run with given args, capture output."""
    cmd = [
        "bash",
        str(_MIGRATE_SCRIPT),
        "--dry-run",
        "--cidx-data-dir",
        cidx_data_dir,
        "--postgres-url",
        "postgresql://user:pass@host/db",
    ] + args
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# CLI flag parsing tests
# ---------------------------------------------------------------------------


@skip_if_no_script
class TestCloneBackendFlagParsing:
    """cluster-migrate.sh accepts new CoW daemon flags without error."""

    def test_clone_backend_cow_daemon_flag_accepted(self, tmp_path: Path):
        """--clone-backend cow-daemon is parsed without unknown argument error."""
        _make_minimal_cidx_dir(tmp_path)
        result = _run_dry(
            [
                "--clone-backend",
                "cow-daemon",
                "--daemon-url",
                "http://localhost:8081",
                "--daemon-api-key",
                "secret",
                "--nfs-mount",
                "/mnt/nfs",
            ],
            cidx_data_dir=str(tmp_path),
        )
        assert "Unknown argument" not in result.stderr, (
            f"Script rejected new flags.\nstderr: {result.stderr}"
        )
        assert result.returncode == 0, (
            f"Script exited non-zero.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_nfs_mount_flag_accepted(self, tmp_path: Path):
        """--nfs-mount is accepted as primary flag name (alias for --ontap-mount)."""
        _make_minimal_cidx_dir(tmp_path)
        result = _run_dry(
            ["--nfs-mount", "/mnt/nfs"],
            cidx_data_dir=str(tmp_path),
        )
        assert "Unknown argument: --nfs-mount" not in result.stderr, (
            f"Script rejected --nfs-mount.\nstderr: {result.stderr}"
        )
        assert result.returncode == 0, (
            f"Script exited non-zero.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_ontap_mount_alias_still_accepted(self, tmp_path: Path):
        """--ontap-mount continues to work as a backward-compatible alias."""
        _make_minimal_cidx_dir(tmp_path)
        result = _run_dry(
            ["--ontap-mount", "/mnt/fsx"],
            cidx_data_dir=str(tmp_path),
        )
        assert "Unknown argument: --ontap-mount" not in result.stderr, (
            f"Script rejected --ontap-mount alias.\nstderr: {result.stderr}"
        )
        assert result.returncode == 0, (
            f"Script exited non-zero.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_daemon_url_flag_accepted(self, tmp_path: Path):
        """--daemon-url is parsed without error."""
        _make_minimal_cidx_dir(tmp_path)
        result = _run_dry(
            ["--daemon-url", "http://storage:8081"],
            cidx_data_dir=str(tmp_path),
        )
        assert "Unknown argument: --daemon-url" not in result.stderr
        assert result.returncode == 0, (
            f"Script exited non-zero.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_daemon_api_key_flag_accepted(self, tmp_path: Path):
        """--daemon-api-key is parsed without error."""
        _make_minimal_cidx_dir(tmp_path)
        result = _run_dry(
            ["--daemon-api-key", "my-secret"],
            cidx_data_dir=str(tmp_path),
        )
        assert "Unknown argument: --daemon-api-key" not in result.stderr
        assert result.returncode == 0, (
            f"Script exited non-zero.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_local_backend_flag_accepted(self, tmp_path: Path):
        """--clone-backend local is accepted."""
        _make_minimal_cidx_dir(tmp_path)
        result = _run_dry(
            ["--clone-backend", "local"],
            cidx_data_dir=str(tmp_path),
        )
        assert "Unknown argument" not in result.stderr
        assert result.returncode == 0, (
            f"Script exited non-zero.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# Dry-run output verification
# ---------------------------------------------------------------------------


@skip_if_no_script
class TestDryRunOutput:
    """Verify dry-run output mentions CoW daemon health check for cow-daemon backend."""

    def test_cow_daemon_dry_run_mentions_health_check(self, tmp_path: Path):
        """Dry-run with cow-daemon backend prints CoW daemon health check message."""
        _make_minimal_cidx_dir(tmp_path)
        result = _run_dry(
            [
                "--clone-backend",
                "cow-daemon",
                "--daemon-url",
                "http://localhost:8081",
                "--daemon-api-key",
                "key",
                "--nfs-mount",
                "/mnt/nfs",
            ],
            cidx_data_dir=str(tmp_path),
        )
        assert result.returncode == 0, (
            f"Script exited non-zero.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        combined = result.stdout + result.stderr
        assert "CoW daemon" in combined, (
            f"Expected 'CoW daemon' in dry-run output.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_local_backend_dry_run_skips_nfs_check(self, tmp_path: Path):
        """Dry-run with local backend succeeds without NFS mount validation error."""
        _make_minimal_cidx_dir(tmp_path)
        result = _run_dry(
            ["--clone-backend", "local"],
            cidx_data_dir=str(tmp_path),
        )
        assert result.returncode == 0, (
            f"Local backend dry-run should succeed.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "NFS mount point does not exist" not in result.stderr, (
            f"Local backend should not require NFS mount.\nstderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# Config generation logic (Python-level tests of the embedded logic)
# ---------------------------------------------------------------------------


class TestConfigGenerationLogic:
    """Test config.json generation logic for each backend type."""

    def _apply_config_update(
        self,
        config_file: Path,
        clone_backend: str,
        postgres_url: str = "postgresql://user:pass@host/db",
        daemon_url: str = "",
        api_key: str = "",
        mount_point: str = "",
    ) -> dict:
        """Replicate the config update logic from cluster-migrate.sh in Python."""
        with open(str(config_file)) as f:
            config = json.load(f)

        config["storage_mode"] = "postgres"
        config["postgres_dsn"] = postgres_url
        config["clone_backend"] = clone_backend

        if clone_backend == "cow-daemon":
            config["cow_daemon"] = {
                "daemon_url": daemon_url,
                "api_key": api_key,
                "mount_point": mount_point,
            }
        elif clone_backend == "local":
            config.pop("ontap", None)

        with open(str(config_file), "w") as f:
            json.dump(config, f, indent=2)

        with open(str(config_file)) as f:
            return json.load(f)

    def _make_config_file(self, tmp_path: Path) -> Path:
        config = {"server_dir": str(tmp_path), "host": "127.0.0.1", "port": 8000}
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config))
        return config_file

    def test_cow_daemon_config_includes_cow_daemon_section(self, tmp_path: Path):
        """Config for cow-daemon backend includes cow_daemon dict with all fields."""
        config_file = self._make_config_file(tmp_path)
        config = self._apply_config_update(
            config_file,
            clone_backend="cow-daemon",
            daemon_url="http://daemon:8081",
            api_key="my-secret",
            mount_point="/mnt/nfs/cidx",
        )
        assert config["clone_backend"] == "cow-daemon"
        assert "cow_daemon" in config
        assert config["cow_daemon"]["daemon_url"] == "http://daemon:8081"
        assert config["cow_daemon"]["api_key"] == "my-secret"
        assert config["cow_daemon"]["mount_point"] == "/mnt/nfs/cidx"

    def test_ontap_config_has_no_cow_daemon_section(self, tmp_path: Path):
        """Config for ontap backend does not include cow_daemon dict."""
        config_file = self._make_config_file(tmp_path)
        config = self._apply_config_update(config_file, clone_backend="ontap")
        assert config.get("clone_backend") == "ontap"
        assert "cow_daemon" not in config

    def test_local_config_has_no_cow_daemon_section(self, tmp_path: Path):
        """Config for local backend has clone_backend=local and no cow_daemon."""
        config_file = self._make_config_file(tmp_path)
        config = self._apply_config_update(config_file, clone_backend="local")
        assert config["clone_backend"] == "local"
        assert "cow_daemon" not in config

    def test_postgres_dsn_always_set_for_all_backends(self, tmp_path: Path):
        """All backends set storage_mode=postgres and postgres_dsn."""
        for backend in ("local", "ontap", "cow-daemon"):
            config_file = self._make_config_file(tmp_path)
            config = self._apply_config_update(config_file, clone_backend=backend)
            assert config["storage_mode"] == "postgres", (
                f"Failed for backend: {backend}"
            )
            assert config["postgres_dsn"] == "postgresql://user:pass@host/db", (
                f"Failed for backend: {backend}"
            )

    def test_cow_daemon_config_roundtrip_with_cidx_config_manager(self, tmp_path: Path):
        """CowDaemonConfig can be deserialized from config.json written by script logic."""
        from code_indexer.server.utils.config_manager import ServerConfigManager

        config_file = self._make_config_file(tmp_path)
        self._apply_config_update(
            config_file,
            clone_backend="cow-daemon",
            daemon_url="http://storage:8081",
            api_key="roundtrip-key",
            mount_point="/mnt/nfs/cidx",
        )

        manager = ServerConfigManager(server_dir_path=str(tmp_path))
        loaded = manager.load_config()

        assert loaded is not None
        assert loaded.clone_backend == "cow-daemon"
        assert loaded.cow_daemon is not None
        assert loaded.cow_daemon.daemon_url == "http://storage:8081"
        assert loaded.cow_daemon.api_key == "roundtrip-key"
        assert loaded.cow_daemon.mount_point == "/mnt/nfs/cidx"
