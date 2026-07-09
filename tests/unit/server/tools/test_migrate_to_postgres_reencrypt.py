"""
Unit tests for token re-encryption support in migrate_to_postgres.py.

Tests cover:
- --server-dir argument is registered in the arg parser
- _reencrypt_tokens() skips when server_dir is None
- _reencrypt_tokens() skips when .jwt_secret is absent
- _reencrypt_tokens() skips when old key == new key (same hostname)
- _reencrypt_tokens() re-encrypts ci_tokens rows
- _reencrypt_tokens() re-encrypts user_git_credentials rows
- Bad token emits warning log; other tokens still processed
- migrate_all() result contains token_reencryption key when server_dir is set

Mocking strategy: only the external psycopg boundary is mocked (no real PG available).
Encryption/decryption uses real AES-256-CBC (no mock). Internal SUT methods are not mocked.
"""

import base64
import hashlib
import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from src.code_indexer.server.tools.migrate_to_postgres import (
    SqliteToPostgresMigrator,
    _build_arg_parser,
)

# ---------------------------------------------------------------------------
# Test constants (not secrets — safe to hard-code)
# ---------------------------------------------------------------------------

_PLATFORM_A = "github"
_PLATFORM_B = "gitlab"
_FORGE_TYPE = "github"
_FORGE_HOST = "github.com"
_USERNAME = "alice"

# ---------------------------------------------------------------------------
# Encryption helpers (mirror _reencrypt_tokens internals for test assertions)
# ---------------------------------------------------------------------------

_PBKDF2_ITERATIONS = 100000
_AES_KEY_SIZE = 32
_AES_BLOCK_SIZE = 16


def _derive_key(salt_input: str) -> bytes:
    salt = hashlib.sha256(salt_input.encode("utf-8")).digest()
    return hashlib.pbkdf2_hmac(
        "sha256",
        b"cidx-token-encryption-key",
        salt,
        _PBKDF2_ITERATIONS,
        dklen=_AES_KEY_SIZE,
    )


def _encrypt(plaintext: str, key: bytes) -> str:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    iv = os.urandom(_AES_BLOCK_SIZE)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    enc = cipher.encryptor()
    return base64.b64encode(iv + enc.update(padded) + enc.finalize()).decode("utf-8")


def _decrypt(ciphertext: str, key: bytes) -> str:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    combined = base64.b64decode(ciphertext.encode("utf-8"))
    iv = combined[:_AES_BLOCK_SIZE]
    enc_data = combined[_AES_BLOCK_SIZE:]
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    dec = cipher.decryptor()
    padded = dec.update(enc_data) + dec.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def server_dir(tmp_path):
    sd = tmp_path / ".cidx-server"
    sd.mkdir()
    return sd


@pytest.fixture
def pg_db(tmp_path):
    """Minimal SQLite DB acting as PostgreSQL substitute for token tables."""
    db_path = str(tmp_path / "pg_mock.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE ci_tokens "
            "(platform TEXT PRIMARY KEY, encrypted_token TEXT NOT NULL, base_url TEXT)"
        )
        conn.execute(
            """CREATE TABLE user_git_credentials (
                credential_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                forge_type TEXT NOT NULL,
                forge_host TEXT NOT NULL,
                encrypted_token TEXT
            )"""
        )
        conn.commit()
    return db_path


@pytest.fixture
def migrator(tmp_path):
    """Migrator without server_dir."""
    sqlite_db = str(tmp_path / "cidx_server.db")
    groups_db = str(tmp_path / "groups.db")
    for db in (sqlite_db, groups_db):
        with sqlite3.connect(db) as conn:
            conn.commit()
    return SqliteToPostgresMigrator(
        sqlite_db_path=sqlite_db,
        groups_db_path=groups_db,
        pg_connection_string="postgresql://fake/fake",
    )


@pytest.fixture
def migrator_with_server_dir(tmp_path, server_dir):
    """Migrator with server_dir set."""
    sqlite_db = str(tmp_path / "cidx_server.db")
    groups_db = str(tmp_path / "groups.db")
    for db in (sqlite_db, groups_db):
        with sqlite3.connect(db) as conn:
            conn.commit()
    return SqliteToPostgresMigrator(
        sqlite_db_path=sqlite_db,
        groups_db_path=groups_db,
        pg_connection_string="postgresql://fake/fake",
        server_dir=str(server_dir),
    )


