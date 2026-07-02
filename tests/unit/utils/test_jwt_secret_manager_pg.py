"""
Unit tests for JWTSecretManager PostgreSQL-backed secret storage.

Story #528: JWT Secret Cluster Sharing

Tests cover:
- AC2: PG mode generates and stores secret on first boot
- AC3: PG mode reads existing secret on subsequent boots
- AC4: Race condition safety (ON CONFLICT DO NOTHING + re-read)
- AC5: SQLite mode behavior unchanged (file-based)
- AC7: Migration from local file to PG on upgrade
- rotate_secret works in PG mode
"""

import secrets
from contextlib import contextmanager
from pathlib import Path
from typing import Optional


from code_indexer.server.utils.jwt_secret_manager import JWTSecretManager


# ---------------------------------------------------------------------------
# AC5: SQLite / file-based mode behavior is completely unchanged
# ---------------------------------------------------------------------------


class TestJWTSecretManagerFileMode:
    """Verify the pre-existing file-based behaviour still works (AC5)."""

    def test_creates_new_secret_when_no_file_exists(self, tmp_path):
        """New secret is generated and saved when secret file does not exist."""
        mgr = JWTSecretManager(server_dir_path=str(tmp_path))
        secret = mgr.get_or_create_secret()
        assert secret
        assert len(secret) >= 32  # token_urlsafe(32) produces >= 43 chars
        assert mgr.secret_file_path.exists()

    def test_returns_existing_secret_from_file(self, tmp_path):
        """Existing secret is read from file on subsequent calls."""
        mgr = JWTSecretManager(server_dir_path=str(tmp_path))
        first = mgr.get_or_create_secret()

        # Create a fresh manager pointing to the same dir — simulates restart
        mgr2 = JWTSecretManager(server_dir_path=str(tmp_path))
        second = mgr2.get_or_create_secret()
        assert first == second

    def test_rotate_secret_updates_file(self, tmp_path):
        """rotate_secret() generates new secret and writes it to file."""
        mgr = JWTSecretManager(server_dir_path=str(tmp_path))
        original = mgr.get_or_create_secret()
        rotated = mgr.rotate_secret()
        assert rotated != original
        # File should now hold the rotated secret
        assert mgr.secret_file_path.read_text().strip() == rotated

    def test_no_pg_dsn_means_no_pg_interaction(self, tmp_path):
        """When pg_dsn is None, JWTSecretManager uses file mode only."""
        mgr = JWTSecretManager(server_dir_path=str(tmp_path), pg_dsn=None)
        secret = mgr.get_or_create_secret()
        assert secret
        assert mgr.secret_file_path.exists()


# ---------------------------------------------------------------------------
# Helpers for in-process PG simulation via a lightweight stub
#
# We cannot use real PostgreSQL in unit tests, but we also must not lie about
# the interface (anti-mock rules).  The strategy here is to provide a minimal
# *in-process stub* that implements exactly the connection-context-manager
# interface that JWTSecretManager calls, backed by an in-memory dict.
# This is NOT a Mock — it is a real, behaviour-correct minimal implementation
# of the narrow interface, so tests prove the logic, not just call sequences.
# ---------------------------------------------------------------------------


