"""
Tests for Story #558: TOTP MFA Core Engine.

Verifies secret generation, TOTP verification with replay prevention,
recovery codes, QR code generation, and MFA lifecycle.
"""

import os
import sqlite3
import tempfile

import pyotp
import pytest

from code_indexer.server.auth.totp_service import TOTPService


@pytest.fixture
def totp_service():
    """Create TOTPService with temp database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    service = TOTPService(db_path=db_path, mfa_encryption_key=key)
    yield service
    if os.path.exists(db_path):
        os.unlink(db_path)


class TestSecretGeneration:
    """Secret generation and storage."""

    def test_generate_secret_returns_base32(self, totp_service):
        """Secret must be a valid base32 string."""
        secret = totp_service.generate_secret("alice")
        assert len(secret) == 32
        totp = pyotp.TOTP(secret)
        assert totp.now()

    def test_generate_secret_stores_encrypted(self, totp_service):
        """Secret must be stored encrypted, not plaintext."""
        secret = totp_service.generate_secret("alice")
        conn = totp_service._get_conn()
        row = conn.execute(
            "SELECT encrypted_secret FROM user_mfa WHERE user_id = 'alice'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["encrypted_secret"] != secret

    def test_generate_secret_mfa_not_enabled(self, totp_service):
        """mfa_enabled must remain False after secret generation."""
        totp_service.generate_secret("alice")
        assert totp_service.is_mfa_enabled("alice") is False

    def test_generate_secret_overwrites_existing(self, totp_service):
        """Generating a new secret replaces the old one."""
        secret1 = totp_service.generate_secret("alice")
        secret2 = totp_service.generate_secret("alice")
        assert secret1 != secret2


class TestProvisioningURI:
    """Provisioning URI generation."""

    def test_returns_otpauth_uri(self, totp_service):
        totp_service.generate_secret("alice")
        uri = totp_service.get_provisioning_uri("alice")
        assert uri is not None
        assert uri.startswith("otpauth://totp/")

    def test_uri_contains_issuer(self, totp_service):
        totp_service.generate_secret("alice")
        uri = totp_service.get_provisioning_uri("alice")
        assert "issuer=CIDX" in uri

    def test_uri_contains_username(self, totp_service):
        totp_service.generate_secret("alice")
        uri = totp_service.get_provisioning_uri("alice")
        assert "alice" in uri

    def test_returns_none_for_unknown_user(self, totp_service):
        assert totp_service.get_provisioning_uri("unknown") is None


class TestQRCode:
    """QR code generation."""

    def test_generates_png_bytes(self, totp_service):
        totp_service.generate_secret("alice")
        uri = totp_service.get_provisioning_uri("alice")
        qr_bytes = totp_service.generate_qr_code(uri)
        assert isinstance(qr_bytes, bytes)
        assert len(qr_bytes) > 100
        assert qr_bytes[:4] == b"\x89PNG"


@pytest.mark.slow
class TestCodeVerification:
    """TOTP code verification."""

    def test_valid_code_accepted(self, totp_service):
        secret = totp_service.generate_secret("alice")
        totp = pyotp.TOTP(secret)
        code = totp.now()
        assert totp_service.verify_code("alice", code) is True

    def test_invalid_code_rejected(self, totp_service):
        totp_service.generate_secret("alice")
        assert totp_service.verify_code("alice", "000000") is False

    def test_replay_rejected(self, totp_service):
        secret = totp_service.generate_secret("alice")
        totp = pyotp.TOTP(secret)
        code = totp.now()
        assert totp_service.verify_code("alice", code) is True
        assert totp_service.verify_code("alice", code) is False

    def test_unknown_user_rejected(self, totp_service):
        assert totp_service.verify_code("unknown", "123456") is False


@pytest.mark.slow
class TestRecoveryCodes:
    """Recovery code generation and verification."""

    def test_generates_correct_count(self, totp_service):
        totp_service.generate_secret("alice")
        codes = totp_service.generate_recovery_codes("alice")
        assert len(codes) == 10

    def test_code_format(self, totp_service):
        totp_service.generate_secret("alice")
        codes = totp_service.generate_recovery_codes("alice")
        for code in codes:
            parts = code.split("-")
            assert len(parts) == 4
            assert all(len(p) == 4 for p in parts)

    def test_valid_code_accepted(self, totp_service):
        totp_service.generate_secret("alice")
        codes = totp_service.generate_recovery_codes("alice")
        assert totp_service.verify_recovery_code("alice", codes[0]) is True

    def test_used_code_rejected(self, totp_service):
        totp_service.generate_secret("alice")
        codes = totp_service.generate_recovery_codes("alice")
        assert totp_service.verify_recovery_code("alice", codes[0]) is True
        assert totp_service.verify_recovery_code("alice", codes[0]) is False

    def test_invalid_code_rejected(self, totp_service):
        totp_service.generate_secret("alice")
        totp_service.generate_recovery_codes("alice")
        assert (
            totp_service.verify_recovery_code("alice", "XXXX-XXXX-XXXX-XXXX") is False
        )

    def test_regenerate_invalidates_old_codes(self, totp_service):
        totp_service.generate_secret("alice")
        old_codes = totp_service.generate_recovery_codes("alice")
        new_codes = totp_service.regenerate_recovery_codes("alice")
        assert totp_service.verify_recovery_code("alice", old_codes[0]) is False
        assert totp_service.verify_recovery_code("alice", new_codes[0]) is True


@pytest.mark.slow
class TestMFALifecycle:
    """MFA activation and deactivation."""

    def test_activate_with_valid_code(self, totp_service):
        secret = totp_service.generate_secret("alice")
        totp = pyotp.TOTP(secret)
        code = totp.now()
        assert totp_service.activate_mfa("alice", code) is True
        assert totp_service.is_mfa_enabled("alice") is True

    def test_activate_with_invalid_code_fails(self, totp_service):
        totp_service.generate_secret("alice")
        assert totp_service.activate_mfa("alice", "000000") is False
        assert totp_service.is_mfa_enabled("alice") is False

    def test_disable_removes_all_data(self, totp_service):
        secret = totp_service.generate_secret("alice")
        totp_service.generate_recovery_codes("alice")
        totp = pyotp.TOTP(secret)
        totp_service.activate_mfa("alice", totp.now())
        totp_service.disable_mfa("alice")
        assert totp_service.is_mfa_enabled("alice") is False
        assert totp_service.get_provisioning_uri("alice") is None

    def test_is_mfa_enabled_false_by_default(self, totp_service):
        assert totp_service.is_mfa_enabled("alice") is False


# ------------------------------------------------------------------
# Cluster/PostgreSQL mode test infrastructure (C1 + C2)
# ------------------------------------------------------------------


class _PgStyleSqliteConn:
    """SQLite connection presenting psycopg-style interface.

    Translates %s placeholders to ? for SQLite compatibility, and
    provides cursor-based access matching psycopg v3 conventions.
    """

    def __init__(self, sqlite_conn):
        self._conn = sqlite_conn
        self._conn.row_factory = sqlite3.Row

    @staticmethod
    def _translate_query(query):
        """Replace %s placeholders with ? for SQLite."""
        return query.replace("%s", "?")

    def execute(self, query, params=None):
        translated = self._translate_query(query)
        if params:
            return self._conn.execute(translated, params)
        return self._conn.execute(translated)

    def commit(self):
        self._conn.commit()

    def cursor(self):
        return _PgStyleSqliteCursor(self._conn)

    def close(self):
        pass  # Pool manages lifecycle


class _PgStyleSqliteCursor:
    """SQLite cursor presenting psycopg-style interface."""

    def __init__(self, conn):
        self._conn = conn
        self._result = None

    def execute(self, query, params=None):
        translated = _PgStyleSqliteConn._translate_query(query)
        if params:
            self._result = self._conn.execute(translated, params)
        else:
            self._result = self._conn.execute(translated)

    def fetchone(self):
        if self._result is None:
            return None
        return self._result.fetchone()

    def fetchall(self):
        if self._result is None:
            return []
        return self._result.fetchall()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _PgStyleSqlitePool:
    """SQLite-backed pool presenting psycopg v3 ConnectionPool interface.

    Allows testing PostgreSQL code paths with real SQL execution
    without requiring a PostgreSQL server.
    """

    def __init__(self):
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cluster_secrets (
                key_name   TEXT PRIMARY KEY,
                key_value  TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS user_mfa (
                user_id TEXT UNIQUE NOT NULL,
                encrypted_secret TEXT NOT NULL,
                key_id INTEGER DEFAULT 1,
                mfa_enabled BOOLEAN DEFAULT 0,
                last_used_counter INTEGER,
                last_used_otp_counter INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS user_recovery_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                used_at TIMESTAMP,
                used_ip TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_recovery_codes_user
            ON user_recovery_codes(user_id);
            """
        )
        self._conn.commit()

    def connection(self):
        return _PgStyleSqlitePoolCtx(self._conn)

    def close(self):
        self._conn.close()


