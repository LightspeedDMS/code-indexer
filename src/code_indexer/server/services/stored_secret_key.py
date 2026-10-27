"""The random key that encrypts stored secrets (git credentials, CI tokens,
LLM lease state).

Solo (SQLite): 32 random bytes, Fernet-encoded, in
``server_dir/.token_encryption_key`` (mode 0600). The file is created once,
atomically and exclusively: a complete temporary file is hard-linked into
place, so a concurrent creator that loses reads the winner's complete key
and an existing file is never overwritten. An unreadable or corrupt file
fails loudly -- regenerating it would orphan every stored credential.

Cluster (PostgreSQL): the key is the ``token_encryption_key`` row of
``cluster_secrets``, created race-safely (see ``cluster_key_provider``), so
every node uses the same key.

The key must be backed up together with the database: a restore without it cannot decrypt
stored credentials.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import secrets
import stat
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, cast

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

KEY_FILENAME = ".token_encryption_key"
CLUSTER_KEY_NAME = "token_encryption_key"
TOKEN_KEY_CAPABILITY = "token_key_v2"
# Capabilities this node version advertises in cluster_nodes.metadata.
NODE_CAPABILITIES: List[str] = [TOKEN_KEY_CAPABILITY]
_KEY_BYTES = 32


class StoredSecretKeyError(RuntimeError):
    """The stored-secret key exists but cannot be used (unreadable or
    corrupt). Never resolved by generating a new key."""


def validate_key(raw: bytes, source: str) -> bytes:
    """Return *raw* (stripped) when it is a Fernet key of 32 bytes."""
    key = raw.strip()
    try:
        decoded = base64.urlsafe_b64decode(key)
    except (binascii.Error, ValueError):
        raise StoredSecretKeyError(
            f"stored-secret key in {source} is corrupt"
        ) from None
    if len(decoded) != _KEY_BYTES:
        raise StoredSecretKeyError(f"stored-secret key in {source} is corrupt")
    return key


def _create_exclusively(path: Path) -> None:
    """Hard-link a complete new key file into *path* unless one exists."""
    from code_indexer.server.services.locked_secret_file import fsync_dir, write_all
    from code_indexer.utils.file_locking import nfs_safe_link_exclusive

    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            write_all(fd, Fernet.generate_key() + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            nfs_safe_link_exclusive(tmp, path)
        except FileExistsError:
            pass  # a concurrent creator won; its key is read below
    finally:
        os.unlink(tmp)
    fsync_dir(path.parent)


def load_or_create_key_file(server_dir: Path) -> bytes:
    """The solo stored-secret key, created on first use.

    Raises:
        StoredSecretKeyError: the key file exists but is unreadable or corrupt.
    """
    directory = Path(server_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / KEY_FILENAME
    if not os.path.lexists(path):
        _create_exclusively(path)
    return validate_key(_read_checked(path), str(path))


_KEY_FILE_READ_LIMIT = 4096


def _read_checked(path: Path) -> bytes:
    """The key file content, after checking it is a regular file (not a
    symlink) owned by this user; group/other access is removed."""
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise StoredSecretKeyError(
                f"stored-secret key file {path} is not a regular file"
            )
        if st.st_uid != os.getuid():
            raise StoredSecretKeyError(
                f"stored-secret key file {path} is not owned by the server user"
            )
        if stat.S_IMODE(st.st_mode) & 0o077:
            os.chmod(path, 0o600)
            logger.warning(
                "stored-secret key file %s was readable by other users; "
                "permissions reset to 0600",
                path,
            )
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            return os.read(fd, _KEY_FILE_READ_LIMIT)
        finally:
            os.close(fd)
    except OSError as exc:
        raise StoredSecretKeyError(
            f"stored-secret key file {path} is unreadable ({type(exc).__name__})"
        ) from None


def load_or_create_cluster_key(pool: Any) -> bytes:
    """The cluster stored-secret key (``cluster_secrets`` row, race-safe)."""
    from code_indexer.server.services.cluster_key_provider import (
        load_or_create_cluster_secret,
    )

    value = load_or_create_cluster_secret(
        pool, CLUSTER_KEY_NAME, lambda: Fernet.generate_key().decode("ascii")
    )
    return validate_key(value.encode("ascii"), f"cluster_secrets.{CLUSTER_KEY_NAME}")


# Every node seen within this window keeps the gate closed until it advertises
# the capability, so a paused or briefly stopped older node is never ignored.
NODE_SEEN_WINDOW_SECONDS = 24 * 3600
# A node's capability stamp is written by the same statement as its heartbeat;
# an older version heartbeats without refreshing it, so a stamp older than the
# node's own last heartbeat (minus this slack) means "not capable".
CAPABILITY_STAMP_SLACK_SECONDS = 5

_GATE_SQL = """
SELECT COALESCE(BOOL_AND(capable), FALSE),
       COALESCE(BOOL_OR(node_id = %s AND capable), FALSE)