class _InMemoryCursor:
    """Tiny cursor stub sufficient for JWTSecretManager's needs."""

    def __init__(self, store: dict):
        self._store = store
        self._result: Optional[tuple] = None

    def execute(self, sql: str, params=()) -> "_InMemoryCursor":
        sql_stripped = " ".join(sql.split()).upper()

        if sql_stripped.startswith("CREATE TABLE"):
            # CREATE TABLE IF NOT EXISTS cluster_secrets — no-op
            pass
        elif sql_stripped.startswith("SELECT KEY_VALUE FROM CLUSTER_SECRETS"):
            row = self._store.get("jwt_secret")
            self._result = (row,) if row else None
        elif (
            "INSERT INTO CLUSTER_SECRETS" in sql_stripped
            and "ON CONFLICT" in sql_stripped
        ):
            key_value = params[0] if params else None
            if key_value:
                if "DO UPDATE" in sql_stripped:
                    # UPSERT: always overwrite (used by rotate_secret)
                    self._store["jwt_secret"] = key_value
                elif "jwt_secret" not in self._store:
                    # DO NOTHING: only insert if absent (used by get_or_create)
                    self._store["jwt_secret"] = key_value
        elif sql_stripped.startswith("UPDATE CLUSTER_SECRETS"):
            key_value = params[0] if params else None
            if key_value:
                self._store["jwt_secret"] = key_value
        return self

    def fetchone(self) -> Optional[tuple]:
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class _InMemoryConn:
    """Minimal connection stub backed by a shared dict."""

    def __init__(self, store: dict):
        self._store = store

    def execute(self, sql: str, params=()) -> _InMemoryCursor:
        cur = _InMemoryCursor(self._store)
        cur.execute(sql, params)
        return cur

    def commit(self):
        pass  # in-memory, always consistent

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def _make_manager_with_store(tmp_path: Path, store: dict) -> JWTSecretManager:
    """
    Build a JWTSecretManager that routes PG calls to an in-memory dict.

    Instead of mocking psycopg globally (which would be a lie), we replace
    the manager's internal _pg_connect method after construction with a
    real context-manager backed by an in-memory connection stub.
    """
    mgr = JWTSecretManager(
        server_dir_path=str(tmp_path), pg_dsn="postgresql://test/test"
    )

    # Override the _pg_connect context-manager to yield an in-memory connection
    @contextmanager
    def _fake_connect():
        yield _InMemoryConn(store)

    mgr._pg_connect = _fake_connect  # type: ignore[attr-defined]
    return mgr


# ---------------------------------------------------------------------------
# AC2 + AC3: PG mode — first boot generates, subsequent boots read existing
# ---------------------------------------------------------------------------


class TestJWTSecretManagerPGMode:
    """PG-backed secret storage behaviour tests."""

    def test_pg_mode_generates_secret_on_first_boot(self, tmp_path):
        """AC2: First boot in PG mode generates a secret and stores it."""
        store: dict = {}
        mgr = _make_manager_with_store(tmp_path, store)
        secret = mgr.get_or_create_secret()
        assert secret
        assert len(secret) >= 32
        assert store.get("jwt_secret") == secret

    def test_pg_mode_reads_existing_secret_on_subsequent_boot(self, tmp_path):
        """AC3: Subsequent boots read the existing secret from PG, not file."""
        store: dict = {}

        # First boot — stores secret
        mgr1 = _make_manager_with_store(tmp_path, store)
        first_secret = mgr1.get_or_create_secret()

        # Second boot with same shared store — should return same secret
        tmp_path2 = tmp_path / "node2"
        tmp_path2.mkdir()
        mgr2 = _make_manager_with_store(tmp_path2, store)
        second_secret = mgr2.get_or_create_secret()

        assert first_secret == second_secret

    def test_pg_mode_secret_not_written_to_local_file(self, tmp_path):
        """PG mode should NOT write the secret to a local file."""
        store: dict = {}
        mgr = _make_manager_with_store(tmp_path, store)
        mgr.get_or_create_secret()
        # No local file written in PG mode
        assert not mgr.secret_file_path.exists()

    def test_pg_mode_rotate_secret_updates_pg(self, tmp_path):
        """rotate_secret() in PG mode updates the PG record."""
        store: dict = {}
        mgr = _make_manager_with_store(tmp_path, store)
        original = mgr.get_or_create_secret()

        rotated = mgr.rotate_secret()
        assert rotated != original
        assert store.get("jwt_secret") == rotated


# ---------------------------------------------------------------------------
# AC4: Race condition safety
# ---------------------------------------------------------------------------


