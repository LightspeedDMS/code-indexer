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
from typing import List, Optional

import pyotp
import qrcode

from cryptography.fernet import Fernet, InvalidToken

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

        self._ensure_tables()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

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

        # Check replay: get last used counter
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT last_used_counter FROM user_mfa WHERE user_id = ?",
                (username,),
            ).fetchone()
            last_counter = (
                row["last_used_counter"] if row and row["last_used_counter"] else 0
            )

            # Verify with window tolerance
            if not totp.verify(code, valid_window=self._window_tolerance):
                return False

            # Check replay: the code's time step must be > last_used_counter
            # Find which counter step matches this code
            for offset in range(-self._window_tolerance, self._window_tolerance + 1):
                test_counter = current_counter + offset
                if totp.at(test_counter * _TOTP_PERIOD) == code:
                    if test_counter <= last_counter:
                        return False  # Replay detected
                    # Update last_used_counter
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
        """Verify and consume a recovery code.

        Returns True if valid. The used code is marked consumed (not deleted)
        so it can't be reused.
        """
        code_hash = self._hash_recovery_code(code)
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT id FROM user_recovery_codes WHERE user_id = ? AND code_hash = ? AND used_at IS NULL",
                (username, code_hash),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "UPDATE user_recovery_codes SET used_at = CURRENT_TIMESTAMP, used_ip = ? WHERE id = ?",
                (ip_address, row["id"]),
            )
            conn.commit()
            # Count remaining
            remaining = conn.execute(
                "SELECT COUNT(*) as cnt FROM user_recovery_codes WHERE user_id = ? AND used_at IS NULL",
                (username,),
            ).fetchone()["cnt"]
            logger.info(
                "Recovery code used for %s. %d codes remaining.", username, remaining
            )
            return True
        finally:
            conn.close()

    def regenerate_recovery_codes(self, username: str) -> List[str]:
        """Delete all existing recovery codes and generate new set.

        Does NOT change the TOTP seed.
        """
        return self.generate_recovery_codes(username)

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