class _PgStyleSqlitePoolCtx:
    """Context manager for pool.connection()."""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return _PgStyleSqliteConn(self._conn)

    def __exit__(self, *args):
        pass


@pytest.fixture
def pg_pool():
    """Create a PG-style SQLite pool for cluster mode tests."""
    pool = _PgStyleSqlitePool()
    yield pool
    pool.close()


@pytest.fixture
def totp_service_with_pool(pg_pool):
    """Create TOTPService with pool set (cluster mode)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    service = TOTPService(db_path=db_path, mfa_encryption_key=key)
    service.set_connection_pool(pg_pool)
    yield service
    if os.path.exists(db_path):
        os.unlink(db_path)


class TestSetConnectionPool:
    """C1/C2: Pool configuration for cluster mode."""

    def test_pool_initially_none(self, totp_service):
        """Pool must be None by default (standalone mode)."""
        assert totp_service._pool is None

    def test_set_connection_pool_stores_pool(self, totp_service, pg_pool):
        """set_connection_pool must store the pool reference."""
        totp_service.set_connection_pool(pg_pool)
        assert totp_service._pool is pg_pool

    def test_set_connection_pool_method_exists(self, totp_service):
        """TOTPService must have set_connection_pool method."""
        assert hasattr(totp_service, "set_connection_pool")
        assert callable(totp_service.set_connection_pool)


class TestClusterEncryptionKey:
    """C1: Encryption key stored in cluster_secrets table."""

    def test_key_stored_in_cluster_secrets_on_pool_set(self, pg_pool):
        """When pool is set, encryption key must be stored in cluster_secrets."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            service = TOTPService(db_path=db_path)
            service.set_connection_pool(pg_pool)

            with pg_pool.connection() as conn:
                row = conn.execute(
                    "SELECT key_value FROM cluster_secrets WHERE key_name = %s",
                    ("mfa_encryption_key",),
                ).fetchone()
            assert row is not None
            assert len(row["key_value"]) > 0
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_shared_key_across_instances(self, pg_pool):
        """Two TOTPService instances with same pool must use same key."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path1 = f.name
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path2 = f.name
        try:
            svc1 = TOTPService(db_path=db_path1)
            svc1.set_connection_pool(pg_pool)

            svc2 = TOTPService(db_path=db_path2)
            svc2.set_connection_pool(pg_pool)

            # Generate secret with svc1, decrypt with svc2
            secret = svc1.generate_secret("alice")
            decrypted = svc2._get_secret("alice")
            assert decrypted == secret
        finally:
            for p in [db_path1, db_path2]:
                if os.path.exists(p):
                    os.unlink(p)

    def test_key_persists_across_pool_reconnect(self, pg_pool):
        """Key in cluster_secrets must persist when a new service connects."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            svc1 = TOTPService(db_path=db_path)
            svc1.set_connection_pool(pg_pool)

            with pg_pool.connection() as conn:
                row = conn.execute(
                    "SELECT key_value FROM cluster_secrets WHERE key_name = %s",
                    ("mfa_encryption_key",),
                ).fetchone()
            key1 = row["key_value"]

            svc2 = TOTPService(db_path=db_path)
            svc2.set_connection_pool(pg_pool)

            with pg_pool.connection() as conn:
                row = conn.execute(
                    "SELECT key_value FROM cluster_secrets WHERE key_name = %s",
                    ("mfa_encryption_key",),
                ).fetchone()
            key2 = row["key_value"]

            assert key1 == key2
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)


