"""
Regression tests for Bug #1519 (secondary hazard).

SSHKeyManager.delete_key() previously had an unconditional unlink() fallback,
with zero ownership/provenance verification, whenever no backend metadata
existed for the requested key_name: it would fall back to
``self.ssh_dir / key_name`` (and ``.pub``) and delete those files if they
happened to exist -- regardless of whether this service ever created them.

Same root flaw as the primary sync() bug: trusting a name match instead of
verified provenance. Reachable via DELETE /api/ssh-keys/{name} or
`cidx keys delete <name>`.

The fix: when no metadata is found for key_name, this service must NEVER
fall back to blindly unlinking a same-named file in self.ssh_dir. The
metadata-present path (deleting the files metadata explicitly points at) is
untouched -- that IS provenance, since the metadata record is how this
service knows it created that specific file.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from code_indexer.server.services.ssh_key_manager import KeyMetadata, SSHKeyManager


# ---------------------------------------------------------------------------
# Helpers: JSON-file backend (default, use_sqlite=False)
# ---------------------------------------------------------------------------


def _make_json_manager(tmp_path: Path) -> SSHKeyManager:
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    metadata_dir = tmp_path / "meta"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    return SSHKeyManager(
        ssh_dir=ssh_dir,
        metadata_dir=metadata_dir,
        config_path=ssh_dir / "config",
        use_sqlite=False,
    )


def _write_json_metadata(manager: SSHKeyManager, metadata: KeyMetadata) -> None:
    manager.metadata_dir.mkdir(parents=True, exist_ok=True)
    (manager.metadata_dir / f"{metadata.name}.json").write_text(
        json.dumps(asdict(metadata))
    )


# ---------------------------------------------------------------------------
# Helpers: SQLite backend (use_sqlite=True)
# ---------------------------------------------------------------------------


def _make_sqlite_manager(tmp_path: Path) -> SSHKeyManager:
    from code_indexer.server.storage.database_manager import DatabaseSchema

    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    metadata_dir = tmp_path / "meta"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "keys.db"
    DatabaseSchema(str(db_path)).initialize_database()

    return SSHKeyManager(
        ssh_dir=ssh_dir,
        metadata_dir=metadata_dir,
        config_path=ssh_dir / "config",
        use_sqlite=True,
        db_path=db_path,
    )


# ---------------------------------------------------------------------------
# (a) Regression: deleting a key WITH real metadata still removes its files
# ---------------------------------------------------------------------------


class TestDeleteKeyWithMetadataRegression:
    def test_json_backend_tracked_key_files_removed(self, tmp_path: Path) -> None:
        manager = _make_json_manager(tmp_path)
        private_path = manager.ssh_dir / "tracked_key"
        public_path = manager.ssh_dir / "tracked_key.pub"
        private_path.write_text("TRACKED_PRIVATE")
        public_path.write_text("TRACKED_PUBLIC")

        metadata = KeyMetadata(
            name="tracked_key",
            fingerprint="SHA256:fake",
            key_type="ed25519",
            private_path=str(private_path),
            public_path=str(public_path),
        )
        _write_json_metadata(manager, metadata)

        result = manager.delete_key("tracked_key")

        assert result is True
        assert not private_path.exists()
        assert not public_path.exists()

    def test_sqlite_backend_tracked_key_files_removed(self, tmp_path: Path) -> None:
        manager = _make_sqlite_manager(tmp_path)
        private_path = manager.ssh_dir / "tracked_key"
        public_path = manager.ssh_dir / "tracked_key.pub"
        private_path.write_text("TRACKED_PRIVATE")
        public_path.write_text("TRACKED_PUBLIC")

        assert manager._sqlite_backend is not None
        manager._sqlite_backend.create_key(
            name="tracked_key",
            fingerprint="SHA256:fake",
            key_type="ed25519",
            private_path=str(private_path),
            public_path=str(public_path),
            public_key="ssh-ed25519 TRACKED_PUBLIC",
            email=None,
            description=None,
            is_imported=False,
        )

        result = manager.delete_key("tracked_key")

        assert result is True
        assert not private_path.exists()
        assert not public_path.exists()


# ---------------------------------------------------------------------------
# (b) Bug #1519 fix: no metadata must never trigger a blind unlink()
# ---------------------------------------------------------------------------


class TestDeleteKeyWithoutMetadataProvenanceGuard:
    def test_json_backend_untracked_same_named_file_is_not_deleted(
        self, tmp_path: Path
    ) -> None:
        manager = _make_json_manager(tmp_path)
        untracked_private = manager.ssh_dir / "id_ed25519"
        untracked_public = manager.ssh_dir / "id_ed25519.pub"
        untracked_private.write_text("PERSONAL_PRIVATE_KEY_CONTENT")
        untracked_public.write_text("ssh-ed25519 PERSONAL_PUBLIC")

        # No metadata was ever created for "id_ed25519" -- this service has
        # no record of ever writing it.
        result = manager.delete_key("id_ed25519")

        # The file must survive -- this service never proved it wrote it.
        assert untracked_private.exists()
        assert untracked_public.exists()
        assert untracked_private.read_text() == "PERSONAL_PRIVATE_KEY_CONTENT"
        assert untracked_public.read_text() == "ssh-ed25519 PERSONAL_PUBLIC"

        # Loud failure signal, distinguishing this from a genuine idempotent
        # no-op (deleting a key that never existed anywhere).
        assert result is False

    def test_sqlite_backend_untracked_same_named_file_is_not_deleted(
        self, tmp_path: Path
    ) -> None:
        manager = _make_sqlite_manager(tmp_path)
        untracked_private = manager.ssh_dir / "id_ed25519"
        untracked_public = manager.ssh_dir / "id_ed25519.pub"
        untracked_private.write_text("PERSONAL_PRIVATE_KEY_CONTENT")
        untracked_public.write_text("ssh-ed25519 PERSONAL_PUBLIC")

        result = manager.delete_key("id_ed25519")

        assert untracked_private.exists()
        assert untracked_public.exists()
        assert untracked_private.read_text() == "PERSONAL_PRIVATE_KEY_CONTENT"
        assert untracked_public.read_text() == "ssh-ed25519 PERSONAL_PUBLIC"
        assert result is False

    def test_json_backend_truly_nonexistent_key_is_idempotent_no_op(
        self, tmp_path: Path
    ) -> None:
        """No metadata AND no same-named file on disk -- a genuine no-op
        delete of a key that never existed. Must remain a silent success,
        preserving the pre-existing idempotent-delete contract."""
        manager = _make_json_manager(tmp_path)

        result = manager.delete_key("never_existed")

        assert result is True

    def test_sqlite_backend_truly_nonexistent_key_is_idempotent_no_op(
        self, tmp_path: Path
    ) -> None:
        manager = _make_sqlite_manager(tmp_path)

        result = manager.delete_key("never_existed")

        assert result is True
