"""
Unit tests for Bug #1222: CITokenManager APP-GENERAL-061 decrypt warning de-spam.

The module-level memo must suppress repeated WARNING floods when the same
undecryptable ciphertext is read by fresh manager instances (manager is NOT
a singleton — it is constructed fresh at every call site).

TDD: tests written BEFORE implementation.
"""

import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Generator, List

import pytest

# ---------------------------------------------------------------------------
# Test constants — synthetic only, not real credentials
# ---------------------------------------------------------------------------

_SALT_A = "test-despam-salt-alpha-1222"

# Fake undecryptable ciphertexts — non-empty base64-ish strings that fail AES decrypt.
_BAD_CIPHERTEXT_1 = (
    "dGVzdC1iYWQtY2lwaGVydGV4dC1BQUFBQUFBQUFBQUFBQUFBQUFBQUFB"
    "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFB"
    "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFB"
)
_BAD_CIPHERTEXT_2 = (
    "dGVzdC1iYWQtY2lwaGVydGV4dC1CQkJCQkJCQkJCQkJCQkJCQkJCQkJC"
    "QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJC"
    "QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJC"
)

# A real GitHub-format test token (not a real credential)
_GOOD_TOKEN = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"

# Concurrency test sizing
_CONCURRENT_WORKERS = 8
_CONCURRENT_CALLS = 16


# ---------------------------------------------------------------------------
# Log-filtering helpers
# ---------------------------------------------------------------------------


def _app061_warnings(caplog) -> List[logging.LogRecord]:
    """Return all WARNING records containing APP-GENERAL-061."""
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "APP-GENERAL-061" in r.getMessage()
    ]


def _app061_debugs(caplog) -> List[logging.LogRecord]:
    """Return all DEBUG records containing APP-GENERAL-061."""
    return [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and "APP-GENERAL-061" in r.getMessage()
    ]


# ---------------------------------------------------------------------------
# DB / manager helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path) -> str:
    """Create a minimal SQLite DB with ci_tokens table."""
    path = str(tmp_path / "cidx_server.db")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ci_tokens ("
            "  platform TEXT PRIMARY KEY,"
            "  encrypted_token TEXT NOT NULL,"
            "  base_url TEXT"
            ")"
        )
        conn.commit()
    return path


def _make_server_dir(tmp_path, salt: str):
    """Create a server dir with .encryption_key_salt set to salt."""
    sd = tmp_path / ".cidx-server"
    sd.mkdir(exist_ok=True)
    (sd / ".encryption_key_salt").write_text(salt)
    return sd


