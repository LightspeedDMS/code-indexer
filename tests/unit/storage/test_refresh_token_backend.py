"""Tests for RefreshTokenBackend Protocol and RefreshTokenSqliteBackend (Story #515)."""

import pytest
from pathlib import Path


class TestRefreshTokenBackendProtocol:
    def test_protocol_is_runtime_checkable(self):
        from code_indexer.server.storage.protocols import RefreshTokenBackend

        class NotABackend:
            pass

        try:
            isinstance(NotABackend(), RefreshTokenBackend)
        except TypeError:
            pytest.fail("RefreshTokenBackend is not @runtime_checkable")

    def test_protocol_has_required_methods(self):
        from code_indexer.server.storage.protocols import RefreshTokenBackend

        required = [
            "create_token_family",
            "get_token_family",
            "revoke_token_family",
            "revoke_user_families",
            "update_family_last_used",
            "store_refresh_token",
            "get_refresh_token_by_hash",
            "mark_token_used",
            "count_active_tokens_in_family",
            "delete_expired_tokens",
            "delete_orphaned_families",
            "close",
        ]
        for m in required:
            assert m in dir(RefreshTokenBackend), f"Missing {m}"


class TestRefreshTokenSqliteBackend:
    @pytest.fixture
    def backend(self, tmp_path):
        from code_indexer.server.storage.sqlite_backends import (
            RefreshTokenSqliteBackend,
        )

        b = RefreshTokenSqliteBackend(str(tmp_path / "test_rt.db"))
        yield b
        b.close()

    def test_satisfies_protocol(self, backend):
        from code_indexer.server.storage.protocols import RefreshTokenBackend

        assert isinstance(backend, RefreshTokenBackend)

    def test_create_and_get_family(self, backend):
        backend.create_token_family(
            "fam1", "alice", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z"
        )
        fam = backend.get_token_family("fam1")
        assert fam is not None
        assert fam["family_id"] == "fam1"
        assert fam["username"] == "alice"
        assert fam["is_revoked"] in (0, False)

    def test_get_family_returns_none_for_unknown(self, backend):
        assert backend.get_token_family("nonexistent") is None

    def test_revoke_family(self, backend):
        backend.create_token_family(
            "fam2", "bob", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z"
        )
        backend.revoke_token_family("fam2", "manual")
        fam = backend.get_token_family("fam2")
        assert fam["is_revoked"] in (1, True)
        assert fam["revocation_reason"] == "manual"

    def test_store_and_get_token(self, backend):
        backend.create_token_family(
            "fam3", "carol", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z"
        )
        backend.store_refresh_token(
            "tok1",
            "fam3",
            "carol",
            "hash123",
            "2024-01-01T00:00:00Z",
            "2024-02-01T00:00:00Z",
        )
        tok = backend.get_refresh_token_by_hash("hash123")
        assert tok is not None
        assert tok["token_id"] == "tok1"
        assert tok["family_id"] == "fam3"

    def test_get_token_returns_none_for_unknown_hash(self, backend):
        assert backend.get_refresh_token_by_hash("nope") is None

    def test_mark_token_used(self, backend):
        backend.create_token_family(
            "fam4", "dave", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z"
        )
        backend.store_refresh_token(
            "tok2",
            "fam4",
            "dave",
            "hash456",
            "2024-01-01T00:00:00Z",
            "2024-02-01T00:00:00Z",
        )
        backend.mark_token_used("tok2", "2024-01-15T00:00:00Z")
        tok = backend.get_refresh_token_by_hash("hash456")
        assert tok["is_used"] in (1, True)

    def test_revoke_user_families(self, backend):
        backend.create_token_family(
            "fam5a", "eve", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z"
        )
        backend.create_token_family(
            "fam5b", "eve", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z"
        )
        count = backend.revoke_user_families("eve", "password_change")
        assert count == 2

    def test_delete_expired_tokens(self, backend):
        backend.create_token_family(
            "fam6", "frank", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z"
        )
        backend.store_refresh_token(
            "tok3",
            "fam6",
            "frank",
            "hash789",
            "2024-01-01T00:00:00Z",
            "2024-01-02T00:00:00Z",
        )
        deleted = backend.delete_expired_tokens("2024-06-01T00:00:00Z")
        assert deleted == 1

    def test_count_active_tokens(self, backend):
        backend.create_token_family(
            "fam7", "grace", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z"
        )
        backend.store_refresh_token(
            "tok4",
            "fam7",
            "grace",
            "hashA",
            "2024-01-01T00:00:00Z",
            "2024-12-01T00:00:00Z",
        )
        backend.store_refresh_token(
            "tok5",
            "fam7",
            "grace",
            "hashB",
            "2024-01-01T00:00:00Z",
            "2024-12-01T00:00:00Z",
        )
        assert backend.count_active_tokens_in_family("fam7") == 2
        backend.mark_token_used("tok4", "2024-01-15T00:00:00Z")
        assert backend.count_active_tokens_in_family("fam7") == 1


class TestBackendRegistryRefreshToken:
    def test_registry_has_refresh_tokens_field(self):
        from code_indexer.server.storage.factory import BackendRegistry
        import dataclasses

        fields = {f.name for f in dataclasses.fields(BackendRegistry)}
        assert "refresh_tokens" in fields

    def test_factory_sqlite_creates_refresh_token_backend(self, tmp_path):
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.storage.protocols import RefreshTokenBackend

        data_dir = str(tmp_path / "data")
        Path(data_dir).mkdir(parents=True)
        (tmp_path / "groups.db").touch()
        registry = StorageFactory._create_sqlite_backends(data_dir)
        assert isinstance(registry.refresh_tokens, RefreshTokenBackend)
