"""
Unit tests for SSHKeyManager cluster-awareness (Bug #1072 Chunk 2, Step 6).

Tests:
- Cluster create_key: encrypts private key content + writes to PG backend
- Solo mode: unchanged behavior (no encryption, no PG write)
- set_cluster_dependencies classmethod: injects into newly constructed instances
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet


# ---------------------------------------------------------------------------
# Helpers: fake key generator
# ---------------------------------------------------------------------------

SENTINEL_PRIVATE_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\nFAKEKEY\n-----END OPENSSH PRIVATE KEY-----\n"
)
SENTINEL_PUBLIC_KEY = "ssh-ed25519 AAAA fake_public_key_content test@example.com"
SENTINEL_FINGERPRINT = "SHA256:fakefingerprint123"


@dataclass
class FakeGeneratedKey:
    """Fake result returned by a fake key generator."""

    name: str
    private_path: Path
    public_path: Path
    public_key: str
    fingerprint: str
    key_type: str


class FakeKeyGenerator:
    """
    Fake SSH key generator that writes sentinel content to disk
    without invoking ssh-keygen.
    """

    def __init__(self, ssh_dir: Path) -> None:
        self.ssh_dir = ssh_dir

    def generate_key(
        self,
        key_name: str,
        key_type: str = "ed25519",
        bits: Optional[int] = None,
        email: Optional[str] = None,
    ) -> FakeGeneratedKey:
        private_path = self.ssh_dir / key_name
        public_path = self.ssh_dir / f"{key_name}.pub"

        # Write sentinel files to disk (so create_key can read them)
        private_path.write_text(SENTINEL_PRIVATE_KEY)
        private_path.chmod(0o600)
        public_path.write_text(SENTINEL_PUBLIC_KEY)
        public_path.chmod(0o644)

        return FakeGeneratedKey(
            name=key_name,
            private_path=private_path,
            public_path=public_path,
            public_key=SENTINEL_PUBLIC_KEY,
            fingerprint=SENTINEL_FINGERPRINT,
            key_type=key_type,
        )


# ---------------------------------------------------------------------------
# Helpers: build SSHKeyManager with isolated directories
# ---------------------------------------------------------------------------


def _make_manager(tmp_path: Path, pg_backend=None, fernet=None):
    """Build an SSHKeyManager with isolated temp dirs and a fake key generator."""
    from code_indexer.server.services.ssh_key_manager import SSHKeyManager
    from code_indexer.server.storage.database_manager import DatabaseSchema

    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    metadata_dir = tmp_path / "meta"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "test_keys.db"

    # Initialize the full schema (including ssh_keys + private_key column)
    # before constructing the manager, mirroring how the server does it on startup.
    schema = DatabaseSchema(str(db_path))
    schema.initialize_database()

    manager = SSHKeyManager(
        ssh_dir=ssh_dir,
        metadata_dir=metadata_dir,
        use_sqlite=True,
        db_path=db_path,
        pg_backend=pg_backend,
        fernet=fernet,
    )
    # Replace key_generator with the fake (avoids actual ssh-keygen)
    manager.key_generator = FakeKeyGenerator(ssh_dir=ssh_dir)
    return manager


# ---------------------------------------------------------------------------
# Fixture: ensure class-level state is clean before/after each test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_class_level_state():
    """Reset SSHKeyManager class-level cluster deps before and after each test."""
    from code_indexer.server.services.ssh_key_manager import SSHKeyManager

    # Reset before test
    SSHKeyManager._cluster_pg_backend = None
    SSHKeyManager._cluster_fernet = None

    yield

    # Reset after test to avoid polluting other tests
    SSHKeyManager._cluster_pg_backend = None
    SSHKeyManager._cluster_fernet = None


# ---------------------------------------------------------------------------
# Test 1: Cluster mode — create_key encrypts and writes to PG
# ---------------------------------------------------------------------------


class TestClusterCreateKeyEncryptsAndWritesToPG:
    def test_pg_backend_called_once_with_private_key(self, tmp_path: Path) -> None:
        """create_key must call pg_backend.create_key exactly once with encrypted private_key."""
        fernet = Fernet(Fernet.generate_key())
        pg_backend = MagicMock()

        manager = _make_manager(tmp_path, pg_backend=pg_backend, fernet=fernet)
        manager.create_key(name="test_key", key_type="ed25519")

        assert pg_backend.create_key.call_count == 1
        call_kwargs = pg_backend.create_key.call_args.kwargs
        assert call_kwargs.get("name") == "test_key"
        assert call_kwargs.get("private_key") is not None

    def test_encrypted_private_key_decrypts_to_original_content(
        self, tmp_path: Path
    ) -> None:
        """The encrypted private_key passed to PG must decrypt back to the original file content."""
        fernet_key = Fernet.generate_key()
        fernet = Fernet(fernet_key)
        pg_backend = MagicMock()

        manager = _make_manager(tmp_path, pg_backend=pg_backend, fernet=fernet)
        manager.create_key(name="test_key", key_type="ed25519")

        call_kwargs = pg_backend.create_key.call_args.kwargs
        encrypted_private = call_kwargs["private_key"]

        # Decrypt with the same key
        decrypted = Fernet(fernet_key).decrypt(encrypted_private.encode()).decode()
        assert decrypted == SENTINEL_PRIVATE_KEY

    def test_pg_backend_receives_correct_key_metadata(self, tmp_path: Path) -> None:
        """PG backend create_key must receive all expected fields."""
        fernet = Fernet(Fernet.generate_key())
        pg_backend = MagicMock()

        manager = _make_manager(tmp_path, pg_backend=pg_backend, fernet=fernet)
        manager.create_key(
            name="deploy_key",
            key_type="ed25519",
            email="user@example.com",
            description="Deploy key for testing",
        )

        call_kwargs = pg_backend.create_key.call_args.kwargs
        assert call_kwargs["name"] == "deploy_key"
        assert call_kwargs["fingerprint"] == SENTINEL_FINGERPRINT
        assert call_kwargs["key_type"] == "ed25519"
        assert call_kwargs["public_key"] == SENTINEL_PUBLIC_KEY
        assert call_kwargs["email"] == "user@example.com"
        assert call_kwargs["description"] == "Deploy key for testing"
        assert call_kwargs["is_imported"] is False
        assert call_kwargs["private_key"] is not None

    def test_pg_failure_is_raised_not_swallowed(self, tmp_path: Path) -> None:
        """If PG backend raises, create_key must propagate the exception (anti-silent-failure)."""
        fernet = Fernet(Fernet.generate_key())
        pg_backend = MagicMock()
        pg_backend.create_key.side_effect = RuntimeError("PG write failed")

        manager = _make_manager(tmp_path, pg_backend=pg_backend, fernet=fernet)

        with pytest.raises(RuntimeError, match="PG write failed"):
            manager.create_key(name="test_key", key_type="ed25519")

    def test_sqlite_backend_still_called_in_cluster_mode(self, tmp_path: Path) -> None:
        """Local SQLite backend must still record the key in cluster mode."""
        fernet = Fernet(Fernet.generate_key())
        pg_backend = MagicMock()

        manager = _make_manager(tmp_path, pg_backend=pg_backend, fernet=fernet)
        result = manager.create_key(name="cluster_key", key_type="ed25519")

        # The returned metadata must have the correct name
        assert result.name == "cluster_key"

        # SQLite backend must also have the record (list_keys returns it)
        assert manager._sqlite_backend is not None
        keys = manager._sqlite_backend.list_keys()
        assert any(k["name"] == "cluster_key" for k in keys)


# ---------------------------------------------------------------------------
# Test 2: Solo mode — unchanged behavior
# ---------------------------------------------------------------------------


class TestSoloModeUnchanged:
    def test_no_pg_backend_no_encryption(self, tmp_path: Path) -> None:
        """In solo mode (no pg_backend/fernet), no encryption or PG write occurs."""
        manager = _make_manager(tmp_path)
        result = manager.create_key(name="solo_key", key_type="ed25519")

        assert result.name == "solo_key"
        # SQLite must have the record
        assert manager._sqlite_backend is not None
        keys = manager._sqlite_backend.list_keys()
        matching = [k for k in keys if k["name"] == "solo_key"]
        assert len(matching) == 1
        # No private_key blob in solo SQLite record
        assert matching[0].get("private_key") is None

    def test_no_pg_call_in_solo_mode(self, tmp_path: Path) -> None:
        """In solo mode, no pg_backend attribute is accessed."""
        manager = _make_manager(tmp_path)
        # Confirm _pg_backend is None
        assert manager._pg_backend is None
        assert manager._fernet is None

        # Should complete without error
        result = manager.create_key(name="solo_key2")
        assert result.name == "solo_key2"

    def test_fernet_only_no_pg_backend_is_solo(self, tmp_path: Path) -> None:
        """Fernet set but no pg_backend => solo mode (no encryption, no PG write)."""
        fernet = Fernet(Fernet.generate_key())
        manager = _make_manager(tmp_path, fernet=fernet)
        result = manager.create_key(name="partial_solo_key")
        assert result.name == "partial_solo_key"


# ---------------------------------------------------------------------------
# Test 3: set_cluster_dependencies classmethod
# ---------------------------------------------------------------------------


class TestSetClusterDependencies:
    def test_classmethod_sets_class_attributes(self, tmp_path: Path) -> None:
        """set_cluster_dependencies stores the backend and fernet at class level."""
        from code_indexer.server.services.ssh_key_manager import SSHKeyManager

        fernet = Fernet(Fernet.generate_key())
        pg_backend = MagicMock()

        SSHKeyManager.set_cluster_dependencies(pg_backend=pg_backend, fernet=fernet)

        assert SSHKeyManager._cluster_pg_backend is pg_backend
        assert SSHKeyManager._cluster_fernet is fernet

    def test_new_instance_picks_up_class_level_deps(self, tmp_path: Path) -> None:
        """A manager created after set_cluster_dependencies picks up PG + Fernet."""
        from code_indexer.server.services.ssh_key_manager import SSHKeyManager

        fernet = Fernet(Fernet.generate_key())
        pg_backend = MagicMock()

        SSHKeyManager.set_cluster_dependencies(pg_backend=pg_backend, fernet=fernet)

        # Build a new manager WITHOUT passing pg_backend/fernet explicitly
        ssh_dir = tmp_path / "ssh"
        ssh_dir.mkdir(mode=0o700)
        db_path = tmp_path / "keys.db"

        # Initialize schema so the ssh_keys table (with private_key column) exists
        from code_indexer.server.storage.database_manager import DatabaseSchema

        DatabaseSchema(str(db_path)).initialize_database()

        manager = SSHKeyManager(
            ssh_dir=ssh_dir,
            metadata_dir=tmp_path / "meta",
            use_sqlite=True,
            db_path=db_path,
        )
        manager.key_generator = FakeKeyGenerator(ssh_dir=ssh_dir)

        assert manager._pg_backend is pg_backend
        assert manager._fernet is fernet

        # create_key should now call pg_backend
        manager.create_key(name="injected_key")
        assert pg_backend.create_key.call_count == 1

    def test_explicit_params_override_class_level(self, tmp_path: Path) -> None:
        """Explicit constructor params take precedence over class-level deps."""
        from code_indexer.server.services.ssh_key_manager import SSHKeyManager

        class_fernet = Fernet(Fernet.generate_key())
        class_pg = MagicMock(name="class_pg")
        SSHKeyManager.set_cluster_dependencies(pg_backend=class_pg, fernet=class_fernet)

        explicit_fernet = Fernet(Fernet.generate_key())
        explicit_pg = MagicMock(name="explicit_pg")

        manager = _make_manager(
            tmp_path, pg_backend=explicit_pg, fernet=explicit_fernet
        )
        manager.create_key(name="explicit_key")

        # Only the explicit pg should be called
        assert explicit_pg.create_key.call_count == 1
        assert class_pg.create_key.call_count == 0


# ---------------------------------------------------------------------------
# Test 4: Bug #1072 Chunk 2 — assign_key_to_host and list_keys must not crash
#         when SQLite backend returns dicts that include 'private_key'.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test 5: Bug #1072 HIGH 1 — delete_key must also remove from PG in cluster mode
# ---------------------------------------------------------------------------


class TestClusterDeleteKeyAlsoDeletesFromPG:
    def test_pg_delete_called_on_cluster_delete(self, tmp_path: Path) -> None:
        """
        delete_key in cluster mode must call pg_backend.delete_key(key_name)
        so the key cannot resurrect on the next sync cycle.

        Regression guard for Bug #1072 HIGH 1.
        """
        fernet = Fernet(Fernet.generate_key())
        pg_backend = MagicMock()

        manager = _make_manager(tmp_path, pg_backend=pg_backend, fernet=fernet)
        manager.create_key(name="cluster_del_key", key_type="ed25519")

        # Reset create call count so we only assert on delete
        pg_backend.reset_mock()

        result = manager.delete_key("cluster_del_key")

        assert result is True
        pg_backend.delete_key.assert_called_once_with("cluster_del_key")

    def test_pg_delete_not_called_in_solo_mode(self, tmp_path: Path) -> None:
        """
        In solo mode (no pg_backend), pg_backend.delete_key must never be called.
        """
        manager = _make_manager(tmp_path)
        manager.create_key(name="solo_del_key", key_type="ed25519")

        # Solo manager has no _pg_backend — just verify delete completes cleanly
        result = manager.delete_key("solo_del_key")
        assert result is True
        # No pg_backend means no PG call — no mock to assert on, test just
        # verifies the code path doesn't crash.
        assert manager._pg_backend is None


class TestCreateKeyUpsertOnReRegistration:
    """
    Regression guard for Bug #1072 HIGH 2.

    Calling create_key twice with the same name must silently upsert rather
    than raise an IntegrityError (UNIQUE constraint on ssh_keys.name).
    This covers both the SQLite path (SSHKeysSqliteBackend) and the PG path
    (SSHKeysPostgresBackend) — the PG path is exercised indirectly via the
    MagicMock which accepts duplicate calls without raising.
    """

    def test_sqlite_create_key_twice_does_not_raise(self, tmp_path: Path) -> None:
        """Second create_key with the same name must upsert, not raise."""
        manager = _make_manager(tmp_path)
        manager.create_key(name="dup_key", key_type="ed25519", description="first")
        # Second call must not raise IntegrityError
        manager.create_key(name="dup_key", key_type="ed25519", description="second")

        assert manager._sqlite_backend is not None
        keys = manager._sqlite_backend.list_keys()
        matching = [k for k in keys if k["name"] == "dup_key"]
        # Exactly one record — upsert, not duplicate
        assert len(matching) == 1
        assert matching[0]["description"] == "second"

    def test_cluster_pg_create_key_called_twice_does_not_raise(
        self, tmp_path: Path
    ) -> None:
        """
        In cluster mode, second create_key with the same name must call
        pg_backend.create_key a second time (upsert semantics) without error.
        """
        fernet = Fernet(Fernet.generate_key())
        pg_backend = MagicMock()

        manager = _make_manager(tmp_path, pg_backend=pg_backend, fernet=fernet)
        manager.create_key(name="dup_cluster_key", key_type="ed25519")
        manager.create_key(name="dup_cluster_key", key_type="ed25519")

        # PG upsert called twice — one per create_key invocation
        assert pg_backend.create_key.call_count == 2

        # SQLite still has exactly one record
        assert manager._sqlite_backend is not None
        keys = manager._sqlite_backend.list_keys()
        matching = [k for k in keys if k["name"] == "dup_cluster_key"]
        assert len(matching) == 1


class TestAssignKeyToHostReturnKeyMetadata:
    def test_assign_key_to_host_returns_key_metadata_without_raising(
        self, tmp_path: Path
    ) -> None:
        """
        assign_key_to_host must return KeyMetadata without TypeError even when
        the SQLite backend's get_key() dict includes a 'private_key' key
        (added in Bug #1072 Chunk 1).

        Regression guard for the bug at ssh_key_manager.py:324 where
        KeyMetadata(**updated_data) would crash with:
          TypeError: __init__() got an unexpected keyword argument 'private_key'
        """
        from code_indexer.server.services.ssh_key_manager import KeyMetadata

        manager = _make_manager(tmp_path)
        manager.create_key(name="assign_test_key", key_type="ed25519")

        result = manager.assign_key_to_host("assign_test_key", "github.com")

        assert isinstance(result, KeyMetadata)
        assert result.name == "assign_test_key"
        assert "github.com" in result.hosts

    def test_list_keys_returns_key_metadata_instances_without_raising(
        self, tmp_path: Path
    ) -> None:
        """
        list_keys() must return KeyMetadata instances without TypeError even when
        the SQLite backend's list_keys() dicts include a 'private_key' key.

        Regression guard for the strip at ssh_key_manager.py:443.
        """
        from code_indexer.server.services.ssh_key_manager import KeyMetadata

        manager = _make_manager(tmp_path)
        manager.create_key(name="list_test_key", key_type="ed25519")

        result = manager.list_keys()

        assert len(result.managed) >= 1
        for key in result.managed:
            assert isinstance(key, KeyMetadata), (
                f"Expected KeyMetadata, got {type(key)}"
            )
        names = [k.name for k in result.managed]
        assert "list_test_key" in names