def _insert_raw_token(db_path: str, platform: str, encrypted_token: str) -> None:
    """Insert a ci_token row directly (bypasses manager encryption)."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ci_tokens (platform, encrypted_token, base_url) "
            "VALUES (?, ?, ?)",
            (platform, encrypted_token, None),
        )
        conn.commit()


def _fresh_manager(server_dir, db_path):
    """Create a fresh CITokenManager via create_token_manager (mirrors production call sites)."""
    from src.code_indexer.server.services.ci_token_manager import create_token_manager

    return create_token_manager(
        server_dir=str(server_dir),
        db_path=db_path,
        storage_mode="postgres",
    )


# ---------------------------------------------------------------------------
# Autouse fixture: reset module-level memo before/after each test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_despam_memo() -> Generator[None, None, None]:
    """
    Clear the module-level de-spam memo before and after each test so tests
    are fully isolated from each other.
    """
    from src.code_indexer.server.services import ci_token_manager as _mod

    def _clear() -> None:
        if hasattr(_mod, "_DECRYPT_WARN_SEEN"):
            with _mod._DECRYPT_WARN_SEEN_LOCK:  # type: ignore[attr-defined]
                _mod._DECRYPT_WARN_SEEN.clear()  # type: ignore[attr-defined]

    _clear()
    yield
    _clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDecryptWarnDespam1222:
    """Tests for the APP-GENERAL-061 de-spam memo (Bug #1222)."""

    def test_first_call_emits_one_warning(self, tmp_path, caplog):
        """
        Test 1: First get_token on an undecryptable row emits exactly ONE APP-GENERAL-061
        WARNING and returns None.
        """
        db_path = _make_db(tmp_path)
        server_dir = _make_server_dir(tmp_path, _SALT_A)
        _insert_raw_token(db_path, "github", _BAD_CIPHERTEXT_1)

        manager = _fresh_manager(server_dir, db_path)

        with caplog.at_level(logging.WARNING):
            result = manager.get_token("github")

        assert result is None, "get_token must return None on undecryptable ciphertext"
        assert len(_app061_warnings(caplog)) == 1, (
            "Expected exactly 1 APP-GENERAL-061 WARNING on first call"
        )

    def test_second_call_same_ciphertext_no_warning(self, tmp_path, caplog):
        """
        Test 2: Second get_token on the SAME undecryptable ciphertext (even via a FRESH
        manager instance) emits ZERO additional WARNING records. Proves module-level memo
        works across instances.
        """
        db_path = _make_db(tmp_path)
        server_dir = _make_server_dir(tmp_path, _SALT_A)
        _insert_raw_token(db_path, "github", _BAD_CIPHERTEXT_1)

        # First call — primes the memo
        manager1 = _fresh_manager(server_dir, db_path)
        manager1.get_token("github")

        # Second call — fresh manager instance, same bad ciphertext
        caplog.clear()
        manager2 = _fresh_manager(server_dir, db_path)

        with caplog.at_level(logging.WARNING):
            result2 = manager2.get_token("github")

        assert result2 is None
        assert len(_app061_warnings(caplog)) == 0, (
            "Expected ZERO APP-GENERAL-061 WARNINGs on second call (same ciphertext)"
        )

    def test_different_ciphertext_emits_new_warning(self, tmp_path, caplog):
        """
        Test 3: A DIFFERENT undecryptable ciphertext for the same platform emits a new
        WARNING (distinct hash key → new entry in memo).
        """
        db_path = _make_db(tmp_path)
        server_dir = _make_server_dir(tmp_path, _SALT_A)

        # Prime memo with ciphertext 1
        _insert_raw_token(db_path, "github", _BAD_CIPHERTEXT_1)
        manager1 = _fresh_manager(server_dir, db_path)
        manager1.get_token("github")

        # Replace with a different bad ciphertext
        _insert_raw_token(db_path, "github", _BAD_CIPHERTEXT_2)

        caplog.clear()
        manager2 = _fresh_manager(server_dir, db_path)

        with caplog.at_level(logging.WARNING):
            result = manager2.get_token("github")

        assert result is None
        assert len(_app061_warnings(caplog)) == 1, (
            "Expected 1 new APP-GENERAL-061 WARNING for different ciphertext"
        )

    def test_success_clears_memo_so_new_bad_ciphertext_warns_again(
        self, tmp_path, caplog
    ):
        """
        Test 4: After a successful decrypt for a platform, a subsequent different bad
        ciphertext warns again (success clears the memo entry for that platform).
        """
        db_path = _make_db(tmp_path)
        server_dir = _make_server_dir(tmp_path, _SALT_A)

        # Step 1: insert bad ciphertext, call get_token → primes memo
        _insert_raw_token(db_path, "github", _BAD_CIPHERTEXT_1)
        manager1 = _fresh_manager(server_dir, db_path)
        manager1.get_token("github")

        # Step 2: save a GOOD token — this should succeed and clear the memo
        manager1.save_token("github", _GOOD_TOKEN)
        result_good = manager1.get_token("github")
        assert result_good is not None, "Good token save+get must succeed"
        assert result_good.token == _GOOD_TOKEN

        # Step 3: insert a NEW bad ciphertext (different from ciphertext 1)
        _insert_raw_token(db_path, "github", _BAD_CIPHERTEXT_2)

        caplog.clear()
        manager2 = _fresh_manager(server_dir, db_path)

        with caplog.at_level(logging.WARNING):
            result_bad = manager2.get_token("github")

        assert result_bad is None
        assert len(_app061_warnings(caplog)) == 1, (
            "Expected 1 WARNING after success cleared memo (new bad ciphertext)"
        )

    def test_concurrent_calls_same_bad_ciphertext_at_most_one_warning(
        self, tmp_path, caplog
    ):
        """
        Test 5: Concurrent get_token calls on the same bad ciphertext from multiple
        threads produce at most ONE APP-GENERAL-061 WARNING (thread-safety of the memo
        lock means no double-emission under race).
        """
        db_path = _make_db(tmp_path)
        server_dir = _make_server_dir(tmp_path, _SALT_A)
        _insert_raw_token(db_path, "github", _BAD_CIPHERTEXT_1)

        results = []

        def call_get_token():
            mgr = _fresh_manager(server_dir, db_path)
            return mgr.get_token("github")

        with caplog.at_level(logging.WARNING):
            with ThreadPoolExecutor(max_workers=_CONCURRENT_WORKERS) as executor:
                futures = [
                    executor.submit(call_get_token) for _ in range(_CONCURRENT_CALLS)
                ]
                for f in as_completed(futures):
                    results.append(f.result())

        assert all(r is None for r in results), "All concurrent calls must return None"
        assert len(_app061_warnings(caplog)) <= 1, (
            "Thread-safety: expected at most 1 WARNING under concurrent calls"
        )

    def test_second_call_may_emit_debug_not_warning(self, tmp_path, caplog):
        """
        Test 6: On the second call for the same undecryptable ciphertext, a DEBUG record
        (not WARNING) may be emitted. Verifies the log is downgraded, not silenced entirely.
        """
        db_path = _make_db(tmp_path)
        server_dir = _make_server_dir(tmp_path, _SALT_A)
        _insert_raw_token(db_path, "github", _BAD_CIPHERTEXT_1)

        # First call — primes memo
        manager1 = _fresh_manager(server_dir, db_path)
        with caplog.at_level(logging.DEBUG):
            manager1.get_token("github")

        caplog.clear()
        manager2 = _fresh_manager(server_dir, db_path)

        with caplog.at_level(logging.DEBUG):
            manager2.get_token("github")

        # No WARNING on second call
        assert len(_app061_warnings(caplog)) == 0, (
            "Second call must NOT emit WARNING for same ciphertext"
        )

        # A DEBUG record should exist (the downgraded log)
        assert len(_app061_debugs(caplog)) >= 1, (
            "Second call should emit at least one DEBUG record for APP-GENERAL-061 "
            "(downgraded from WARNING)"
        )
