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
from typing import List, Set
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MANIFEST_NAME = ".cidx-ssh-keys.json"

# Bug #1521: the manifest is a SINGLE file shared by every server process
# pointed at one ssh directory, so its entries are namespaced by a stable
# backend identity (``{"version": 2, "backends": {<id>: [names]}}``) instead of
# the old flat ``{"keys": [...]}`` list.  Production backends derive that
# identity from their own locator (SQLite db path / PostgreSQL DSN); a
# MagicMock has none, which by design disables stale-key cleanup -- so these
# tests state the identity outright.
TEST_BACKEND_IDENTITY = "test-backend-identity"


def _write_manifest(ssh_dir: Path, names: List[str]) -> None:
    """Seed a v2 manifest claiming the given names for the test backend."""
    ssh_dir.mkdir(parents=True, exist_ok=True)
    (ssh_dir / MANIFEST_NAME).write_text(
        json.dumps({"version": 2, "backends": {TEST_BACKEND_IDENTITY: sorted(names)}})
    )


def _managed_names(ssh_dir: Path) -> Set[str]:
    """Read back the names the test backend currently claims to manage."""
    data = json.loads((ssh_dir / MANIFEST_NAME).read_text())
    return set(data["backends"].get(TEST_BACKEND_IDENTITY, []))


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

    return SSHKeySyncService(
        ssh_keys_backend=backend,
        ssh_dir=str(ssh_dir),
        backend_identity=TEST_BACKEND_IDENTITY,
    )


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
        # Manifest says THIS backend manages "old_key" but the backend no
        # longer has it -- the legitimate stale-cleanup case.
        _write_manifest(tmp_path, ["old_key"])
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
        _write_manifest(tmp_path, ["old_key"])

        backend = _make_backend([])
        svc = _make_service(backend, tmp_path)
        svc.sync()

        assert "old_key" not in _managed_names(tmp_path)


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

        assert _managed_names(tmp_path) == {"key_x", "key_y"}

    def test_manifest_updated_when_key_added(self, tmp_path: Path) -> None:
        # First sync: one key
        backend = _make_backend([_key_data("key_a")])
        svc = _make_service(backend, tmp_path)
        svc.sync()

        # Second sync: two keys
        backend.list_keys.return_value = [_key_data("key_a"), _key_data("key_b")]
        svc.sync()

        assert _managed_names(tmp_path) == {"key_a", "key_b"}

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


# ---------------------------------------------------------------------------
# Tests: safety guard against real ~/.ssh under pytest (real incident,
# 2026-08-02 -- a test that forgot to override ssh_dir let sync() delete
# three genuine private keys from the developer's actual home directory)
# ---------------------------------------------------------------------------