@pytest.fixture
def psycopg_mock_for(pg_db):
    """Factory fixture: patch psycopg.connect to use SQLite-backed mock for a given pg_db path."""

    @contextmanager
    def _patch():
        mock_conn = _make_sqlite_psycopg_mock(pg_db)
        with patch(
            "src.code_indexer.server.tools.migrate_to_postgres.psycopg"
        ) as mock_psycopg:
            mock_psycopg.connect.return_value.__enter__ = lambda s: mock_conn
            mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
            yield mock_psycopg

    return _patch


# ---------------------------------------------------------------------------
# Tests: --server-dir in arg parser
# ---------------------------------------------------------------------------


class TestServerDirArgParser:
    def test_server_dir_arg_in_arg_parser(self):
        """--server-dir argument is registered in the CLI arg parser."""
        parser = _build_arg_parser()
        args = parser.parse_args(
            [
                "--sqlite-path",
                "/fake/cidx_server.db",
                "--groups-path",
                "/fake/groups.db",
                "--pg-url",
                "postgresql://localhost/db",
                "--server-dir",
                "/fake/.cidx-server",
            ]
        )
        assert args.server_dir == "/fake/.cidx-server"


# ---------------------------------------------------------------------------
# Tests: _reencrypt_tokens
# ---------------------------------------------------------------------------


class TestReencryptTokens:
    def test_reencrypt_tokens_skipped_when_no_server_dir(self, migrator):
        """_reencrypt_tokens returns {} immediately when server_dir is None."""
        result = migrator._reencrypt_tokens()
        assert result == {}

    def test_reencrypt_tokens_skipped_when_server_dir_not_a_directory(self, tmp_path):
        """_reencrypt_tokens returns {} when server_dir path is not a directory."""
        sqlite_db = str(tmp_path / "cidx_server.db")
        groups_db = str(tmp_path / "groups.db")
        for db in (sqlite_db, groups_db):
            with sqlite3.connect(db) as conn:
                conn.commit()
        nonexistent_dir = str(tmp_path / "does_not_exist")
        m = SqliteToPostgresMigrator(
            sqlite_db_path=sqlite_db,
            groups_db_path=groups_db,
            pg_connection_string="postgresql://fake/fake",
            server_dir=nonexistent_dir,
        )
        result = m._reencrypt_tokens()
        assert result == {}

    def test_reencrypt_tokens_skipped_when_no_jwt_secret(
        self, migrator_with_server_dir, server_dir
    ):
        """_reencrypt_tokens returns {} when .jwt_secret does not exist."""
        assert not (server_dir / ".jwt_secret").exists()
        result = migrator_with_server_dir._reencrypt_tokens()
        assert result == {}

    def test_reencrypt_tokens_same_key_no_op(
        self, migrator_with_server_dir, server_dir
    ):
        """_reencrypt_tokens returns {} when old key == new key (jwt_secret == hostname)."""
        hostname = os.uname().nodename
        (server_dir / ".jwt_secret").write_text(hostname)
        result = migrator_with_server_dir._reencrypt_tokens()
        assert result == {}

    def test_reencrypt_tokens_ci_tokens(
        self, migrator_with_server_dir, server_dir, pg_db, psycopg_mock_for
    ):
        """_reencrypt_tokens re-encrypts ci_tokens rows from hostname key to jwt_secret key."""
        jwt_secret = uuid.uuid4().hex
        (server_dir / ".jwt_secret").write_text(jwt_secret)

        old_key = _derive_key(os.uname().nodename)
        new_key = _derive_key(jwt_secret)
        plaintext = uuid.uuid4().hex
        old_enc = _encrypt(plaintext, old_key)

        with sqlite3.connect(pg_db) as conn:
            conn.execute(
                "INSERT INTO ci_tokens (platform, encrypted_token) VALUES (?, ?)",
                (_PLATFORM_A, old_enc),
            )
            conn.commit()

        with psycopg_mock_for():
            result = migrator_with_server_dir._reencrypt_tokens()

        assert result.get("ci_tokens") == 1
        with sqlite3.connect(pg_db) as conn:
            row = conn.execute(
                "SELECT encrypted_token FROM ci_tokens WHERE platform = ?",
                (_PLATFORM_A,),
            ).fetchone()
        assert row is not None
        assert _decrypt(row[0], new_key) == plaintext

    def test_reencrypt_tokens_git_credentials(
        self, migrator_with_server_dir, server_dir, pg_db, psycopg_mock_for
    ):
        """_reencrypt_tokens re-encrypts user_git_credentials rows."""
        jwt_secret = uuid.uuid4().hex
        (server_dir / ".jwt_secret").write_text(jwt_secret)

        old_key = _derive_key(os.uname().nodename)
        new_key = _derive_key(jwt_secret)
        plaintext = uuid.uuid4().hex
        old_enc = _encrypt(plaintext, old_key)
        cred_id = uuid.uuid4().hex

        with sqlite3.connect(pg_db) as conn:
            conn.execute(
                "INSERT INTO user_git_credentials "
                "(credential_id, username, forge_type, forge_host, encrypted_token) "
                "VALUES (?, ?, ?, ?, ?)",
                (cred_id, _USERNAME, _FORGE_TYPE, _FORGE_HOST, old_enc),
            )
            conn.commit()

        with psycopg_mock_for():
            result = migrator_with_server_dir._reencrypt_tokens()

        assert result.get("user_git_credentials") == 1
        with sqlite3.connect(pg_db) as conn:
            row = conn.execute(
                "SELECT encrypted_token FROM user_git_credentials WHERE credential_id = ?",
                (cred_id,),
            ).fetchone()
        assert row is not None
        assert _decrypt(row[0], new_key) == plaintext

    def test_reencrypt_tokens_bad_token_skipped(
        self, migrator_with_server_dir, server_dir, pg_db, psycopg_mock_for, caplog
    ):
        """Bad encrypted token emits a warning log; the good token is still processed."""
        jwt_secret = uuid.uuid4().hex
        (server_dir / ".jwt_secret").write_text(jwt_secret)

        old_key = _derive_key(os.uname().nodename)
        new_key = _derive_key(jwt_secret)
        good_plaintext = uuid.uuid4().hex
        good_enc = _encrypt(good_plaintext, old_key)
        bad_enc = base64.b64encode(b"X" * 32).decode()  # not valid AES-CBC ciphertext

        with sqlite3.connect(pg_db) as conn:
            conn.execute(
                "INSERT INTO ci_tokens (platform, encrypted_token) VALUES (?, ?)",
                (_PLATFORM_A, good_enc),
            )
            conn.execute(
                "INSERT INTO ci_tokens (platform, encrypted_token) VALUES (?, ?)",
                (_PLATFORM_B, bad_enc),
            )
            conn.commit()

        with psycopg_mock_for(), caplog.at_level(logging.WARNING):
            result = migrator_with_server_dir._reencrypt_tokens()

        assert result.get("ci_tokens") == 1
        assert any(r.levelno >= logging.WARNING for r in caplog.records)

        with sqlite3.connect(pg_db) as conn:
            row = conn.execute(
                "SELECT encrypted_token FROM ci_tokens WHERE platform = ?",
                (_PLATFORM_A,),
            ).fetchone()
        assert _decrypt(row[0], new_key) == good_plaintext