@pytest.mark.slow
class TestClusterMFAOperations:
    """C2: MFA data operations through PostgreSQL pool."""

    def test_generate_secret_via_pool(self, totp_service_with_pool):
        """generate_secret must store data via pool connection."""
        secret = totp_service_with_pool.generate_secret("alice")
        assert len(secret) == 32

    def test_get_secret_via_pool(self, totp_service_with_pool):
        """_get_secret must read data via pool connection."""
        secret = totp_service_with_pool.generate_secret("alice")
        retrieved = totp_service_with_pool._get_secret("alice")
        assert retrieved == secret

    def test_verify_code_via_pool(self, totp_service_with_pool):
        """verify_code must work through pool connection."""
        secret = totp_service_with_pool.generate_secret("alice")
        totp = pyotp.TOTP(secret)
        code = totp.now()
        assert totp_service_with_pool.verify_code("alice", code) is True

    def test_replay_rejected_via_pool(self, totp_service_with_pool):
        """Replay prevention must work through pool connection."""
        secret = totp_service_with_pool.generate_secret("alice")
        totp = pyotp.TOTP(secret)
        code = totp.now()
        assert totp_service_with_pool.verify_code("alice", code) is True
        assert totp_service_with_pool.verify_code("alice", code) is False

    def test_recovery_codes_via_pool(self, totp_service_with_pool):
        """Recovery code generation must work through pool."""
        totp_service_with_pool.generate_secret("alice")
        codes = totp_service_with_pool.generate_recovery_codes("alice")
        assert len(codes) == 10
        assert totp_service_with_pool.verify_recovery_code("alice", codes[0]) is True
        assert totp_service_with_pool.verify_recovery_code("alice", codes[0]) is False

    def test_activate_mfa_via_pool(self, totp_service_with_pool):
        """MFA activation must work through pool."""
        secret = totp_service_with_pool.generate_secret("alice")
        totp = pyotp.TOTP(secret)
        code = totp.now()
        assert totp_service_with_pool.activate_mfa("alice", code) is True
        assert totp_service_with_pool.is_mfa_enabled("alice") is True

    def test_disable_mfa_via_pool(self, totp_service_with_pool):
        """MFA disable must work through pool."""
        secret = totp_service_with_pool.generate_secret("alice")
        totp = pyotp.TOTP(secret)
        totp_service_with_pool.activate_mfa("alice", totp.now())
        totp_service_with_pool.disable_mfa("alice")
        assert totp_service_with_pool.is_mfa_enabled("alice") is False

    def test_is_mfa_enabled_via_pool(self, totp_service_with_pool):
        """is_mfa_enabled must work through pool."""
        assert totp_service_with_pool.is_mfa_enabled("alice") is False

    def test_get_provisioning_uri_via_pool(self, totp_service_with_pool):
        """Provisioning URI must work through pool."""
        totp_service_with_pool.generate_secret("alice")
        uri = totp_service_with_pool.get_provisioning_uri("alice")
        assert uri is not None
        assert uri.startswith("otpauth://totp/")

    def test_manual_entry_key_via_pool(self, totp_service_with_pool):
        """Manual entry key must work through pool."""
        totp_service_with_pool.generate_secret("alice")
        key = totp_service_with_pool.get_manual_entry_key("alice")
        assert key is not None
        assert " " in key

    def test_unknown_user_returns_none_via_pool(self, totp_service_with_pool):
        """Unknown user queries must return None/False via pool."""
        assert totp_service_with_pool._get_secret("unknown") is None
        assert totp_service_with_pool.is_mfa_enabled("unknown") is False
        assert totp_service_with_pool.verify_code("unknown", "123456") is False


