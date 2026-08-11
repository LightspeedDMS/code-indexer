"""
SSH Key Cluster Sync Service.

Story #428: Per-node sync service that reads SSH key metadata from PostgreSQL
(or any SSHKeysBackend) and writes key files to local ~/.ssh/.

Tracks which keys it manages via a manifest JSON file so it can remove stale
entries on the next sync without touching keys it never created.

Bug #1521: that manifest is a SINGLE file shared by every server process
pointed at the same ssh directory, so its entries are namespaced by a stable
BACKEND IDENTITY -- see ``SSHKeySyncService._derive_backend_identity``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from code_indexer.server.services.ssh_config_manager import (
    HostEntry,
    SSHConfigManager,
)

logger = logging.getLogger(__name__)

# Manifest schema version.  v1 was ``{"keys": [...]}`` -- a flat, unattributed
# list that could not express WHICH backend recorded which name (Bug #1521).
MANIFEST_SCHEMA_VERSION = 2

# Length of the hex digest used as a backend identity.  128 bits of SHA-256 is
# far beyond collision risk for the handful of backends a host ever sees, and
# keeps the manifest readable.
_BACKEND_IDENTITY_DIGEST_LENGTH = 32


class SSHKeySyncService:
    """Syncs SSH keys from a backend (PG/SQLite) to local filesystem."""

    def __init__(
        self,
        ssh_keys_backend: Any,
        ssh_dir: str = "~/.ssh",
        fernet: Any = None,
        backend_identity: Optional[str] = None,
    ) -> None:
        """
        Initialize the sync service.

        Args:
            ssh_keys_backend: Any object with a list_keys() method that returns
                              a list of dicts with at least: name, private_key
                              (or public_key), private_path, public_path.
            ssh_dir: Directory to write SSH key files into.
                     Defaults to ~/.ssh (expanded at init time).
            fernet: Optional Fernet instance used to decrypt private key content
                    stored encrypted in the backend (cluster mode).  When None
                    the private key bytes are written as-is (solo/SQLite mode).
            backend_identity: Optional explicit identity used to namespace this
                    instance's entries in the shared manifest (Bug #1521).
                    When omitted it is derived from the backend itself; see
                    ``_derive_backend_identity``.
        """
        self._backend = ssh_keys_backend
        self._ssh_dir = Path(ssh_dir).expanduser()
        self._manifest_file = self._ssh_dir / ".cidx-ssh-keys.json"
        self._config_path = self._ssh_dir / "config"
        self._config_manager = SSHConfigManager()
        self._fernet = fernet
        # Resolved once: the identity must be stable for the lifetime of this
        # service and must depend only on the BACKEND, so that two
        # independently constructed services sharing one backend (e.g. two
        # cluster nodes on one PostgreSQL instance) resolve to the same
        # manifest namespace and keep their mutual cleanup working.
        self.backend_identity: Optional[str] = (
            backend_identity
            if backend_identity is not None
            else self._derive_backend_identity(ssh_keys_backend)
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sync(self) -> Dict[str, Any]:
        """
        Full sync: read backend keys, write missing files, remove stale ones.

        Returns:
            dict with keys:
              - written: list of key names written to disk
              - removed: list of key names removed from disk
              - unchanged: list of key names already up-to-date
              - errors: list of error strings encountered
        """
        # Safety guard (2026-08-02 incident): a test that constructs this
        # service without overriding ssh_dir silently reconciles against
        # the REAL, unoverridden ~/.ssh -- an empty/fake test backend then
        # makes every previously-real, manifest-tracked key "stale" and
        # deletes it. This guard makes that class of mistake a loud no-op
        # instead of a silent, irreversible deletion of a developer's
        # actual keys. It can never affect a legitimate test: every real
        # test in this suite passes an explicit tmp_path-based ssh_dir,
        # which never equals the real expanduser("~/.ssh").
        if (
            "PYTEST_CURRENT_TEST" in os.environ
            and self._ssh_dir == Path("~/.ssh").expanduser()
        ):
            msg = (
                "SSHKeySyncService.sync() refused: running under pytest "
                f"with an unoverridden real ssh_dir ({self._ssh_dir}). "
                "Pass an explicit tmp_path-based ssh_dir in tests."
            )
            # Bug #1551: this refusal is a by-design, correctly-handled
            # guard -- it fires on every test run that constructs this
            # service without an explicit tmp_path-based ssh_dir, which is
            # expected happy-path behavior, not an emergency. Logging it at
            # CRITICAL devalued the tier (8 of 14 high-severity entries in a
            # 24h window were this single benign message), crowding out
            # genuine emergencies. DEBUG keeps it discoverable without
            # flooding the high-severity channel. The guard's REFUSAL
            # behavior itself is unchanged -- only this log line's severity.
            logger.debug(msg)
            return {"written": [], "removed": [], "unchanged": [], "errors": [msg]}

        self._ssh_dir.mkdir(parents=True, mode=0o700, exist_ok=True)

        # Read current state from backend
        try:
            backend_keys = self._backend.list_keys()
        except Exception as exc:
            logger.error(f"Failed to read SSH keys from backend: {exc}")
            return {"written": [], "removed": [], "unchanged": [], "errors": [str(exc)]}

        backend_names: Set[str] = {k["name"] for k in backend_keys}
        managed_names = self._get_managed_keys()

        written = []
        unchanged = []
        errors = []

        # Write keys that exist in backend but not yet on disk
        for key_data in backend_keys:
            name = key_data["name"]
            try:
                private_key = key_data.get("private_key")
                if private_key and self._fernet is not None:
                    try:
                        private_key = self._fernet.decrypt(
                            private_key.encode()
                        ).decode()
                    except Exception as exc:
                        logger.error(
                            f"Failed to decrypt SSH private key '{name}': {exc}"
                        )
                        errors.append(f"{name}: decrypt failed: {exc}")
                        continue
                public_key = key_data.get("public_key")

                private_path = self._ssh_dir / name
                public_path = self._ssh_dir / f"{name}.pub"

                needs_write = False
                if private_key and not private_path.exists():
                    needs_write = True
                if public_key and not public_path.exists():
                    needs_write = True

                if needs_write:
                    self._write_key_file(name, private_key or "", public_key or "")
                    written.append(name)
                    logger.info(f"SSH key synced to disk: {name}")
                else:
                    unchanged.append(name)
            except Exception as exc:
                logger.error(f"Failed to write SSH key '{name}': {exc}")
                errors.append(f"{name}: {exc}")

        # Remove stale keys — recorded by THIS backend but no longer in it.
        #
        # `managed_names` is deliberately scoped to this service's own backend
        # namespace (Bug #1521).  Before that scoping it was every name in the
        # shared manifest regardless of which process/backend wrote it, so a
        # second server instance with its own (e.g. empty) backend computed
        # `managed_names - backend_names` over ANOTHER instance's legitimately
        # owned keys and unlink()ed them -- proven, irreversible data loss.
        # Same-backend multi-node cleanup (an admin deleting a key on cluster
        # node A, node B removing its now-orphaned local copy on the next sync)
        # still works, because every node shares one backend identity and
        # therefore one namespace.
        removed = []
        stale_names = managed_names - backend_names
        for name in stale_names:
            try:
                private_path = self._ssh_dir / name
                public_path = self._ssh_dir / f"{name}.pub"
                if private_path.exists():
                    private_path.unlink()
                    logger.info(f"Removed stale SSH private key: {private_path}")
                if public_path.exists():
                    public_path.unlink()
                    logger.info(f"Removed stale SSH public key: {public_path}")
                removed.append(name)
            except Exception as exc:
                logger.error(f"Failed to remove stale SSH key '{name}': {exc}")
                errors.append(f"remove {name}: {exc}")

        # Update manifest with provenance-verified managed names only
        # (Bug #1519): a name is only ever recorded as "managed" here if
        # this service can prove it either (a) actually wrote it just now
        # (in `written`), or (b) was already correctly verified-managed in
        # a prior sync AND is still reported by the backend now
        # (`managed_names & backend_names`). Never simply because the
        # backend reports a name -- that previously let a skipped write
        # (name collision with a pre-existing, unrelated file) get recorded
        # as managed, causing a later stale-key sweep to unlink() a file
        # this service never created.
        provenance_verified_names = (managed_names & backend_names) | set(written)
        self._update_manifest(provenance_verified_names)

        # Materialize the ~/.ssh/config Host->IdentityFile mapping so that git
        # SSH remotes (e.g. the cidx-meta backup remote git@github.com) resolve
        # to the locally-synced key on EVERY cluster node -- not just the node
        # where the key was originally created.  Without this, a worker node
        # that becomes cluster leader cannot authenticate the backup push/fetch
        # (Permission denied (publickey)).
        self._sync_ssh_config(backend_keys, errors)

        return {
            "written": written,
            "removed": removed,
            "unchanged": unchanged,
            "errors": errors,
        }

    def _sync_ssh_config(self, backend_keys: list, errors: list) -> None:
        """Regenerate the CIDX-managed ~/.ssh/config section from backend keys.

        For each key, one Host block is emitted per assigned host.  The
        IdentityFile always points at the path THIS node wrote the key to
        (``ssh_dir/<name>``) -- never the originating node's ``private_path``,
        which is meaningless on other nodes.  The user-authored section of the
        config (and any Include directives) is preserved byte-for-byte.

        Failures are appended to ``errors`` and never raised -- SSH key file
        materialization has already succeeded at this point and must not be
        rolled back by a config-write problem.
        """
        try:
            entries: List[HostEntry] = []
            for key_data in backend_keys:
                name = key_data["name"]
                hosts = key_data.get("hosts") or []
                key_path = str(self._ssh_dir / name)
                for hostname in hosts:
                    entries.append(
                        HostEntry(
                            host=hostname,
                            hostname=hostname,
                            key_path=key_path,
                        )
                    )

            parsed = self._config_manager.parse_config(self._config_path)

            # Idempotency guard: this service runs on EVERY node startup, so a
            # blind write_config() every time would drift the file (the manager
            # appends a trailing newline per round-trip).  Only write when the
            # desired CIDX Host mappings differ from what is already on disk.
            desired = [(entry.host, entry.key_path) for entry in entries]
            current = self._parse_cidx_host_mappings(parsed.cidx_section)
            if current == desired:
                return

            self._config_manager.write_config(self._config_path, parsed, entries)
            if entries:
                logger.info(
                    "SSH config synced: %d host mapping(s) written to %s",
                    len(entries),
                    self._config_path,
                )
        except Exception as exc:
            logger.error(f"Failed to sync SSH config {self._config_path}: {exc}")
            errors.append(f"ssh-config: {exc}")

    @staticmethod
    def _parse_cidx_host_mappings(cidx_section: List[str]) -> List[tuple]:
        """Parse a CIDX config section into ordered (host, identityfile) tuples.

        Used purely for change-detection; mirrors the block shape written by
        ``SSHConfigManager._format_host_block`` (Host / IdentityFile lines).
        """
        mappings: List[tuple] = []
        current_host: str | None = None
        for raw in cidx_section:
            line = raw.strip()
            if line.lower().startswith("host "):
                current_host = line[len("host ") :].strip()
            elif line.lower().startswith("identityfile ") and current_host:
                identity = line[len("identityfile ") :].strip()
                mappings.append((current_host, identity))
                current_host = None
        return mappings

    # ------------------------------------------------------------------
    # Backend identity (Bug #1521)
    # ------------------------------------------------------------------

    @staticmethod
    def _identity_digest(scheme: str, value: str) -> str:
        """Hash a backend locator into an opaque, stable identity.

        The locator is never stored verbatim: a PostgreSQL DSN can carry an
        authentication token, and the manifest is a plain file on disk.
        """
        raw = f"{scheme}:{value}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:_BACKEND_IDENTITY_DIGEST_LENGTH]

    @staticmethod
    def _derive_backend_identity(backend: Any) -> Optional[str]:
        """Derive a stable identity for the given SSH keys backend.

        The identity answers exactly one question: "do two sync services read
        and write the same logical set of SSH key rows?"  It MUST therefore be

          * IDENTICAL across cluster nodes sharing one PostgreSQL instance --
            that is what keeps legitimate multi-node stale cleanup working; and
          * DIFFERENT between two independent solo/SQLite databases -- that is
            what closes the Bug #1521 cross-instance deletion vector.

        Resolution order (first match wins):
          1. An explicit ``backend_identity()`` method on the backend.
          2. SQLite: the resolved database file path.
          3. PostgreSQL: the pool's connection string (same DSN on every node).

        Returns None when no stable locator can be found.  Callers must treat
        that as "cannot attribute anything in the manifest to me" and refuse to
        delete -- an orphaned file lingering is vastly preferable to destroying
        a real, in-use key.
        """
        # 1. Explicit opt-in. Every isinstance(str) check below matters: a
        #    MagicMock or other opaque test double auto-creates attributes, and
        #    accepting one would fabricate an unstable, meaningless identity.
        provider = getattr(backend, "backend_identity", None)
        if callable(provider):
            try:
                declared = provider()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "SSH keys backend rejected backend_identity() call: %s", exc
                )
                declared = None
            if isinstance(declared, str) and declared:
                return SSHKeySyncService._identity_digest("explicit", declared)

        # 2. SQLite backends hold a DatabaseConnectionManager keyed by db path.
        conn_manager = getattr(backend, "_conn_manager", None)
        db_path = getattr(conn_manager, "db_path", None)
        if isinstance(db_path, str) and db_path:
            return SSHKeySyncService._identity_digest(
                "sqlite", os.path.abspath(db_path)
            )

        # 3. PostgreSQL backends hold a connection pool. `_connection_string`
        #    is this project's own ConnectionPool wrapper; `conninfo` covers a
        #    raw psycopg_pool.ConnectionPool being passed directly.
        pool = getattr(backend, "_pool", None)
        if pool is not None:
            for attribute in ("_connection_string", "conninfo"):
                dsn = getattr(pool, attribute, None)
                if isinstance(dsn, str) and dsn:
                    return SSHKeySyncService._identity_digest("postgres", dsn)

        return None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _write_key_file(self, name: str, private_key: str, public_key: str) -> None:
        """
        Write key files with correct permissions (600 private, 644 public).

        Args:
            name: Key name — used as filename under ssh_dir.
            private_key: Private key content (PEM/OpenSSH).  Empty string = skip.
            public_key: Public key content.  Empty string = skip.
        """
        private_path = self._ssh_dir / name
        public_path = self._ssh_dir / f"{name}.pub"

        if private_key:
            private_path.write_text(private_key)
            os.chmod(private_path, 0o600)

        if public_key:
            public_path.write_text(public_key)
            os.chmod(public_path, 0o644)

    def _read_manifest_document(self) -> Dict[str, Any]:
        """Read the raw manifest document.

        Returns an empty dict when the manifest is absent, unreadable or not a
        JSON object -- never raises, since a damaged manifest must degrade into
        "I manage nothing" (and therefore delete nothing), not into a crash.
        """
        if not self._manifest_file.exists():
            return {}
        try:
            data = json.loads(self._manifest_file.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"Cannot read SSH key manifest {self._manifest_file}: {exc}")
            return {}
        if not isinstance(data, dict):
            logger.warning(
                "SSH key manifest %s is not a JSON object; ignoring its contents",
                self._manifest_file,
            )
            return {}
        typed: Dict[str, Any] = data
        return typed

    @staticmethod
    def _namespaces_of(document: Dict[str, Any]) -> Dict[str, List[str]]:
        """Extract the backend-id -> managed-names map from a manifest doc."""
        namespaces = document.get("backends")
        if not isinstance(namespaces, dict):
            return {}
        cleaned: Dict[str, List[str]] = {}
        for backend_id, names in namespaces.items():
            if isinstance(backend_id, str) and isinstance(names, list):
                cleaned[backend_id] = [name for name in names if isinstance(name, str)]
        return cleaned

    def _get_managed_keys(self) -> Set[str]:
        """
        Read the key names THIS backend previously recorded as managed.

        Only this service's own backend namespace is returned.  Entries written
        by any other backend -- and legacy v1 entries, whose provenance is
        unknowable -- are deliberately excluded, because this set is what drives
        deletion (Bug #1521).
        """
        document = self._read_manifest_document()

        if document.get("keys"):
            # Pre-#1521 (v1) manifest: a flat list with no record of which
            # backend wrote which name. Adopting those entries would reproduce
            # the exact data-loss vector on the first sync after upgrade, so
            # they are dropped rather than acted upon.
            logger.warning(
                "SSH key manifest %s uses the legacy unattributed schema; its "
                "entries have unknown provenance and will not be adopted or "
                "removed. CIDX will re-establish provenance for keys it writes "
                "from now on; any genuinely orphaned file must be removed "
                "explicitly.",
                self._manifest_file,
            )

        if self.backend_identity is None:
            logger.warning(
                "SSH keys backend %s exposes no stable identity; refusing to "
                "treat any manifest entry in %s as removable. Key files will "
                "still be written, but stale-key cleanup is disabled for this "
                "backend.",
                type(self._backend).__name__,
                self._manifest_file,
            )
            return set()

        return set(self._namespaces_of(document).get(self.backend_identity, []))

    def _update_manifest(self, keys: Set[str]) -> None:
        """
        Persist this backend's managed key names into the shared manifest.

        Only this service's own namespace is rewritten; every other backend's
        namespace is carried over verbatim, so a second server instance can
        never erase another instance's provenance record (which would silently
        disable that instance's own legitimate cleanup).

        A top-level "keys" list is deliberately NEVER written: that is exactly
        what a pre-#1521 process would read and then delete against its own
        unrelated backend.  Omitting it makes such a process see an empty
        managed set and delete nothing.
        """
        try:
            namespaces = self._namespaces_of(self._read_manifest_document())

            if self.backend_identity is not None:
                if keys:
                    namespaces[self.backend_identity] = sorted(keys)
                else:
                    namespaces.pop(self.backend_identity, None)

            data = {
                "version": MANIFEST_SCHEMA_VERSION,
                "backends": namespaces,
            }
            self._manifest_file.write_text(json.dumps(data, indent=2))
            os.chmod(self._manifest_file, 0o600)
        except OSError as exc:
            logger.error(f"Failed to update SSH key manifest: {exc}")