class TestManifestProvenanceBug1519:
    """Regression tests for Bug #1519.

    SSHKeySyncService.sync() previously recorded a backend-reported key name
    in the manifest as "managed by us" unconditionally (``_update_manifest
    (backend_names)``), even when the write for that name was skipped
    because a same-named file already existed on disk. A later backend
    rename/retirement of that name then caused the stale-key cleanup step to
    unlink() a file this service never actually wrote -- confirmed in a real
    production incident (2026-08-03) that deleted a personal
    ``~/.ssh/id_ed25519`` keypair.
    """

    def test_skipped_write_due_to_name_collision_is_not_recorded_as_managed(
        self, tmp_path: Path
    ) -> None:
        # A pre-existing, unrelated personal key file that happens to share
        # a filename with a backend-tracked key.
        (tmp_path / "id_ed25519").write_text("PERSONAL_PRIVATE_KEY_CONTENT")
        (tmp_path / "id_ed25519.pub").write_text("ssh-ed25519 PERSONAL_PUBLIC")

        backend = _make_backend([_key_data("id_ed25519")])
        svc = _make_service(backend, tmp_path)

        result = svc.sync()

        # The write is correctly skipped (file already exists)...
        assert "id_ed25519" not in result["written"]
        assert "id_ed25519" in result["unchanged"]

        # ...and critically, the manifest must NOT claim we manage it --
        # this service never proved it wrote this file.
        assert "id_ed25519" not in _managed_names(tmp_path)

    def test_personal_key_survives_backend_rename_after_name_collision(
        self, tmp_path: Path
    ) -> None:
        """Full incident reproduction (issue #1519):

        1. A personal id_ed25519 key exists, unrelated to CIDX.
        2. A backend-tracked key of the same name registers; sync() skips
           the write because the file already exists.
        3. The backend key is later renamed/retired (no longer reported).
        4. A second sync() must NOT delete the personal key -- this service
           never provably wrote it.
        """
        original_private = "PERSONAL_PRIVATE_KEY_CONTENT"
        original_public = "ssh-ed25519 PERSONAL_PUBLIC"
        (tmp_path / "id_ed25519").write_text(original_private)
        (tmp_path / "id_ed25519.pub").write_text(original_public)

        # Step 2: backend reports a same-named key; sync() skips the write.
        backend = _make_backend([_key_data("id_ed25519")])
        svc = _make_service(backend, tmp_path)
        svc.sync()

        # Step 3: the backend key is renamed/retired -- no longer reported.
        backend.list_keys.return_value = []

        # Step 4: second sync() must NOT delete the personal key.
        result = svc.sync()

        assert (tmp_path / "id_ed25519").exists()
        assert (tmp_path / "id_ed25519.pub").exists()
        assert (tmp_path / "id_ed25519").read_text() == original_private
        assert (tmp_path / "id_ed25519.pub").read_text() == original_public
        assert "id_ed25519" not in result["removed"]

    def test_genuinely_written_key_still_cleaned_up_when_backend_drops_it(
        self, tmp_path: Path
    ) -> None:
        """Opposite case -- no regression: a key this service DID legitimately
        write (fresh backend key, no local file conflict) must still be
        correctly recorded as managed and cleaned up as stale once the
        backend later drops it."""
        backend = _make_backend([_key_data("fresh_key")])
        svc = _make_service(backend, tmp_path)

        first = svc.sync()
        assert "fresh_key" in first["written"]

        assert "fresh_key" in _managed_names(tmp_path)

        # Backend drops the key.
        backend.list_keys.return_value = []
        second = svc.sync()

        assert "fresh_key" in second["removed"]
        assert not (tmp_path / "fresh_key").exists()
        assert not (tmp_path / "fresh_key.pub").exists()


class TestRealHomeUnderPytestGuard:
    """sync() must refuse to touch the real, unoverridden ~/.ssh while a
    test process is active, regardless of what the backend reports. This
    guard can never affect a legitimate test, since every legitimate test
    passes an explicit tmp_path-based ssh_dir -- it exists solely to make
    an accidentally-real ssh_dir a loud no-op instead of a silent deletion
    of the developer's actual keys."""

    def test_sync_refuses_when_ssh_dir_is_real_home_under_pytest(self) -> None:
        from code_indexer.server.services.ssh_key_sync_service import (
            SSHKeySyncService,
        )

        # Backend reports NO keys -- exactly the shape that triggered the
        # real incident (an empty/fake test backend reconciling against a
        # manifest of previously-real keys would delete everything).
        backend = _make_backend([])
        # Deliberately DO NOT override ssh_dir -- this reproduces the bug:
        # relying on the class default, which resolves to the real ~/.ssh.
        svc = SSHKeySyncService(ssh_keys_backend=backend)

        # PYTEST_CURRENT_TEST is set by pytest itself for the duration of
        # every test -- no monkeypatching needed, this test genuinely runs
        # under pytest right now.
        assert "PYTEST_CURRENT_TEST" in os.environ

        result = svc.sync()

        assert result["written"] == []
        assert result["removed"] == []
        assert result["unchanged"] == []
        assert any("refus" in e.lower() for e in result["errors"])
        # The backend must never even be consulted -- the guard fires
        # before any read/write/delete against the real filesystem.
        backend.list_keys.assert_not_called()

    def test_sync_proceeds_normally_when_ssh_dir_is_tmp_path_under_pytest(
        self, tmp_path: Path
    ) -> None:
        """Every existing, legitimate test in this file uses tmp_path --
        confirm the guard is a true no-op for that case (it must not
        regress any of the tests above)."""
        backend = _make_backend([_key_data("akey")])
        svc = _make_service(backend, tmp_path)

        result = svc.sync()

        assert result["written"] == ["akey"]
        assert result["errors"] == []
        assert (tmp_path / "akey").exists()