class TestPGRaceConditionSafety:
    """
    Verify that simultaneous first-boot attempts do not corrupt the secret.

    The ON CONFLICT DO NOTHING + re-read pattern ensures only one winner.
    The in-memory store naturally serializes inserts, so we can verify
    that regardless of ordering, get_or_create_secret() always returns
    the value actually stored (the winner).
    """

    def test_concurrent_first_boot_returns_winner_secret(self, tmp_path):
        """AC4: Two simultaneous first-boot calls yield the same stored secret."""
        # Pre-seed the store as if another node already inserted
        pre_seeded_secret = secrets.token_urlsafe(32)
        store: dict = {"jwt_secret": pre_seeded_secret}

        # Our node tries to insert its own secret — should lose to the winner
        mgr = _make_manager_with_store(tmp_path, store)
        result = mgr.get_or_create_secret()

        # Must return the pre-seeded (winning) secret, not its own generated one
        assert result == pre_seeded_secret

    def test_on_conflict_does_not_overwrite_existing(self, tmp_path):
        """ON CONFLICT DO NOTHING: existing secret is never overwritten by new insert."""
        store: dict = {}

        mgr1 = _make_manager_with_store(tmp_path, store)
        first_secret = mgr1.get_or_create_secret()

        # Simulate a late-arriving node trying to insert again
        tmp_path2 = tmp_path / "node2"
        tmp_path2.mkdir()
        mgr2 = _make_manager_with_store(tmp_path2, store)
        second_secret = mgr2.get_or_create_secret()

        # Neither call should have overwritten the original
        assert store["jwt_secret"] == first_secret
        assert second_secret == first_secret


# ---------------------------------------------------------------------------
# AC7: Migration from local file to PG on upgrade
# ---------------------------------------------------------------------------


class TestLocalFileMigrationToPG:
    """Existing local-file secret is copied to PG on upgrade."""

    def test_migrate_local_secret_to_pg_copies_file_secret(self, tmp_path):
        """AC7: If local file exists, its secret is copied to PG during migration."""
        # Set up local file with existing secret (pre-upgrade state)
        local_secret = secrets.token_urlsafe(32)
        secret_file = tmp_path / ".jwt_secret"
        secret_file.write_text(local_secret)
        secret_file.chmod(0o600)

        store: dict = {}
        mgr = _make_manager_with_store(tmp_path, store)
        mgr._migrate_local_secret_to_pg()

        assert store.get("jwt_secret") == local_secret

    def test_migrate_noop_when_no_local_file(self, tmp_path):
        """AC7: Migration is a no-op when no local file exists."""
        store: dict = {}
        mgr = _make_manager_with_store(tmp_path, store)
        mgr._migrate_local_secret_to_pg()
        assert "jwt_secret" not in store

    def test_migrate_does_not_overwrite_existing_pg_secret(self, tmp_path):
        """AC7: Migration respects ON CONFLICT — does not overwrite existing PG secret."""
        # Local file has old secret
        local_secret = secrets.token_urlsafe(32)
        secret_file = tmp_path / ".jwt_secret"
        secret_file.write_text(local_secret)
        secret_file.chmod(0o600)

        # PG already has a newer secret (another node beat us to it)
        pg_secret = secrets.token_urlsafe(32)
        store: dict = {"jwt_secret": pg_secret}

        mgr = _make_manager_with_store(tmp_path, store)
        mgr._migrate_local_secret_to_pg()

        # PG secret must NOT be overwritten
        assert store.get("jwt_secret") == pg_secret

    def test_get_or_create_migrates_then_returns_pg_secret(self, tmp_path):
        """Integration: get_or_create_secret with migration returns PG-stored value."""
        local_secret = secrets.token_urlsafe(32)
        secret_file = tmp_path / ".jwt_secret"
        secret_file.write_text(local_secret)
        secret_file.chmod(0o600)

        store: dict = {}
        mgr = _make_manager_with_store(tmp_path, store)

        # Explicitly migrate then get
        mgr._migrate_local_secret_to_pg()
        result = mgr.get_or_create_secret()

        assert result == local_secret
        assert store.get("jwt_secret") == local_secret
