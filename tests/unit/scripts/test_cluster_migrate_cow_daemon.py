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
from typing import cast

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


def _run_dry_combined(args: list, cidx_data_dir: str) -> tuple[int, str]:
    """Run dry-run script and return (returncode, combined stdout+stderr)."""
    result = _run_dry(args, cidx_data_dir)
    return result.returncode, result.stdout + result.stderr


def _setup_and_run(tmp_path: Path, args: list, setup_extra=None) -> str:
    """Create minimal cidx dir, optionally run setup_extra(tmp_path), run dry-run.

    setup_extra: optional callable(tmp_path) invoked after minimal dir creation
    and before the script, allowing tests to create extra fixtures without
    bypassing this helper.
    """
    _make_minimal_cidx_dir(tmp_path)
    if setup_extra is not None:
        setup_extra(tmp_path)
    rc, combined = _run_dry_combined(args, str(tmp_path))
    assert rc == 0, f"Script exited non-zero.\ncombined: {combined}"
    return combined


def _assert_contains_all(combined: str, tokens: list[str]) -> None:
    """Assert every token appears in combined output."""
    for token in tokens:
        assert token in combined, f"Expected {token!r} in output.\ncombined: {combined}"


def _assert_contains_none(combined: str, tokens: list[str]) -> None:
    """Assert no token appears in combined output."""
    for token in tokens:
        assert token not in combined, (
            f"Expected {token!r} to be absent from output.\ncombined: {combined}"
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
            return cast(
                dict, json.load(f)
            )  # json.load returns Any; config files are always objects

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


# ---------------------------------------------------------------------------
# Bug fix tests: node_id (Bug 1), optional DB paths (Bug 2),
# local backend NFS skip (Bug 3)
# ---------------------------------------------------------------------------

_OPTIONAL_DB_TOKENS = ["oauth", "scip_audit", "refresh_tokens"]


def _create_optional_dbs(tmp_path: Path) -> None:
    """setup_extra: create oauth.db, scip_audit.db, refresh_tokens.db in data dir."""
    data_dir = tmp_path / "data"
    for name in ("oauth.db", "scip_audit.db", "refresh_tokens.db"):
        (data_dir / name).write_text("")


@skip_if_no_script
class TestNodeIdConfig:
    """Bug 1: cluster.node_id must be set in config.json for leader election."""

    def test_node_id_appears_in_dry_run_output(self, tmp_path: Path):
        """--node-id test-node-1 causes 'node_id' to appear in dry-run output."""
        combined = _setup_and_run(tmp_path, ["--node-id", "test-node-1"])
        assert "node_id" in combined, (
            f"Expected 'node_id' in dry-run output when --node-id provided.\n"
            f"combined: {combined}"
        )

    def test_node_id_flag_accepted_without_error(self, tmp_path: Path):
        """--node-id is parsed without 'Unknown argument' error."""
        combined = _setup_and_run(tmp_path, ["--node-id", "staging-abc123"])
        assert "Unknown argument" not in combined, (
            f"Script rejected --node-id flag.\ncombined: {combined}"
        )

    def test_node_id_auto_generated_when_not_provided(self, tmp_path: Path):
        """Without --node-id, script auto-generates one and 'node_id' appears in dry-run."""
        combined = _setup_and_run(tmp_path, [])
        assert "node_id" in combined, (
            f"Expected auto-generated 'node_id' in dry-run output.\ncombined: {combined}"
        )


@skip_if_no_script
class TestLocalBackendSkipsNfsCopy:
    """Bug 3: --clone-backend local must guard the three NFS copy calls in main()."""

    def test_local_backend_skips_nfs_copy(self, tmp_path: Path):
        """Dry-run with local backend prints a message indicating NFS copy is skipped."""
        combined = _setup_and_run(tmp_path, ["--clone-backend", "local"])
        assert (
            "skipping NFS copy" in combined or "skipping nfs copy" in combined.lower()
        ), (
            f"Expected 'skipping NFS copy' message for local backend.\ncombined: {combined}"
        )

    def test_local_backend_does_not_mention_rsync_to_nfs(self, tmp_path: Path):
        """Dry-run with local backend does not attempt rsync to root /golden-repos/."""
        combined = _setup_and_run(tmp_path, ["--clone-backend", "local"])
        assert "-> /golden-repos/" not in combined, (
            f"Local backend must not rsync to /golden-repos/ (empty ONTAP_MOUNT).\n"
            f"combined: {combined}"
        )


@skip_if_no_script
class TestOptionalDbPaths:
    """Bug 2: optional DB files must be conditionally passed to the migration tool."""

    def test_optional_dbs_mentioned_in_dry_run_when_present(self, tmp_path: Path):
        """When oauth.db/scip_audit.db/refresh_tokens.db exist, dry-run names each."""
        combined = _setup_and_run(tmp_path, [], setup_extra=_create_optional_dbs)
        _assert_contains_all(combined, _OPTIONAL_DB_TOKENS)

    def test_optional_dbs_not_mentioned_when_absent(self, tmp_path: Path):
        """When optional DB files are absent, none of their names appear in dry-run output."""
        combined = _setup_and_run(tmp_path, [])
        _assert_contains_none(combined, _OPTIONAL_DB_TOKENS)


# ---------------------------------------------------------------------------
# Storage node flag tests (NFS self-mount prevention)
# ---------------------------------------------------------------------------


@skip_if_no_script
class TestIsStorageNodeFlag:
    """--is-storage-node flag: storage server node uses local path, never NFS self-mount."""

    def test_is_storage_node_flag_accepted(self, tmp_path: Path):
        """--is-storage-node is parsed without 'Unknown argument' error."""
        _make_minimal_cidx_dir(tmp_path)
        result = _run_dry(
            ["--is-storage-node", "--nfs-mount", "/mnt/cow-storage"],
            cidx_data_dir=str(tmp_path),
        )
        assert "Unknown argument" not in result.stderr, (
            f"Script rejected --is-storage-node flag.\nstderr: {result.stderr}"
        )
        assert result.returncode == 0, (
            f"Script exited non-zero.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_storage_node_dry_run_mentions_local_path(self, tmp_path: Path):
        """Dry-run with --is-storage-node prints 'Storage node' and 'local path' message."""
        combined = _setup_and_run(
            tmp_path, ["--is-storage-node", "--nfs-mount", "/mnt/cow-storage"]
        )
        assert "Storage node" in combined, (
            f"Expected 'Storage node' in dry-run output when --is-storage-node provided.\n"
            f"combined: {combined}"
        )
        assert "local path" in combined, (
            f"Expected 'local path' in dry-run output when --is-storage-node provided.\n"
            f"combined: {combined}"
        )

    def test_storage_node_dry_run_no_nfs_validation(self, tmp_path: Path):
        """Dry-run with --is-storage-node does NOT print 'Would validate NFS mount'."""
        combined = _setup_and_run(
            tmp_path, ["--is-storage-node", "--nfs-mount", "/mnt/cow-storage"]
        )
        assert "Would validate NFS mount" not in combined, (
            f"Storage node must not trigger NFS mount validation message.\n"
            f"combined: {combined}"
        )

    def test_non_storage_node_dry_run_mentions_nfs(self, tmp_path: Path):
        """Without --is-storage-node, dry-run output contains 'Would validate NFS mount'."""
        combined = _setup_and_run(tmp_path, ["--nfs-mount", "/mnt/nfs"])
        assert "Would validate NFS mount" in combined, (
            f"Expected NFS mount validation message for non-storage-node.\n"
            f"combined: {combined}"
        )

    def test_storage_node_with_cow_daemon_backend(self, tmp_path: Path):
        """--is-storage-node combined with --clone-backend cow-daemon in dry-run."""
        combined = _setup_and_run(
            tmp_path,
            [
                "--is-storage-node",
                "--clone-backend",
                "cow-daemon",
                "--daemon-url",
                "http://localhost:8081",
                "--daemon-api-key",
                "key",
                "--nfs-mount",
                "/mnt/nfs",
            ],
        )
        assert "Storage node" in combined
        assert "Would validate NFS mount" not in combined
        assert "CoW daemon" in combined