# ------------------------------------------------------------------
# Story #923 AC9: verify_enabled_code() with timestamp-based CAS replay
# ------------------------------------------------------------------

import time as _time  # noqa: E402

# TOTP codes rotate every 30 s; sleep 31 s to guarantee a new window.
_TOTP_WINDOW_ADVANCE_SECONDS = 31
_VEC_USERNAME = "vec_user"
_VEC_BAD_CODE = "000000"


@pytest.fixture
def _activated_service(totp_service):
    """Service with MFA activated; positioned one window past activation."""
    secret = totp_service.generate_secret(_VEC_USERNAME)
    totp = pyotp.TOTP(secret)
    totp_service.activate_mfa(_VEC_USERNAME, totp.now())
    _time.sleep(_TOTP_WINDOW_ADVANCE_SECONDS)
    return totp_service, totp


@pytest.fixture
def _activated_service_with_pool(totp_service_with_pool):
    """Pool-mode service with MFA activated; positioned one window past activation."""
    secret = totp_service_with_pool.generate_secret(_VEC_USERNAME)
    totp = pyotp.TOTP(secret)
    totp_service_with_pool.activate_mfa(_VEC_USERNAME, totp.now())
    _time.sleep(_TOTP_WINDOW_ADVANCE_SECONDS)
    return totp_service_with_pool, totp


