"""The SecOps service-account credential: validation on save and encrypted,
write-only storage in the SIEM database (cluster-wide: SQLite solo,
PostgreSQL cluster; table ``siem_delivery_credential``, one row).

Encryption reuses the server's stored-secret mechanism, the same one the
CI-token and git-credential managers use (``services/token_encryption.py``:
AES-256-CBC, key derived from ``.encryption_key_salt``).  No new crypto.

Write-only: nothing here returns, logs or audits key material.  Callers get
the non-secret identity only (``client_email``, ``private_key_id``, who set
it and when).  Validation errors name the problem, never a value.
"""

from __future__ import annotations

import binascii
import hashlib
import hmac
import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from code_indexer.server.services.siem_delivery import state_store
from code_indexer.server.services.siem_delivery.db import Dialect, SiemDb, SiemTx
from code_indexer.server.services.siem_delivery.destination import (
    SECOPS_SCOPE,
    SECOPS_TOKEN_URI,
    SiemConfigInvalid,
    parse_harness_endpoint,
)
from code_indexer.server.services.siem_delivery.sender import (
    CredentialError,
    ProbeResult,
)
from code_indexer.server.services.token_encryption import (
    decrypt_single,
    encrypt_token,
)

logger = logging.getLogger(__name__)

MAX_CREDENTIAL_JSON_BYTES = 64 * 1024
_KEY_CHECK_LABEL = b"siem-credential"
REQUIRED_FIELDS = ("client_email", "private_key", "private_key_id", "token_uri")
_TOKEN_PATH = "/token"
_CLIENT_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,128}@[A-Za-z0-9.-]{1,125}$")
_PRIVATE_KEY_ID_RE = re.compile(r"^[A-Za-z0-9]{1,128}$")
_IDENTITY_COLUMNS = "credential_id, client_email, private_key_id, set_by, set_at"


