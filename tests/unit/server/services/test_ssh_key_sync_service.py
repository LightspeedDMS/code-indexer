"""
Unit tests for SSHKeySyncService.

Story #428: SSH Key Cluster Sync Service

Tests:
- sync() writes missing key files to disk
- sync() skips keys that already exist on disk
- sync() removes stale keys (managed by service but no longer in backend)
- manifest is read at start of sync and updated at end
- file permissions: private keys get 0o600, public keys get 0o644
- backend errors are surfaced in the returned errors list
- manifest is unreadable -> treated as empty (no crash)
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_backend(keys: list) -> MagicMock:
    """Return a mock backend whose list_keys() returns the given list."""
    backend = MagicMock()
    backend.list_keys.return_value = keys
    return backend


def _key_data(
    name: str,
    private_key: str = "PRIVATE_KEY_CONTENT",
    public_key: str = "ssh-ed25519 AAAA comment",
    hosts: list | None = None,
) -> dict:
    return {
        "name": name,
        "private_key": private_key,
        "public_key": public_key,
        "fingerprint": f"SHA256:fake_{name}",
        "key_type": "ed25519",
        "hosts": list(hosts) if hosts else [],
    }


def _make_service(backend, ssh_dir: Path):
    from code_indexer.server.services.ssh_key_sync_service import SSHKeySyncService

    return SSHKeySyncService(ssh_keys_backend=backend, ssh_dir=str(ssh_dir))


# ---------------------------------------------------------------------------
# Tests: sync writes missing key files
# ---------------------------------------------------------------------------


class TestSyncWritesMissingKeys:
    def test_private_key_file_created(self, tmp_path: Path) -> None:
        backend = _make_backend([_key_data("deploy_key")])
        svc = _make_service(backend, tmp_path)

        result = svc.sync()

        assert (tmp_path / "deploy_key").exists()
        assert result["written"] == ["deploy_key"]
        assert result["errors"] == []

    def test_public_key_file_created(self, tmp_path: Path) -> None:
        backend = _make_backend([_key_data("deploy_key")])
        svc = _make_service(backend, tmp_path)

        svc.sync()

        assert (tmp_path / "deploy_key.pub").exists()

    def test_private_key_content_correct(self, tmp_path: Path) -> None:
        backend = _make_backend([_key_data("mykey", private_key="SECRET_PRIVATE")])
        svc = _make_service(backend, tmp_path)

        svc.sync()

        assert (tmp_path / "mykey").read_text() == "SECRET_PRIVATE"

    def test_public_key_content_correct(self, tmp_path: Path) -> None:
        backend = _make_backend([_key_data("mykey", public_key="ssh-ed25519 AABBCC")])
        svc = _make_service(backend, tmp_path)

        svc.sync()

        assert (tmp_path / "mykey.pub").read_text() == "ssh-ed25519 AABBCC"

    def test_multiple_keys_all_written(self, tmp_path: Path) -> None:
        keys = [_key_data("key_a"), _key_data("key_b"), _key_data("key_c")]
        backend = _make_backend(keys)
        svc = _make_service(backend, tmp_path)

        result = svc.sync()

        assert set(result["written"]) == {"key_a", "key_b", "key_c"}
        for name in ("key_a", "key_b", "key_c"):
            assert (tmp_path / name).exists()
            assert (tmp_path / f"{name}.pub").exists()


# ---------------------------------------------------------------------------
# Tests: sync skips existing key files
# ---------------------------------------------------------------------------


class TestSyncSkipsExistingKeys:
    def test_existing_files_not_in_written(self, tmp_path: Path) -> None:
        # Pre-create both files
        (tmp_path / "mykey").write_text("OLD_PRIVATE")
        (tmp_path / "mykey.pub").write_text("OLD_PUBLIC")

        backend = _make_backend([_key_data("mykey")])
        svc = _make_service(backend, tmp_path)

        result = svc.sync()

        assert "mykey" not in result["written"]
        assert "mykey" in result["unchanged"]

    def test_existing_files_content_preserved(self, tmp_path: Path) -> None:
        (tmp_path / "mykey").write_text("ORIGINAL_PRIVATE")
        (tmp_path / "mykey.pub").write_text("ORIGINAL_PUBLIC")

        backend = _make_backend(
            [_key_data("mykey", private_key="NEW_PRIVATE", public_key="NEW_PUBLIC")]
        )
        svc = _make_service(backend, tmp_path)
        svc.sync()

        # Files should NOT be overwritten (write only happens when files are absent)
        assert (tmp_path / "mykey").read_text() == "ORIGINAL_PRIVATE"
        assert (tmp_path / "mykey.pub").read_text() == "ORIGINAL_PUBLIC"


# ---------------------------------------------------------------------------
# Tests: sync removes stale keys
# ---------------------------------------------------------------------------


class TestSyncRemovesStaleKeys:
    def test_stale_private_key_removed(self, tmp_path: Path) -> None:
        # Manifest says we manage "old_key" but backend no longer has it
        manifest = {"keys": ["old_key"]}
        (tmp_path / ".cidx-ssh-keys.json").write_text(json.dumps(manifest))
        (tmp_path / "old_key").write_text("STALE_PRIVATE")
        (tmp_path / "old_key.pub").write_text("STALE_PUBLIC")

        backend = _make_backend([])  # backend is now empty
        svc = _make_service(backend, tmp_path)
        result = svc.sync()

        assert "old_key" in result["removed"]
        assert not (tmp_path / "old_key").exists()
        assert not (tmp_path / "old_key.pub").exists()

    def test_unmanaged_keys_not_removed(self, tmp_path: Path) -> None:
        # A key that exists on disk but NOT in manifest should never be removed
        (tmp_path / "user_own_key").write_text("USER_PRIVATE")
        (tmp_path / "user_own_key.pub").write_text("USER_PUBLIC")

        backend = _make_backend([])  # manifest is empty (no manifest file)
        svc = _make_service(backend, tmp_path)
        svc.sync()

        assert (tmp_path / "user_own_key").exists()

    def test_stale_key_removed_from_manifest(self, tmp_path: Path) -> None:
        manifest = {"keys": ["old_key"]}
        (tmp_path / ".cidx-ssh-keys.json").write_text(json.dumps(manifest))

        backend = _make_backend([])
        svc = _make_service(backend, tmp_path)
        svc.sync()

        updated = json.loads((tmp_path / ".cidx-ssh-keys.json").read_text())
        assert "old_key" not in updated["keys"]


# ---------------------------------------------------------------------------
# Tests: manifest tracking
# ---------------------------------------------------------------------------


class TestManifestTracking:
    def test_manifest_created_after_sync(self, tmp_path: Path) -> None:
        backend = _make_backend([_key_data("new_key")])
        svc = _make_service(backend, tmp_path)
        svc.sync()

        assert (tmp_path / ".cidx-ssh-keys.json").exists()

    def test_manifest_contains_synced_key_names(self, tmp_path: Path) -> None:
        backend = _make_backend([_key_data("key_x"), _key_data("key_y")])
        svc = _make_service(backend, tmp_path)
        svc.sync()

        data = json.loads((tmp_path / ".cidx-ssh-keys.json").read_text())
        assert set(data["keys"]) == {"key_x", "key_y"}

    def test_manifest_updated_when_key_added(self, tmp_path: Path) -> None:
        # First sync: one key
        backend = _make_backend([_key_data("key_a")])
        svc = _make_service(backend, tmp_path)
        svc.sync()

        # Second sync: two keys
        backend.list_keys.return_value = [_key_data("key_a"), _key_data("key_b")]
        svc.sync()

        data = json.loads((tmp_path / ".cidx-ssh-keys.json").read_text())
        assert set(data["keys"]) == {"key_a", "key_b"}

    def test_corrupted_manifest_treated_as_empty(self, tmp_path: Path) -> None:
        # Write garbage JSON
        (tmp_path / ".cidx-ssh-keys.json").write_text("not valid json {{{")

        backend = _make_backend([_key_data("new_key")])
        svc = _make_service(backend, tmp_path)
        # Should not raise
        result = svc.sync()

        assert result["errors"] == []
        assert "new_key" in result["written"]


# ---------------------------------------------------------------------------
# Tests: ~/.ssh/config Host-mapping materialization (cluster auth fix)
#
# Root cause (staging): sync() materialized key FILES on every node but never
# wrote the ~/.ssh/config Host->IdentityFile mapping, so worker-leader nodes
# could not select cidx_github_key for `ssh git@github.com` -> the cidx-meta
# backup push/fetch failed with Permission denied (publickey).
# ---------------------------------------------------------------------------


class TestSyncWritesSshConfig:
    @staticmethod
    def _config_path(ssh_dir: Path) -> Path:
        return ssh_dir / "config"

    def test_host_block_written_for_assigned_host(self, tmp_path: Path) -> None:
        backend = _make_backend([_key_data("cidx_github_key", hosts=["github.com"])])
        svc = _make_service(backend, tmp_path)

        result = svc.sync()

        config = self._config_path(tmp_path).read_text()
        assert "Host github.com" in config
        assert "HostName github.com" in config
        assert f"IdentityFile {tmp_path / 'cidx_github_key'}" in config
        assert "IdentitiesOnly yes" in config
        assert result["errors"] == []

    def test_identityfile_points_at_locally_synced_key(self, tmp_path: Path) -> None:
        # IdentityFile MUST reference the path THIS node wrote the key to,
        # never the originating node's private_path (which differs per node).
        key = _key_data("cidx_github_key", hosts=["github.com"])
        key["private_path"] = "/some/other/node/home/.ssh/cidx_github_key"
        backend = _make_backend([key])
        svc = _make_service(backend, tmp_path)

        svc.sync()

        config = self._config_path(tmp_path).read_text()
        assert f"IdentityFile {tmp_path / 'cidx_github_key'}" in config
        assert "/some/other/node/home/.ssh/cidx_github_key" not in config

    def test_multiple_hosts_each_get_block(self, tmp_path: Path) -> None:
        backend = _make_backend(
            [_key_data("multi", hosts=["github.com", "gitlab.com"])]
        )
        svc = _make_service(backend, tmp_path)
        svc.sync()

        config = self._config_path(tmp_path).read_text()
        assert "Host github.com" in config
        assert "Host gitlab.com" in config

    def test_no_hosts_writes_no_host_block(self, tmp_path: Path) -> None:
        backend = _make_backend([_key_data("hostless", hosts=[])])
        svc = _make_service(backend, tmp_path)
        svc.sync()

        config_path = self._config_path(tmp_path)
        # No Host block should be emitted; if a config exists it must be marker-only.
        if config_path.exists():
            assert "Host " not in config_path.read_text()

    def test_user_config_section_preserved(self, tmp_path: Path) -> None:
        config_path = self._config_path(tmp_path)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            "Host my-personal-server\n  HostName 10.0.0.5\n  User myuser\n"
        )

        backend = _make_backend([_key_data("cidx_github_key", hosts=["github.com"])])
        svc = _make_service(backend, tmp_path)
        svc.sync()

        config = config_path.read_text()
        assert "Host my-personal-server" in config
        assert "HostName 10.0.0.5" in config
        assert "Host github.com" in config

    def test_config_permissions_are_600(self, tmp_path: Path) -> None:
        backend = _make_backend([_key_data("cidx_github_key", hosts=["github.com"])])
        svc = _make_service(backend, tmp_path)
        svc.sync()

        mode = stat.S_IMODE(os.stat(self._config_path(tmp_path)).st_mode)
        assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"

    def test_sync_idempotent_on_config(self, tmp_path: Path) -> None:
        backend = _make_backend([_key_data("cidx_github_key", hosts=["github.com"])])
        svc = _make_service(backend, tmp_path)
        svc.sync()
        first = self._config_path(tmp_path).read_text()
        # Key files now exist; second sync is a no-op for files but must
        # still keep the config block intact (and not duplicate it).
        svc.sync()
        second = self._config_path(tmp_path).read_text()

        assert second.count("Host github.com") == 1
        assert first == second

    def test_config_write_failure_surfaced_in_errors(self, tmp_path: Path) -> None:
        backend = _make_backend([_key_data("cidx_github_key", hosts=["github.com"])])
        svc = _make_service(backend, tmp_path)

        # Force the config write to fail; key-file materialization already
        # succeeded, so the failure must be captured, not raised.
        from unittest.mock import patch

        with patch.object(
            svc._config_manager,
            "write_config",
            side_effect=PermissionError("read-only config"),
        ):
            result = svc.sync()

        assert any(e.startswith("ssh-config:") for e in result["errors"])
        assert "read-only config" in " ".join(result["errors"])
        # Key files were still written despite the config failure.
        assert (tmp_path / "cidx_github_key").exists()

    def test_config_block_removed_when_host_unassigned(self, tmp_path: Path) -> None:
        backend = _make_backend([_key_data("cidx_github_key", hosts=["github.com"])])
        svc = _make_service(backend, tmp_path)
        svc.sync()
        assert "Host github.com" in self._config_path(tmp_path).read_text()

        # Host removed from the key in the backend -> block must disappear.
        backend.list_keys.return_value = [_key_data("cidx_github_key", hosts=[])]
        svc.sync()
        assert "Host github.com" not in self._config_path(tmp_path).read_text()


# ---------------------------------------------------------------------------
# Tests: file permissions
# ---------------------------------------------------------------------------


class TestFilePermissions:
    def test_private_key_permission_is_600(self, tmp_path: Path) -> None:
        backend = _make_backend([_key_data("perm_key")])
        svc = _make_service(backend, tmp_path)
        svc.sync()

        mode = stat.S_IMODE(os.stat(tmp_path / "perm_key").st_mode)
        assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"

    def test_public_key_permission_is_644(self, tmp_path: Path) -> None:
        backend = _make_backend([_key_data("perm_key")])
        svc = _make_service(backend, tmp_path)
        svc.sync()

        mode = stat.S_IMODE(os.stat(tmp_path / "perm_key.pub").st_mode)
        assert mode == 0o644, f"Expected 0o644, got {oct(mode)}"


# ---------------------------------------------------------------------------
# Tests: backend error handling
# ---------------------------------------------------------------------------


class TestBackendErrorHandling:
    def test_backend_exception_surfaced_in_errors(self, tmp_path: Path) -> None:
        backend = MagicMock()
        backend.list_keys.side_effect = RuntimeError("DB connection failed")
        svc = _make_service(backend, tmp_path)

        result = svc.sync()

        assert len(result["errors"]) == 1
        assert "DB connection failed" in result["errors"][0]
        assert result["written"] == []
        assert result["removed"] == []

    def test_partial_key_data_missing_private_key(self, tmp_path: Path) -> None:
        # Key with no private_key — only public key present
        key = {  # type: ignore[var-annotated]
            "name": "pub_only",
            "private_key": None,
            "public_key": "ssh-ed25519 AAAA",
            "fingerprint": "SHA256:fake",
            "key_type": "ed25519",
            "hosts": [],
        }
        backend = _make_backend([key])
        svc = _make_service(backend, tmp_path)
        result = svc.sync()

        # Should write the pub file only
        assert (tmp_path / "pub_only.pub").exists()
        assert not (tmp_path / "pub_only").exists()
        assert result["errors"] == []

    def test_ssh_dir_created_if_missing(self, tmp_path: Path) -> None:
        nested_dir = tmp_path / "some" / "nested" / "dir"
        backend = _make_backend([_key_data("akey")])
        svc = _make_service(backend, nested_dir)

        result = svc.sync()

        assert nested_dir.exists()
        assert result["errors"] == []