FROM (
  SELECT node_id,
         CASE
           WHEN jsonb_typeof(metadata) = 'object'
            AND jsonb_typeof(metadata -> 'capabilities') = 'array'
            AND jsonb_typeof(metadata -> 'capabilities_at') = 'number'
           THEN (metadata -> 'capabilities') ? %s
            AND (metadata ->> 'capabilities_at')::float8
                >= EXTRACT(EPOCH FROM last_heartbeat) - %s
           ELSE FALSE
         END AS capable
  FROM cluster_nodes
  WHERE last_heartbeat >= NOW() - %s * INTERVAL '1 second'
) AS seen
"""


def cluster_v2_writes_allowed(pool: Any, node_id: Optional[str] = None) -> bool:
    """True only when this node's own row is capable and every node seen in
    the last NODE_SEEN_WINDOW_SECONDS advertises ``token_key_v2`` with a
    current stamp. Fails closed: no node id (defaults to the registered local
    node), no rows, or any query error means legacy writes."""
    from psycopg.rows import tuple_row

    node = node_id if node_id is not None else local_node_id()
    if node is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor(row_factory=tuple_row) as cur:
                cur.execute(
                    _GATE_SQL,
                    (
                        node,
                        TOKEN_KEY_CAPABILITY,
                        CAPABILITY_STAMP_SLACK_SECONDS,
                        NODE_SEEN_WINDOW_SECONDS,
                    ),
                )
                row = cur.fetchone()
    except Exception as exc:
        logger.warning(
            "Stored-secret v2 gate check failed (%s); writing the legacy format",
            type(exc).__name__,
        )
        return False
    return row is not None and bool(row[0]) and bool(row[1])


# Cluster mode is configured once at startup (the pool and the immutable
# cluster key); solo processes never configure it and use the key file.
_CLUSTER_SOURCE: Dict[str, Any] = {}
_CLUSTER_LOCK = threading.Lock()


def configure_cluster_key_source(pool: Any) -> None:
    """Load (or create) the cluster key and use it for every cipher built in
    this process from now on. Blocking DB work: call it off the event loop."""
    key = load_or_create_cluster_key(pool)
    with _CLUSTER_LOCK:
        _CLUSTER_SOURCE["pool"] = pool
        _CLUSTER_SOURCE["key"] = key


def clear_cluster_key_source() -> None:
    with _CLUSTER_LOCK:
        _CLUSTER_SOURCE.clear()


def register_local_node(node_id: str) -> None:
    """Record this process's cluster node id (set when its heartbeat row is
    registered); the gate stays closed until then."""
    with _CLUSTER_LOCK:
        _CLUSTER_SOURCE["node_id"] = node_id


def local_node_id() -> Optional[str]:
    with _CLUSTER_LOCK:
        node = _CLUSTER_SOURCE.get("node_id")
    return None if node is None else str(node)


def cluster_pool() -> Optional[Any]:
    with _CLUSTER_LOCK:
        return _CLUSTER_SOURCE.get("pool")


def _read_cluster_jwt_secret(pool: Any) -> Optional[str]:
    from psycopg.rows import tuple_row

    with pool.connection() as conn:
        with conn.cursor(row_factory=tuple_row) as cur:
            cur.execute(
                "SELECT key_value FROM cluster_secrets WHERE key_name = %s",
                ("jwt_secret",),
            )
            row = cur.fetchone()
    return None if row is None else str(row[0])


class StoredSecretCipher:
    """Encrypts and decrypts stored secrets for one consumer.

    Writes are v2 under the stored-secret key when the gate is open (always
    in solo; in a cluster only while every active node advertises
    ``token_key_v2``), else legacy under this node's current legacy key, so
    older nodes can still read them. Reads accept both formats; a legacy
    value read while the gate is open is rewritten as v2 through the
    caller's compare-and-set."""

    def __init__(
        self,
        server_dir: Path,
        context_label: str,
        cluster_secret: Optional[str] = None,
        pool: Optional[Any] = None,
        legacy_keys: Optional[Callable[[], List[bytes]]] = None,
    ) -> None:
        if pool is None:
            with _CLUSTER_LOCK:
                pool = _CLUSTER_SOURCE.get("pool")
                key = _CLUSTER_SOURCE.get("key")
        else:
            key = load_or_create_cluster_key(pool)
        self._server_dir = Path(server_dir)
        self._label = context_label
        self._pool = pool
        self._cluster_secret = cluster_secret
        self._legacy_keys_override = legacy_keys
        self._v2_key: bytes
        if pool is not None:
            # the source dict is Any-typed; "key" is always stored with "pool"
            self._v2_key = cast(bytes, key)
        else:
            self._v2_key = load_or_create_key_file(self._server_dir)

    @property
    def node_id(self) -> Optional[str]:
        """The cluster node this cipher runs on (None in solo)."""
        return None if self._pool is None else local_node_id()

    def v2_writes_allowed(self) -> bool:
        if self._pool is None:
            return True
        return cluster_v2_writes_allowed(self._pool)

    def encrypt(self, plaintext: str) -> str:
        from code_indexer.server.services.token_encryption import (
            encrypt_token,
            encrypt_v2,
            legacy_write_key,
        )

        if self.v2_writes_allowed():
            return encrypt_v2(plaintext, self._v2_key)
        return encrypt_token(plaintext, legacy_write_key(self._server_dir))

    def _legacy_key_set(self) -> Tuple[List[bytes], bool]:
        """(keys, complete); complete is False when a source could not be read."""
        from code_indexer.server.services.token_encryption import (
            legacy_candidate_keys,
            warn_unreadable_legacy_source,
        )

        if self._legacy_keys_override is not None:
            return list(self._legacy_keys_override()), True
        shared = self._cluster_secret
        shared_read = True
        if self._pool is not None:
            try:
                shared = _read_cluster_jwt_secret(self._pool)
            except Exception as exc:  # one source failing must not stop the rest
                warn_unreadable_legacy_source("cluster_secrets.jwt_secret", exc)
                shared, shared_read = None, False
        keys, files_read = legacy_candidate_keys(self._server_dir, shared)
        return keys, shared_read and files_read

    def decrypt(self, value: str) -> Tuple[str, bool]:
        """(plaintext, is_legacy); raises a ValueError subclass when unreadable
        (StoredSecretUndetermined when a key source could not be read)."""
        from code_indexer.server.services.token_encryption import (
            StoredSecretAmbiguous,
            StoredSecretKeyMismatch,
            StoredSecretUndetermined,
            StoredSecretUnreadable,
            decrypt_v2,
            decrypt_with_fallback,
        )

        complete = True

        def legacy_keys() -> List[bytes]:
            nonlocal complete
            keys, complete = self._legacy_key_set()
            return keys

        undetermined = StoredSecretUndetermined(
            f"{self._label}: a key source could not be read; the stored secret "
            "is left as it is"
        )
        try:
            return decrypt_with_fallback(value, self._v2_key, legacy_keys, self._label)
        except StoredSecretAmbiguous:
            raise
        except StoredSecretUnreadable:
            if complete:
                raise
            raise undetermined from None
        except StoredSecretKeyMismatch as mismatch:
            # The server_dir key file exists after solo operation (or when a
            # component ran before the cluster key source was configured); a
            # v2 value under it is read and rewritten under the cluster key.
            solo_file = self._server_dir / KEY_FILENAME
            if self._pool is None or not os.path.lexists(solo_file):
                raise
            try:
                solo_key = validate_key(_read_checked(solo_file), str(solo_file))
            except StoredSecretKeyError as exc:  # a secondary source: skip it
                from code_indexer.server.services.token_encryption import (
                    warn_unreadable_legacy_source,
                )

                warn_unreadable_legacy_source(str(solo_file), exc)
                raise undetermined from mismatch
            return decrypt_v2(value, solo_key), True

    def rewrite_value(self, plaintext: str) -> Optional[str]:
        """The v2 value to store in place of a legacy one, or None while the
        cluster gate is closed."""
        from code_indexer.server.services.token_encryption import encrypt_v2

        if not self.v2_writes_allowed():
            return None
        return encrypt_v2(plaintext, self._v2_key)

    def read(self, value: str, replace: Callable[[str, str], bool]) -> str:
        """Decrypt *value*; a legacy value is rewritten as v2 through
        ``replace(old, new)`` (a compare-and-set) when the gate is open."""
        plaintext, is_legacy = self.decrypt(value)
        if is_legacy:
            new_value = self.rewrite_value(plaintext)
            if new_value is not None and not replace(value, new_value):
                logger.info(
                    "%s: stored secret changed concurrently; rewrite skipped",
                    self._label,
                )
        return plaintext