class SiemCredentialInvalid(ValueError):
    """A submitted service-account key is unusable; the reason never
    carries a value from the key."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"service account key rejected: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class StoredCredential:
    """The decrypted key (in memory only) and its storage identity."""

    credential_id: str
    info: Dict[str, Any]


def _harness_token_uri(uri: str) -> bool:
    if not uri.endswith(_TOKEN_PATH):
        return False
    try:
        parse_harness_endpoint(uri[: -len(_TOKEN_PATH)])
    except SiemConfigInvalid:
        return False
    return True


def _check_fields(info: Dict[str, Any]) -> None:
    if info.get("type") != "service_account":
        raise SiemCredentialInvalid('"type" must be "service_account"')
    for name in REQUIRED_FIELDS:
        value = info.get(name)
        if not isinstance(value, str) or not value.strip():
            raise SiemCredentialInvalid(f"required field {name} is missing")
    if not _CLIENT_EMAIL_RE.match(info["client_email"]):
        raise SiemCredentialInvalid("client_email is not a service-account address")
    if not _PRIVATE_KEY_ID_RE.match(info["private_key_id"]):
        raise SiemCredentialInvalid("private_key_id is not a key id")


def _check_token_uri(uri: str, harness_active: bool) -> None:
    if uri == SECOPS_TOKEN_URI:
        return
    if harness_active and _harness_token_uri(uri):
        return
    raise SiemCredentialInvalid("token_uri is not the allowed token endpoint")


def _check_private_key(info: Dict[str, Any]) -> None:
    """The key must load and be RSA (google-auth signs RS256 only)."""
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from code_indexer.server.services.siem_delivery.transport import (
        import_google_auth,
    )

    try:
        key = serialization.load_pem_private_key(
            info["private_key"].encode("utf-8"), password=None
        )
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise SiemCredentialInvalid("the private key does not load") from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise SiemCredentialInvalid("the private key is not an RSA key")
    google = import_google_auth()
    try:
        google.oauth2.service_account.Credentials.from_service_account_info(
            info, scopes=[SECOPS_SCOPE]
        )
    except (ValueError, TypeError, KeyError):
        raise SiemCredentialInvalid("the private key does not load") from None


def validate_service_account_json(text: str, *, harness_active: bool) -> Dict[str, Any]:
    """The parsed key when usable for SIEM delivery, else raise
    :class:`SiemCredentialInvalid`.  The token URI must be Google's token
    endpoint (or, only behind the fault-injection gate, a loopback
    ``<harness origin>/token``)."""
    if len(text.encode("utf-8")) > MAX_CREDENTIAL_JSON_BYTES:
        raise SiemCredentialInvalid("the key is larger than 64 KiB")
    try:
        info = json.loads(text)
    except ValueError:
        raise SiemCredentialInvalid("the key is not valid JSON") from None
    if not isinstance(info, dict):
        raise SiemCredentialInvalid("the key is not a JSON object")
    _check_fields(info)
    _check_token_uri(info["token_uri"], harness_active)
    _check_private_key(info)
    return info


def _identity(row: Dict[str, Any]) -> Dict[str, Any]:
    parsed = Dialect.parse_ts(row["set_at"])
    return {
        "client_email": str(row["client_email"]),
        "private_key_id": str(row["private_key_id"]),
        "set_by": str(row["set_by"]),
        "set_at": parsed.isoformat() if parsed is not None else None,
    }


class SiemCredentialStore:
    """The one stored credential (row id 1), encrypted at rest."""

    def __init__(self, db: SiemDb, encryption_key: bytes) -> None:
        self.db = db
        self._key_provider: Callable[[], bytes] = lambda: encryption_key
        self._derived: Optional[Tuple[bytes, str]] = None
        self._derive_lock = threading.Lock()
        self._mismatch_logged = False  # per-process log de-dup

    @classmethod
    def lazy(
        cls, db: SiemDb, key_provider: Callable[[], bytes]
    ) -> "SiemCredentialStore":
        """A store whose key is derived on FIRST USE (a scheduler or worker
        thread), never at construction (which runs on the startup path)."""
        store = cls(db, b"")
        store._key_provider = key_provider
        return store

    def _key_material(self) -> Tuple[bytes, str]:
        """(encryption key, key-check), derived once per process."""
        with self._derive_lock:
            if self._derived is None:
                key = self._key_provider()
                check = hmac.new(key, _KEY_CHECK_LABEL, hashlib.sha256).hexdigest()
                self._derived = (key, check)
            return self._derived

    def _report_key_mismatch(self) -> None:
        if not self._mismatch_logged:
            self._mismatch_logged = True
            logger.warning(
                "SIEM delivery: the stored service-account key was encrypted with "
                "a different encryption key than this process derives (cluster: "
                "the shared JWT secret was rotated or differs; solo: the "
                ".encryption_key_salt changed). Re-upload the key in the Web UI."
            )
        raise CredentialError(ProbeResult.CREDENTIAL_KEY_MISMATCH)

    def set(self, info: Dict[str, Any], *, actor: str) -> Tuple[str, Dict[str, Any]]:
        """Store a VALIDATED key; ``("set" | "replaced", identity)``."""
        key, key_check = self._key_material()
        ciphertext = encrypt_token(json.dumps(info), key)

        def _do(tx: SiemTx) -> Tuple[str, Dict[str, Any]]:
            state_store.state_in(tx, lock=True)  # serialise credential changes
            prior = tx.one("SELECT id FROM siem_delivery_credential WHERE id = 1")
            tx.execute(
                "INSERT INTO siem_delivery_credential (id, credential_id, "
                "encrypted_key, key_check, client_email, private_key_id, set_by, "
                "set_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE "
                "SET credential_id = excluded.credential_id, "
                "encrypted_key = excluded.encrypted_key, "
                "key_check = excluded.key_check, "
                "client_email = excluded.client_email, "
                "private_key_id = excluded.private_key_id, "
                "set_by = excluded.set_by, set_at = excluded.set_at",
                (
                    uuid.uuid4().hex,
                    ciphertext,
                    key_check,
                    info["client_email"],
                    info["private_key_id"],
                    actor,
                    tx.ts(tx.now()),
                ),
            )
            row = tx.one(
                f"SELECT {_IDENTITY_COLUMNS} FROM siem_delivery_credential WHERE id = 1"
            )
            assert row is not None
            if prior:
                # Bug #2018: the canary proved the REPLACED key
                state_store.invalidate_canary(tx)
            return ("replaced" if prior else "set"), _identity(row)

        result: Tuple[str, Dict[str, Any]] = self.db.write(_do, phase="credential")
        return result

    def remove(self, *, actor: str) -> Optional[Dict[str, Any]]:
        """Delete the stored key; its identity, or None when none was stored."""

        def _do(tx: SiemTx) -> Optional[Dict[str, Any]]:
            state_store.state_in(tx, lock=True)
            row = tx.one(
                f"SELECT {_IDENTITY_COLUMNS} FROM siem_delivery_credential WHERE id = 1"
            )
            if row is None:
                return None
            tx.execute("DELETE FROM siem_delivery_credential WHERE id = 1")
            state_store.invalidate_canary(tx)  # Bug #2018
            return _identity(row)

        removed: Optional[Dict[str, Any]] = self.db.write(_do, phase="credential")
        return removed

    def credential_id(self) -> Optional[str]:
        """The stored key's storage id (new on every set), None when none."""
        row = self.db.read(
            lambda tx: tx.one(
                "SELECT credential_id FROM siem_delivery_credential WHERE id = 1"
            )
        )
        return str(row["credential_id"]) if row is not None else None

    def identity(self) -> Optional[Dict[str, Any]]:
        row = self.db.read(
            lambda tx: tx.one(
                f"SELECT {_IDENTITY_COLUMNS} FROM siem_delivery_credential WHERE id = 1"
            )
        )
        return _identity(row) if row is not None else None

    def load(self) -> Optional[StoredCredential]:
        """The decrypted key, None when none is stored; raises
        :class:`CredentialError`: ``credential_key_mismatch`` when the row
        was encrypted under another key, ``credential_invalid`` when it
        cannot be decrypted or parsed under this one."""
        row = self.db.read(
            lambda tx: tx.one(
                "SELECT credential_id, encrypted_key, key_check "
                "FROM siem_delivery_credential WHERE id = 1"
            )
        )
        if row is None:
            return None
        key, key_check = self._key_material()
        if not hmac.compare_digest(str(row["key_check"]), key_check):
            self._report_key_mismatch()
        try:
            info = json.loads(decrypt_single(str(row["encrypted_key"]), key))
        except (ValueError, binascii.Error, UnicodeDecodeError):
            raise CredentialError(ProbeResult.CREDENTIAL_INVALID) from None
        if not isinstance(info, dict):
            raise CredentialError(ProbeResult.CREDENTIAL_INVALID)
        return StoredCredential(str(row["credential_id"]), info)