# ---------------------------------------------------------------------------
# Tests: migrate_all includes token_reencryption
# ---------------------------------------------------------------------------


class TestMigrateAllReencrypt:
    def test_migrate_all_calls_reencrypt_when_server_dir_set(
        self, migrator_with_server_dir, server_dir, pg_db, psycopg_mock_for
    ):
        """migrate_all() result contains token_reencryption key when server_dir is set."""
        jwt_secret = uuid.uuid4().hex
        (server_dir / ".jwt_secret").write_text(jwt_secret)

        with psycopg_mock_for():
            report = migrator_with_server_dir.migrate_all()

        assert "token_reencryption" in report


# ---------------------------------------------------------------------------
# SQLite-backed psycopg connection mock
# ---------------------------------------------------------------------------


def _make_sqlite_psycopg_mock(db_path: str):
    """Mock psycopg connection backed by real SQLite for integration-level encryption tests."""
    conn = sqlite3.connect(db_path)

    class _FakeCursor:
        def __init__(self):
            self._cursor = conn.cursor()
            self.rowcount = 0

        def execute(self, sql, params=()):
            sql_sq = sql.replace("%s", "?")
            self._cursor.execute(sql_sq, params)
            self.rowcount = self._cursor.rowcount
            return self

        def fetchall(self):
            return [tuple(r) for r in self._cursor.fetchall()]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class _FakeConn:
        def execute(self, sql, params=()):
            cur = _FakeCursor()
            cur.execute(sql, params)
            return cur

        def cursor(self):
            return _FakeCursor()

        def commit(self):
            conn.commit()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            conn.commit()

    return _FakeConn()
