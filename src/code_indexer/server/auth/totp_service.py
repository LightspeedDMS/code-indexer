"""
TOTP MFA Core Engine (Story #558).

Provides secret generation, TOTP verification with replay prevention,
recovery code management with HMAC-SHA256, QR code generation, and
Fernet encryption for secrets at rest.
"""

import hashlib
import hmac
import io
import logging
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, List, Optional

import pyotp
import qrcode

from cryptography.fernet import Fernet, InvalidToken

try:
    from psycopg.rows import dict_row
except ImportError:  # psycopg3 not installed (standalone mode)
    dict_row = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

# TOTP parameters (fixed per RFC 6238 for authenticator app compatibility)
_TOTP_DIGITS = 6
_TOTP_PERIOD = 30
_TOTP_ISSUER = "CIDX"


class TOTPService:
    """Core TOTP MFA engine.

    Handles secret generation, code verification, recovery codes,
    and encryption. No UI — consumed by login handlers.
    """

    def __init__(
        self,
        db_path: str,
        mfa_encryption_key: Optional[str] = None,
        window_tolerance: int = 1,
        recovery_code_count: int = 10,
    ) -> None:
        self._db_path = db_path
        self._window_tolerance = window_tolerance
        self._recovery_code_count = recovery_code_count

        # Initialize or load encryption key.
        # Priority: explicit param > key file > auto-generate + persist
        if mfa_encryption_key:
            self._fernet = Fernet(mfa_encryption_key.encode())
            self._key_id = 1
        else:
            key_file = Path(db_path).parent / "mfa_key.dat"
            if key_file.exists():
                stored_key = key_file.read_text().strip()
                self._fernet = Fernet(stored_key.encode())
                self._key_id = 1
                logger.info("TOTPService: loaded MFA encryption key from %s", key_file)
            else:
                key = Fernet.generate_key()
                self._fernet = Fernet(key)
                self._key_id = 1
                key_file.write_text(key.decode())
                os.chmod(str(key_file), 0o600)
                logger.info(
                    "TOTPService: generated and persisted MFA encryption key to %s",
                    key_file,
                )

        # Cluster mode: PostgreSQL connection pool (set via set_connection_pool)
        self._pool: Optional[Any] = None

        self._ensure_tables()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def set_connection_pool(self, pool: Any) -> None:
        """Set PostgreSQL connection pool for cluster mode.

        When set, all database operations use PostgreSQL instead of SQLite,
        and the MFA encryption key is stored in the cluster_secrets table
        so all cluster nodes share the same key.
        """
        self._pool = pool
        self._load_or_create_cluster_key()
        logger.info("TOTPService: using PostgreSQL connection pool (cluster mode)")

    def _load_or_create_cluster_key(self) -> None:
        """Load or create MFA encryption key in cluster_secrets table.

        Race-condition safe: uses INSERT ... ON CONFLICT DO NOTHING,
        then re-reads to get the winning value.
        """
        assert self._pool is not None
        with self._pool.connection() as conn:
            conn.row_factory = dict_row
            # Try to read existing key
            row = conn.execute(
                "SELECT key_value FROM cluster_secrets WHERE key_name = %s",
                ("mfa_encryption_key",),
            ).fetchone()
            if row:
                key = row["key_value"] if isinstance(row, dict) else row[0]
                self._fernet = Fernet(key.encode())
                self._key_id = 1
                logger.info(
                    "TOTPService: loaded MFA encryption key from cluster_secrets"
                )
                return

            # Generate new key and insert (race-safe)
            new_key = Fernet.generate_key().decode()
            conn.execute(
                """
                INSERT INTO cluster_secrets (key_name, key_value)
                VALUES (%s, %s)
                ON CONFLICT (key_name) DO NOTHING
                """,
                ("mfa_encryption_key", new_key),
            )
            conn.commit()

            # Re-read the winner
            row = conn.execute(
                "SELECT key_value FROM cluster_secrets WHERE key_name = %s",
                ("mfa_encryption_key",),
            ).fetchone()
            key = row["key_value"] if isinstance(row, dict) else row[0]
            self._fernet = Fernet(key.encode())
            self._key_id = 1
            logger.info(
                "TOTPService: generated and stored MFA encryption key in cluster_secrets"
            )

    def _ensure_tables(self) -> None:
        """Create MFA tables if they don't exist."""
        conn = self._get_conn()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS user_mfa (
                    user_id TEXT UNIQUE NOT NULL,
                    encrypted_secret TEXT NOT NULL,
                    key_id INTEGER DEFAULT 1,
                    mfa_enabled BOOLEAN DEFAULT 0,
                    last_used_counter INTEGER,
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
            conn.commit()
        finally:
            conn.close()
        # Add elevation replay-prevention column if missing.
        # SQLite solo: applied here (idempotent ALTER TABLE).
        # PostgreSQL cluster: applied by migration 023_totp_replay_prevention.sql.
        if self._pool is None:
            self._ensure_last_used_otp_counter_column()

    def _ensure_last_used_otp_counter_column(self) -> None:
        """Add last_used_otp_counter to user_mfa if absent (SQLite, idempotent).

        Stores int(unix_time // 30) — the TOTP time-step index — for CAS
        replay prevention in the elevation flow (Story #923 AC9).
        """
        conn = self._get_conn()
        try:
            existing = {
                row[1] for row in conn.execute("PRAGMA table_info(user_mfa)").fetchall()
            }
            if "last_used_otp_counter" not in existing:
                conn.execute(
                    "ALTER TABLE user_mfa ADD COLUMN last_used_otp_counter INTEGER"
                )
                conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Secret Management
    # ------------------------------------------------------------------

    def generate_secret(self, username: str) -> str:
        """Generate and store a new TOTP secret for a user.

        Returns the plaintext base32 secret (for QR code display).
        The secret is stored encrypted in the database.
        mfa_enabled remains False until activate_mfa() is called.
        """
        secret = pyotp.random_base32()
        encrypted = self._fernet.encrypt(secret.encode()).decode()

        if self._pool is not None:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO user_mfa (user_id, encrypted_secret, key_id, mfa_enabled)
                    VALUES (%s, %s, %s, FALSE)
                    ON CONFLICT(user_id) DO UPDATE SET
                        encrypted_secret = excluded.encrypted_secret,
                        key_id = excluded.key_id,
                        mfa_enabled = FALSE,
                        last_used_counter = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (username, encrypted, self._key_id),
                )
                conn.commit()
        else:
            conn = self._get_conn()
            try:
                conn.execute(
                    """
                    INSERT INTO user_mfa (user_id, encrypted_secret, key_id, mfa_enabled)
                    VALUES (?, ?, ?, 0)
                    ON CONFLICT(user_id) DO UPDATE SET
                        encrypted_secret = excluded.encrypted_secret,
                        key_id = excluded.key_id,
                        mfa_enabled = 0,
                        last_used_counter = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (username, encrypted, self._key_id),
                )
                conn.commit()
            finally:
                conn.close()

        return str(secret)

    def _get_secret(self, username: str) -> Optional[str]:
        """Retrieve and decrypt the TOTP secret for a user."""
        if self._pool is not None:
            with self._pool.connection() as conn:
                conn.row_factory = dict_row
                row = conn.execute(
                    "SELECT encrypted_secret FROM user_mfa WHERE user_id = %s",
                    (username,),
                ).fetchone()
                if row is None:
                    return None
                try:
                    return str(
                        self._fernet.decrypt(row["encrypted_secret"].encode()).decode()
                    )
                except InvalidToken:
                    logger.error("Failed to decrypt TOTP secret for %s", username)
                    return None
        else:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT encrypted_secret FROM user_mfa WHERE user_id = ?",
                    (username,),
                ).fetchone()
                if row is None:
                    return None
                try:
                    return str(
                        self._fernet.decrypt(row["encrypted_secret"].encode()).decode()
                    )
                except InvalidToken:
                    logger.error("Failed to decrypt TOTP secret for %s", username)
                    return None
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Provisioning URI & QR Code
    # ------------------------------------------------------------------

    def get_provisioning_uri(self, username: str) -> Optional[str]:
        """Return otpauth:// URI for authenticator app enrollment."""
        secret = self._get_secret(username)
        if secret is None:
            return None
        totp = pyotp.TOTP(secret, digits=_TOTP_DIGITS, interval=_TOTP_PERIOD)
        return str(totp.provisioning_uri(name=username, issuer_name=_TOTP_ISSUER))

    def generate_qr_code(self, uri: str) -> bytes:
        """Generate a PNG QR code image from a provisioning URI."""
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(uri)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return buffer.getvalue()

    # ------------------------------------------------------------------
    # TOTP Verification
    # ------------------------------------------------------------------

    def verify_code(self, username: str, code: str) -> bool:
        """Verify a TOTP code with replay prevention.

        Returns True if the code is valid and not replayed.
        Updates last_used_counter on success.
        """
        secret = self._get_secret(username)
        if secret is None:
            return False

        totp = pyotp.TOTP(secret, digits=_TOTP_DIGITS, interval=_TOTP_PERIOD)

        # Get current time step
        current_counter = int(time.time()) // _TOTP_PERIOD

        # Check replay: get last used counter and verify
        if self._pool is not None:
            return self._verify_code_with_conn_pg(username, code, totp, current_counter)
        return self._verify_code_with_conn_sqlite(username, code, totp, current_counter)

    def _verify_code_with_conn_pg(
        self, username: str, code: str, totp: "pyotp.TOTP", current_counter: int
    ) -> bool:
        """Verify TOTP code using PostgreSQL connection."""
        assert self._pool is not None
        with self._pool.connection() as conn:
            conn.row_factory = dict_row
            row = conn.execute(
                "SELECT last_used_counter FROM user_mfa WHERE user_id = %s",
                (username,),
            ).fetchone()
            last_counter = (
                row["last_used_counter"] if row and row["last_used_counter"] else 0
            )

            if not totp.verify(code, valid_window=self._window_tolerance):
                return False

            for offset in range(-self._window_tolerance, self._window_tolerance + 1):
                test_counter = current_counter + offset
                if totp.at(test_counter * _TOTP_PERIOD) == code:
                    if test_counter <= last_counter:
                        return False
                    conn.execute(
                        "UPDATE user_mfa SET last_used_counter = %s, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s",
                        (test_counter, username),
                    )
                    conn.commit()
                    return True

            return False

    def _verify_code_with_conn_sqlite(
        self, username: str, code: str, totp: "pyotp.TOTP", current_counter: int
    ) -> bool:
        """Verify TOTP code using SQLite connection."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT last_used_counter FROM user_mfa WHERE user_id = ?",
                (username,),
            ).fetchone()
            last_counter = (
                row["last_used_counter"] if row and row["last_used_counter"] else 0
            )

            if not totp.verify(code, valid_window=self._window_tolerance):
                return False

            for offset in range(-self._window_tolerance, self._window_tolerance + 1):
                test_counter = current_counter + offset
                if totp.at(test_counter * _TOTP_PERIOD) == code:
                    if test_counter <= last_counter:
                        return False
                    conn.execute(
                        "UPDATE user_mfa SET last_used_counter = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                        (test_counter, username),
                    )
                    conn.commit()
                    return True

            return False
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Recovery Codes
    # ------------------------------------------------------------------

    def generate_recovery_codes(self, username: str) -> List[str]:
        """Generate recovery codes, store HMAC-SHA256 hashes, return plaintext.

        Deletes any existing recovery codes for this user first.
        """
        codes = []
        for _ in range(self._recovery_code_count):
            # Generate 16 random alphanumeric chars, format as XXXX-XXXX-XXXX-XXXX
            raw = secrets.token_hex(8).upper()[:16]
            code = f"{raw[:4]}-{raw[4:8]}-{raw[8:12]}-{raw[12:16]}"
            codes.append(code)

        # Hash and store
        if self._pool is not None:
            with self._pool.connection() as conn:
                conn.execute(
                    "DELETE FROM user_recovery_codes WHERE user_id = %s", (username,)
                )
                for code in codes:
                    code_hash = self._hash_recovery_code(code)
                    conn.execute(
                        "INSERT INTO user_recovery_codes (user_id, code_hash) VALUES (%s, %s)",
                        (username, code_hash),
                    )
                conn.commit()
        else:
            conn = self._get_conn()
            try:
                conn.execute(
                    "DELETE FROM user_recovery_codes WHERE user_id = ?", (username,)
                )
                for code in codes:
                    code_hash = self._hash_recovery_code(code)
                    conn.execute(
                        "INSERT INTO user_recovery_codes (user_id, code_hash) VALUES (?, ?)",
                        (username, code_hash),
                    )
                conn.commit()
            finally:
                conn.close()

        return codes

    def verify_recovery_code(
        self, username: str, code: str, ip_address: str = "unknown"
    ) -> bool:
        """Atomically verify and consume a recovery code (Codex AC10 + Codex M1).

        Single conditional UPDATE prevents TOCTOU race where two concurrent
        requests could both consume the same unused code.

        Returns True if valid. The used code is marked consumed (not deleted)
        so it can't be reused.
        """
        code_hash = self._hash_recovery_code(code)
        if self._pool is not None:
            with self._pool.connection() as conn:
                cursor = conn.execute(
                    """
                    UPDATE user_recovery_codes
                    SET used_at = CURRENT_TIMESTAMP, used_ip = %s
                    WHERE user_id = %s
                      AND code_hash = %s
                      AND used_at IS NULL
                    """,
                    (ip_address, username, code_hash),
                )
                conn.commit()
                if cursor.rowcount == 0:
                    return False
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM user_recovery_codes WHERE user_id = %s AND used_at IS NULL",
                    (username,),
                ).fetchone()[0]
                logger.info(
                    "Recovery code used for %s. %d codes remaining.",
                    username,
                    remaining,
                )
                return True
        else:
            conn = self._get_conn()
            try:
                cursor = conn.execute(
                    """
                    UPDATE user_recovery_codes
                    SET used_at = CURRENT_TIMESTAMP, used_ip = ?
                    WHERE user_id = ?
                      AND code_hash = ?
                      AND used_at IS NULL
                    """,
                    (ip_address, username, code_hash),
                )
                conn.commit()
                if cursor.rowcount == 0:
                    return False
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM user_recovery_codes WHERE user_id = ? AND used_at IS NULL",
                    (username,),
                ).fetchone()[0]
                logger.info(
                    "Recovery code used for %s. %d codes remaining.",
                    username,
                    remaining,
                )
                return True
            finally:
                conn.close()

    def regenerate_recovery_codes(self, username: str) -> List[str]:
        """Delete all existing recovery codes and generate new set.

        Does NOT change the TOTP seed.
        """
        return self.generate_recovery_codes(username)

    # ------------------------------------------------------------------
    # Elevation-specific TOTP verification (Story #923 AC9)
    # ------------------------------------------------------------------

    def verify_enabled_code(self, username: str, code: str) -> bool:
        """Verify a TOTP code only when MFA is fully enabled, with CAS replay guard.

        Unlike verify_code(), this method:
        - Rejects codes that are not exactly _TOTP_DIGITS decimal digits.
        - Returns False immediately if MFA is not enabled for the user.
        - Finds the exact TOTP time-step that produced the submitted code,
          then atomically records that step in last_used_otp_counter so that
          a second call with the same code (same time-step) is rejected.

        Args:
            username: Non-empty admin username.
            code: Exactly 6 decimal digits from the authenticator app.

        Returns:
            True if MFA is enabled, code is valid, and the matched time-step
            has not been used before for elevation.  False otherwise.

        Raises:
            ValueError: If username is blank or code is not 6 decimal digits.
        """
        if not isinstance(username, str) or not username.strip():
            raise ValueError("username must be a non-empty string")
        if not isinstance(code, str) or not (
            len(code) == _TOTP_DIGITS and code.isdigit()
        ):
            raise ValueError(
                f"code must be exactly {_TOTP_DIGITS} decimal digits, got {code!r}"
            )

        if not self.is_mfa_enabled(username):
            return False

        secret = self._get_secret(username)
        if secret is None:
            return False

        totp = pyotp.TOTP(secret, digits=_TOTP_DIGITS, interval=_TOTP_PERIOD)

        # Find the specific time-step that produced this code so the CAS
        # guard stores the exact matched window, not the current server window.
        base_step = int(time.time()) // _TOTP_PERIOD
        matched_step: Optional[int] = None
        for offset in range(-self._window_tolerance, self._window_tolerance + 1):
            candidate = base_step + offset
            if totp.at(candidate * _TOTP_PERIOD) == code:
                matched_step = candidate
                break

        if matched_step is None:
            return False

        if self._pool is not None:
            return self._cas_otp_counter_pg(username, matched_step)
        return self._cas_otp_counter_sqlite(username, matched_step)

    def _cas_otp_counter_sqlite(self, username: str, step: int) -> bool:
        """CAS update last_used_otp_counter if step is newer (SQLite)."""
        conn = None
        try:
            conn = self._get_conn()
            conn.execute("BEGIN EXCLUSIVE")
            row = conn.execute(
                "SELECT last_used_otp_counter FROM user_mfa WHERE user_id = ?",
                (username,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return False
            stored = row["last_used_otp_counter"]
            if stored is not None and stored >= step:
                conn.rollback()
                return False
            conn.execute(
                "UPDATE user_mfa SET last_used_otp_counter = ? WHERE user_id = ?",
                (step, username),
            )
            conn.commit()
            return True
        finally:
            if conn:
                conn.close()

    def _cas_otp_counter_pg(self, username: str, step: int) -> bool:
        """CAS update last_used_otp_counter if step is newer (PostgreSQL).

        Uses cursor.rowcount instead of RETURNING so the same SQL works
        against the SQLite-backed test harness (SQLite < 3.35 lacks RETURNING).
        """
        assert self._pool is not None
        with self._pool.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE user_mfa
                   SET last_used_otp_counter = %s
                 WHERE user_id = %s
                   AND (last_used_otp_counter IS NULL OR last_used_otp_counter < %s)
                """,
                (step, username, step),
            )
            conn.commit()
            return bool(cursor.rowcount > 0)

    def _hash_recovery_code(self, code: str) -> str:
        """HMAC-SHA256 hash of a recovery code using the encryption key as pepper."""
        key_bytes = self._fernet._signing_key  # type: ignore[attr-defined]
        return hmac.new(key_bytes, code.encode(), hashlib.sha256).hexdigest()

    # ------------------------------------------------------------------
    # MFA Lifecycle
    # ------------------------------------------------------------------

    def activate_mfa(self, username: str, verification_code: str) -> bool:
        """Verify a TOTP code and activate MFA for the user.

        Returns True if activation succeeded.
        """
        if not self.verify_code(username, verification_code):
            return False

        if self._pool is not None:
            with self._pool.connection() as conn:
                conn.execute(
                    "UPDATE user_mfa SET mfa_enabled = TRUE, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s",
                    (username,),
                )
                conn.commit()
        else:
            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE user_mfa SET mfa_enabled = 1, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                    (username,),
                )
                conn.commit()
            finally:
                conn.close()

        logger.info("MFA activated for user %s", username)
        return True

    def disable_mfa(self, username: str) -> None:
        """Disable MFA and remove all MFA data for a user."""
        if self._pool is not None:
            with self._pool.connection() as conn:
                conn.execute("DELETE FROM user_mfa WHERE user_id = %s", (username,))
                conn.execute(
                    "DELETE FROM user_recovery_codes WHERE user_id = %s", (username,)
                )
                conn.commit()
        else:
            conn = self._get_conn()
            try:
                conn.execute("DELETE FROM user_mfa WHERE user_id = ?", (username,))
                conn.execute(
                    "DELETE FROM user_recovery_codes WHERE user_id = ?", (username,)
                )
                conn.commit()
            finally:
                conn.close()

        logger.info("MFA disabled for user %s", username)

    def is_mfa_enabled(self, username: str) -> bool:
        """Check if MFA is enabled for a user."""
        if self._pool is not None:
            with self._pool.connection() as conn:
                conn.row_factory = dict_row
                row = conn.execute(
                    "SELECT mfa_enabled FROM user_mfa WHERE user_id = %s",
                    (username,),
                ).fetchone()
                return bool(row and row["mfa_enabled"])
        else:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT mfa_enabled FROM user_mfa WHERE user_id = ?",
                    (username,),
                ).fetchone()
                return bool(row and row["mfa_enabled"])
            finally:
                conn.close()

    def get_manual_entry_key(self, username: str) -> Optional[str]:
        """Return the base32 secret formatted for manual entry."""
        secret = self._get_secret(username)
        if secret is None:
            return None
        # Format as groups of 4 for readability
        return " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))