@pytest.mark.slow
class TestVerifyEnabledCodeWithPool:
    """verify_enabled_code() via PostgreSQL pool path (_cas_otp_counter_pg)."""

    def test_returns_true_for_valid_code_via_pool(self, _activated_service_with_pool):
        """Must return True for a fresh valid code when MFA is active (pool path)."""
        svc, totp = _activated_service_with_pool
        assert svc.verify_enabled_code(_VEC_USERNAME, totp.now()) is True

    def test_returns_false_on_replay_via_pool(self, _activated_service_with_pool):
        """Same OTP must be rejected on second call via pool (replay prevention)."""
        svc, totp = _activated_service_with_pool
        fresh_code = totp.now()
        assert svc.verify_enabled_code(_VEC_USERNAME, fresh_code) is True
        assert svc.verify_enabled_code(_VEC_USERNAME, fresh_code) is False


@pytest.mark.slow
class TestVerifyEnabledCode:
    """verify_enabled_code() requires MFA enabled; blocks replay via timestamp CAS."""

    def test_raises_on_non_digit_code(self, totp_service):
        """Must raise ValueError when code contains non-digit characters."""
        with pytest.raises(ValueError, match="6 decimal digits"):
            totp_service.verify_enabled_code(_VEC_USERNAME, "abc123")

    def test_raises_on_wrong_length_code(self, totp_service):
        """Must raise ValueError when code is not exactly 6 digits."""
        with pytest.raises(ValueError, match="6 decimal digits"):
            totp_service.verify_enabled_code(_VEC_USERNAME, "12345")

    def test_returns_false_when_mfa_not_enabled(self, totp_service):
        """Must return False when MFA is not yet activated."""
        totp_service.generate_secret(_VEC_USERNAME)
        assert totp_service.verify_enabled_code(_VEC_USERNAME, "123456") is False

    def test_returns_false_for_wrong_code_when_enabled(self, totp_service):
        """Must return False for invalid code even when MFA is active."""
        secret = totp_service.generate_secret(_VEC_USERNAME)
        totp = pyotp.TOTP(secret)
        totp_service.activate_mfa(_VEC_USERNAME, totp.now())
        assert totp_service.verify_enabled_code(_VEC_USERNAME, _VEC_BAD_CODE) is False

    def test_returns_true_for_valid_code_when_enabled(self, _activated_service):
        """Must return True for a fresh valid code when MFA is active."""
        svc, totp = _activated_service
        assert svc.verify_enabled_code(_VEC_USERNAME, totp.now()) is True

    def test_returns_false_on_replay(self, _activated_service):
        """Same OTP must be rejected on second call (replay prevention)."""
        svc, totp = _activated_service
        fresh_code = totp.now()
        assert svc.verify_enabled_code(_VEC_USERNAME, fresh_code) is True
        assert svc.verify_enabled_code(_VEC_USERNAME, fresh_code) is False


# ------------------------------------------------------------------
# Codex M1: Atomic CAS concurrency test for verify_recovery_code
# ------------------------------------------------------------------


def test_verify_recovery_code_concurrent_consumption_only_one_succeeds(tmp_path):
    """Atomic CAS — 20 threads consuming the same code → exactly 1 succeeds.

    Verifies that the single conditional UPDATE prevents TOCTOU race where two
    concurrent requests could both observe the same unused code and both succeed.
    """
    import threading

    db = str(tmp_path / "totp.db")
    svc = TOTPService(db_path=db)
    svc.generate_secret("admin")
    codes = svc.generate_recovery_codes("admin")
    code = codes[0]

    results = []
    lock = threading.Lock()

    def attempt():
        result = svc.verify_recovery_code("admin", code)
        with lock:
            results.append(result)

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = sum(1 for r in results if r)
    assert successes == 1, (
        f"Expected exactly 1 success under concurrency, got {successes}"
    )
