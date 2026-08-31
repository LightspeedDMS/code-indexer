"""
SSH Key Manager Service (Core Orchestrator).

Provides unified interface for SSH key management, coordinating
key generation, metadata storage, and SSH config updates.

Supports both SQLite backend (Story #702) and JSON file storage (backward compatible).
Cluster mode (Bug #1072): encrypts private key content via Fernet and persists
the encrypted blob to the PostgreSQL backend so the sync service can distribute
the key to all cluster nodes.
"""

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple
import filelock

from .ssh_key_generator import SSHKeyGenerator
from .ssh_config_manager import SSHConfigManager, HostEntry
from .key_discovery_service import KeyDiscoveryService, KeyInfo

logger = logging.getLogger(__name__)


class KeyNotFoundError(Exception):
    """Raised when requested key does not exist."""

    pass


class HostConflictError(Exception):
    """Raised when host already exists in user section of SSH config."""

    pass


class PublicKeyNotFoundError(Exception):
    """Raised when public key file is missing."""

    pass


@dataclass
class KeyMetadata:
    """Metadata for a managed SSH key."""

    name: str
    fingerprint: str
    key_type: str
    private_path: str
    public_path: str
    public_key: Optional[str] = None
    email: Optional[str] = None
    description: Optional[str] = None
    hosts: List[str] = field(default_factory=list)
    created_at: Optional[str] = None
    imported_at: Optional[str] = None
    is_imported: bool = False


@dataclass
class KeyListResult:
    """Result of listing SSH keys."""

    managed: List[KeyMetadata] = field(default_factory=list)
    unmanaged: List[KeyInfo] = field(default_factory=list)


class SSHKeyManager:
    """
    Core orchestrator for SSH key management.

    Coordinates key generation, metadata storage, SSH config updates,
    and key discovery operations.

    Supports both SQLite backend (Story #702) and JSON file storage (backward compatible).
    Cluster mode (Bug #1072): when pg_backend and fernet are set (via constructor or
    set_cluster_dependencies classmethod), create_key encrypts the private key content
    and persists the encrypted blob to PostgreSQL so the sync service can distribute it.
    """

    # Class-level cluster dependencies — set once from lifespan via set_cluster_dependencies().
    # Instances fall back to these when not explicitly passed to __init__.
    _cluster_pg_backend: Optional[Any] = None
    _cluster_fernet: Optional[Any] = None

    @classmethod
    def set_cluster_dependencies(cls, pg_backend: Any, fernet: Any) -> None:
        """Inject cluster-mode dependencies at the class level.

        Called once from lifespan when the server starts in cluster mode.
        All subsequently constructed SSHKeyManager instances will pick up
        these values unless they are overridden via explicit constructor params.

        Args:
            pg_backend: SSHKeysPostgresBackend instance.
            fernet: cryptography.fernet.Fernet instance for encrypting private keys.
        """
        cls._cluster_pg_backend = pg_backend
        cls._cluster_fernet = fernet
        logger.info("SSHKeyManager: cluster dependencies injected (PG + Fernet)")

    def __init__(
        self,
        ssh_dir: Optional[Path] = None,
        metadata_dir: Optional[Path] = None,
        config_path: Optional[Path] = None,
        use_sqlite: bool = False,
        db_path: Optional[Path] = None,
        pg_backend: Optional[Any] = None,
        fernet: Optional[Any] = None,
    ):
        """
        Initialize the SSH key manager.

        Args:
            ssh_dir: Directory for SSH keys. Defaults to ~/.ssh/
            metadata_dir: Directory for key metadata. Defaults to
                          ~/.code-indexer-server/ssh_keys/
            config_path: Path to SSH config file. Defaults to ~/.ssh/config
            use_sqlite: If True, use SQLite backend instead of JSON files (Story #702)
            db_path: Path to SQLite database file (required when use_sqlite=True)
            pg_backend: Optional SSHKeysPostgresBackend for cluster mode (Bug #1072).
                        Falls back to class-level _cluster_pg_backend if not supplied.
            fernet: Optional Fernet instance for encrypting private keys in cluster mode.
                    Falls back to class-level _cluster_fernet if not supplied.
        """
        self._use_sqlite = use_sqlite
        self._sqlite_backend: Optional[Any] = None

        # Cluster-mode deps: explicit params take precedence over class-level attrs.
        self._pg_backend: Optional[Any] = (
            pg_backend if pg_backend is not None else self.__class__._cluster_pg_backend
        )
        self._fernet: Optional[Any] = (
            fernet if fernet is not None else self.__class__._cluster_fernet
        )

        if ssh_dir is None:
            ssh_dir = Path.home() / ".ssh"
        if metadata_dir is None:
            metadata_dir = Path.home() / ".code-indexer-server" / "ssh_keys"
        if config_path is None:
            config_path = ssh_dir / "config"

        self.ssh_dir = ssh_dir
        self.metadata_dir = metadata_dir
        self.config_path = config_path

        # Initialize component services
        self.key_generator = SSHKeyGenerator(ssh_dir=ssh_dir)
        self.config_manager = SSHConfigManager()
        self.discovery_service = KeyDiscoveryService(ssh_dir=ssh_dir)

        # Lock file for concurrent operations
        self.lock_path = metadata_dir.parent / "ssh_keys.lock"

        if use_sqlite:
            if db_path is None:
                raise ValueError("db_path is required when use_sqlite=True")
            from code_indexer.server.storage.sqlite_backends import (
                SSHKeysSqliteBackend,
            )

            self._sqlite_backend = SSHKeysSqliteBackend(str(db_path))

    def _get_lock(self) -> filelock.FileLock:
        """Get file lock for concurrent operation protection."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        return filelock.FileLock(str(self.lock_path))

    def create_key(
        self,
        name: str,
        key_type: str = "ed25519",
        email: Optional[str] = None,
        description: Optional[str] = None,
    ) -> KeyMetadata:
        """
        Create a new SSH key pair with metadata.

        Args:
            name: Name for the key (used as filename)
            key_type: Type of key (ed25519, rsa)
            email: Comment/email to include in the key
            description: Human-readable description

        Returns:
            KeyMetadata for the created key
        """

        with self._get_lock():
            # Generate the key
            generated = self.key_generator.generate_key(
                key_name=name,
                key_type=key_type,
                email=email,
            )

            created_at = datetime.now().isoformat()

            # Create metadata
            metadata = KeyMetadata(
                name=name,
                fingerprint=generated.fingerprint,
                key_type=key_type,
                private_path=str(generated.private_path),
                public_path=str(generated.public_path),
                public_key=generated.public_key,
                email=email,
                description=description,
                hosts=[],
                created_at=created_at,
                is_imported=False,
            )

            # --- Cluster mode: encrypt private key and persist to PG ---
            pg_backend = self._pg_backend
            fernet = self._fernet
            if pg_backend is not None and fernet is not None:
                # Read the private key file written by the generator
                private_key_content = Path(generated.private_path).read_text()
                encrypted_private = fernet.encrypt(
                    private_key_content.encode()
                ).decode()
                try:
                    pg_backend.create_key(
                        name=name,
                        fingerprint=generated.fingerprint,
                        key_type=key_type,
                        private_path=str(generated.private_path),
                        public_path=str(generated.public_path),
                        public_key=generated.public_key,
                        email=email,
                        description=description,
                        is_imported=metadata.is_imported,
                        private_key=encrypted_private,
                    )
                    logger.info(
                        "SSHKeyManager: persisted encrypted private key for '%s' to PG",
                        name,
                    )
                except Exception:
                    logger.exception(
                        "SSHKeyManager: failed to persist encrypted private key for '%s' to PG",
                        name,
                    )
                    raise

            # --- Save local metadata: SQLite or JSON ---
            # In cluster mode the SQLite record has private_key=None (PG is the
            # source of truth for sync).  In solo mode private_key stays None too
            # (the column exists but solo nodes don't use the encrypted blob).
            if self._use_sqlite and self._sqlite_backend is not None:
                self._sqlite_backend.create_key(
                    name=name,
                    fingerprint=generated.fingerprint,
                    key_type=key_type,
                    private_path=str(generated.private_path),
                    public_path=str(generated.public_path),
                    public_key=generated.public_key,
                    email=email,
                    description=description,
                    is_imported=False,
                )
            else:
                self._save_metadata(metadata)

            return metadata

    def assign_key_to_host(
        self,
        key_name: str,
        hostname: str,
        force: bool = False,
    ) -> KeyMetadata:
        """
        Assign a key to a hostname in SSH config.

        Args:
            key_name: Name of the key to assign
            hostname: Hostname to assign the key to
            force: If True, override user section conflicts

        Returns:
            Updated KeyMetadata
        """

        with self._get_lock():
            if self._use_sqlite and self._sqlite_backend is not None:
                # SQLite backend (Story #702)
                key_data = self._sqlite_backend.get_key(key_name)
                if key_data is None:
                    return self._assign_cluster_only_key_to_host(
                        key_name, hostname, force
                    )

                self._raise_on_user_section_conflict(hostname, force)

                # Add hostname if not already present
                if hostname not in key_data["hosts"]:
                    self._sqlite_backend.assign_host(key_name, hostname)

                # --- Cluster mode: mirror host assignment to PG so the sync
                # service (which reads exclusively from PG) can materialize
                # the Host block fleet-wide (Bug #1504). Mirrors delete_key's
                # existing cluster-mode pattern: local write first, then PG;
                # a PG failure is logged and re-raised, never swallowed. ---
                self._mirror_host_assignment_to_cluster(key_name, hostname)

                # Update SSH config
                self._update_ssh_config()

                # Return updated metadata
                updated_data = self._sqlite_backend.get_key(key_name)
                if updated_data is None:
                    raise KeyNotFoundError(
                        f"Key '{key_name}' unexpectedly missing after assignment"
                    )
                return self._key_metadata_from_backend(updated_data)
            else:
                # JSON file storage (backward compatible)
                metadata = self._load_metadata(key_name)
                if metadata is None:
                    return self._assign_cluster_only_key_to_host(
                        key_name, hostname, force
                    )

                self._raise_on_user_section_conflict(hostname, force)

                # Add hostname to metadata if not already present
                if hostname not in metadata.hosts:
                    metadata.hosts.append(hostname)
                    self._save_metadata(metadata)

                # Update SSH config
                self._update_ssh_config()

                return metadata

    def _raise_on_user_section_conflict(self, hostname: str, force: bool) -> None:
        """Refuse to shadow a hand-written ``~/.ssh/config`` Host entry.

        Shared verbatim by all three assignment paths (node-local SQLite,
        node-local JSON, and the cluster-resolved path added for Bug #1526) so
        the guard cannot drift between them.

        Raises:
            HostConflictError: ``hostname`` already appears in the user section
                and the caller did not pass ``force=True``.
        """
        if force:
            return

        conflict = self.config_manager.check_host_conflict(self.config_path, hostname)
        if conflict.exists and conflict.in_user_section:
            raise HostConflictError(
                f"Host {hostname} exists in user section. "
                "Use force=True or remove manually."
            )

    def _mirror_host_assignment_to_cluster(self, key_name: str, hostname: str) -> None:
        """Mirror a host assignment into the shared cluster backend.

        Bug #1504: ``SSHKeySyncService`` reads host assignments exclusively from
        the shared backend, so without this the Host block is never materialized
        fleet-wide.  A no-op in solo mode (no ``_pg_backend``).

        Raises:
            Exception: whatever the backend raised.  Mirrors create_key's and
                delete_key's policy -- log, then re-raise; never swallowed.
        """
        pg_backend = self._pg_backend
        if pg_backend is None:
            return

        try:
            pg_backend.assign_host(key_name, hostname)
            logger.info(
                "SSHKeyManager: assigned host '%s' to key '%s' in PG backend",
                hostname,
                key_name,
            )
        except Exception:
            logger.exception(
                "SSHKeyManager: failed to assign host '%s' to key '%s' in PG backend",
                hostname,
                key_name,
            )
            raise

    def _assign_cluster_only_key_to_host(
        self, key_name: str, hostname: str, force: bool
    ) -> KeyMetadata:
        """Assign a host to a key known only through the shared cluster backend.

        Bug #1526: this node holds no local record to update, so the shared
        backend IS the record -- and it is what ``SSHKeySyncService`` reads to
        materialize the Host block on every node.  This node's ``~/.ssh/config``
        is then regenerated, which now includes the cluster key because Bug #1524
        made the list path cluster-aware.

        Called with ``self._get_lock()`` already held by ``assign_key_to_host``.

        Raises:
            KeyNotFoundError: unknown locally AND in the cluster -- always the
                outcome in solo mode, keeping that path byte-identical.
            HostConflictError: the same user-section guard the node-local paths
                apply, evaluated BEFORE anything is written.
        """
        if self._cluster_managed_key_metadata(key_name) is None:
            raise KeyNotFoundError(f"Key not found: {key_name}")

        self._raise_on_user_section_conflict(hostname, force)
        self._mirror_host_assignment_to_cluster(key_name, hostname)
        self._update_ssh_config()

        updated = self._cluster_managed_key_metadata(key_name)
        if updated is None:
            raise KeyNotFoundError(
                f"Key '{key_name}' unexpectedly missing after assignment"
            )
        return updated

    def _has_untracked_conflicting_file(self, key_name: str) -> bool:
        """Bug #1519 provenance guard.

        When no backend/JSON metadata exists for ``key_name``, this service
        has no proof it ever created a file by that name -- it could be an
        unrelated file (e.g. a user's personal SSH key) that merely shares
        a filename with a previously backend-tracked key. Returns True (and
        logs a WARNING) only when such a same-named file is actually
        present in ``self.ssh_dir``, so the caller can refuse to delete it
        instead of blindly trusting the name match.

        Also hardens against path-traversal in ``key_name`` (e.g.
        ``"../foo"``): any name whose resolved path would fall outside
        ``self.ssh_dir`` is rejected up front, before any filesystem
        existence check is performed.
        """
        default_private = self.ssh_dir / key_name
        default_public = self.ssh_dir / f"{key_name}.pub"

        ssh_dir_resolved = self.ssh_dir.resolve()
        if not (
            default_private.resolve().is_relative_to(ssh_dir_resolved)
            and default_public.resolve().is_relative_to(ssh_dir_resolved)
        ):
            logger.warning(
                "SSHKeyManager.delete_key('%s'): key name resolves outside "
                "ssh_dir -- refusing (path-traversal guard)",
                key_name,
            )
            return True

        if not (default_private.exists() or default_public.exists()):
            return False

        logger.warning(
            "SSHKeyManager.delete_key('%s'): no tracked metadata found, "
            "but a same-named file exists in %s -- refusing to delete "
            "an untracked file (Bug #1519 provenance guard)",
            key_name,
            self.ssh_dir,
        )
        return True

    def _unlink_key_files(self, private_path: str, public_path: str) -> None:
        """Remove the key pair a provenance-bearing record points at.

        Shared by every ``delete_key`` path (node-local SQLite, node-local
        JSON, and the cluster-resolved path added for Bug #1527) so the three
        cannot drift.  Absent files are not an error: delete is idempotent, and
        a cluster-managed key legitimately has no local copy on a node that
        never ran ``SSHKeySyncService.sync()``.
        """
        for path in (Path(private_path), Path(public_path)):
            if path.exists():
                path.unlink()

    def _delete_cluster_managed_or_refuse(self, key_name: str) -> bool:
        """Resolve a node-local delete miss against cluster truth (Bug #1527).

        ``delete_key`` used to conclude "untracked" straight from a node-local
        miss, so a genuinely cluster-managed key -- one the shared backend
        tracks, and that Bug #1524 already reports as ``managed`` here -- hit
        Bug #1519's provenance refusal on every node that had not itself
        created it.  On clustered staging that made a real key undeletable
        through HAProxy round-robin except on the minority of nodes holding a
        local record.

        The shared backend row IS provenance: this service (on some node)
        created that key, and ``SSHKeySyncService`` materializes it here as
        ``ssh_dir/<name>`` -- which is exactly the path
        ``_cluster_managed_key_metadata`` returns, since
        ``_local_materialized_paths`` rebases the row onto this node and
        refuses any name that would not resolve to a direct child of
        ``ssh_dir``.  Cluster truth therefore can never authorize a deletion
        outside ``ssh_dir``, and a same-named file is only removed for a name
        the cluster positively confirms.

        When the shared backend has NO record either (solo mode always, or a
        genuinely foreign name collision), the decision falls through to
        ``_has_untracked_conflicting_file`` unchanged -- Bug #1519/#1521's
        refusal is preserved verbatim, which is the whole point of routing the
        cluster check BEFORE it rather than instead of it.

        Returns:
            True when the delete must be refused (Bug #1519 guard fired),
            False when there is nothing to refuse -- either the key was
            cluster-managed and its local files have now been removed, or no
            same-named file exists at all (idempotent no-op).

        Raises:
            Exception: whatever the shared backend raised, via
                ``_cluster_managed_key_metadata``.  Mirrors this class's policy
                for the shared backend (log then re-raise): degrading to the
                node-local view would silently reintroduce the false refusal.
        """
        metadata = self._cluster_managed_key_metadata(key_name)
        if metadata is None:
            return self._has_untracked_conflicting_file(key_name)

        logger.info(
            "SSHKeyManager.delete_key('%s'): no node-local record, but the "
            "shared cluster backend confirms the key is managed -- deleting "
            "this node's materialized copy (Bug #1527)",
            key_name,
        )
        self._unlink_key_files(metadata.private_path, metadata.public_path)
        return False

    def _delete_from_cluster_backend(self, key_name: str) -> None:
        """Remove the key's row from the shared cluster backend.

        Without this the key resurrects on the next ``SSHKeySyncService.sync()``,
        which reads exclusively from the shared backend.  A no-op in solo mode
        (no ``_pg_backend``).

        Applies to BOTH storage modes: this used to live inside ``delete_key``'s
        SQLite-only branch, so a JSON-metadata node in cluster mode never
        removed the shared row.  That asymmetry became load-bearing once Bug
        #1527 let a cluster-managed key be deleted from a node holding no local
        record -- the local files would go and the row would stay, resurrecting
        the key on the next sync.

        Raises:
            Exception: whatever the backend raised.  Mirrors create_key's and
                assign_key_to_host's policy -- log, then re-raise; never
                swallowed.
        """
        pg_backend = self._pg_backend
        if pg_backend is None:
            return

        try:
            pg_backend.delete_key(key_name)
            logger.info("SSHKeyManager: deleted key '%s' from PG backend", key_name)
        except Exception:
            logger.exception(
                "SSHKeyManager: failed to delete key '%s' from PG backend",
                key_name,
            )
            raise

    def delete_key(self, key_name: str) -> bool:
        """
        Delete an SSH key, its config entries, and metadata.

        Args:
            key_name: Name of the key to delete

        Returns:
            True on success, including the idempotent case where no
            metadata AND no on-disk file exist for key_name (nothing to
            delete). Returns False when neither node-local metadata NOR the
            shared cluster backend (Bug #1527) has a record of key_name but a
            same-named file is present in ssh_dir (Bug #1519 provenance
            guard) -- this service never proved it wrote that file, so it
            refuses to delete it rather than blindly trusting the name.
        """

        with self._get_lock():
            untracked_file_blocked = False

            if self._use_sqlite and self._sqlite_backend is not None:
                # SQLite backend (Story #702)
                key_data = self._sqlite_backend.get_key(key_name)

                if key_data:
                    self._unlink_key_files(
                        key_data["private_path"], key_data["public_path"]
                    )
                else:
                    untracked_file_blocked = self._delete_cluster_managed_or_refuse(
                        key_name
                    )

                # Remove from SQLite (cascade deletes hosts)
                self._sqlite_backend.delete_key(key_name)
            else:
                # JSON file storage (backward compatible)
                metadata = self._load_metadata(key_name)

                if metadata:
                    self._unlink_key_files(metadata.private_path, metadata.public_path)
                else:
                    untracked_file_blocked = self._delete_cluster_managed_or_refuse(
                        key_name
                    )

                # Remove metadata file
                metadata_path = self.metadata_dir / f"{key_name}.json"
                if metadata_path.exists():
                    metadata_path.unlink()

            # Cluster mode: remove from the shared backend so the key cannot
            # resurrect on the next sync.  Applies to both storage modes above.
            self._delete_from_cluster_backend(key_name)

            # Update SSH config to remove entries
            self._update_ssh_config()

            return not untracked_file_blocked

    def list_keys(self) -> KeyListResult:
        """
        List all managed and unmanaged SSH keys.

        Returns:
            KeyListResult with managed and unmanaged key lists
        """

        return self._list_keys_internal()

    def _list_keys_internal(self) -> KeyListResult:
        """
        Internal implementation of list_keys without API metrics tracking.

        Used by _update_ssh_config() to avoid double-counting API calls.

        Returns:
            KeyListResult with managed and unmanaged key lists
        """
        if self._use_sqlite and self._sqlite_backend is not None:
            # SQLite backend (Story #702)
            keys_data = self._sqlite_backend.list_keys()
            managed_keys = [self._key_metadata_from_backend(kd) for kd in keys_data]
        else:
            # JSON file storage (backward compatible)
            managed_keys = []
            if self.metadata_dir.exists():
                for metadata_file in self.metadata_dir.glob("*.json"):
                    try:
                        data = json.loads(metadata_file.read_text())
                        managed_keys.append(KeyMetadata(**data))
                    except (json.JSONDecodeError, TypeError):
                        continue

        # --- Cluster mode: union with the shared backend's truth (Bug #1524) ---
        managed_keys = self._merge_cluster_managed_keys(managed_keys)

        # Discover all keys on filesystem
        all_discovered = self.discovery_service.discover_existing_keys()
        # KeyMetadata.private_path is declared `str`; str() makes the
        # string-to-string comparison below explicit at the call site.
        managed_paths = {str(k.private_path) for k in managed_keys}

        # Find unmanaged keys
        unmanaged_keys: List[KeyInfo] = []
        for key_info in all_discovered:
            if str(key_info.private_path) not in managed_paths:
                unmanaged_keys.append(key_info)

        return KeyListResult(managed=managed_keys, unmanaged=unmanaged_keys)

    def _local_materialized_paths(self, key_name: str) -> Optional[Tuple[str, str]]:
        """Resolve where a cluster-tracked key named ``key_name`` lives on THIS node.

        Returns ``(private_path, public_path)`` as strings, or None when
        ``key_name`` cannot be trusted as a path component.  Key names reach
        this method from a shared backend row, so they are never assumed safe:

        1. The name must be a BARE FILENAME -- ``key_name == Path(key_name).name``
           rejects anything carrying a separator (``a/b``, ``foo/../bar``), the
           traversal names ``.``/``..``, and absolute paths, before any
           filesystem call happens.
        2. Both resolved paths must still be direct children of
           ``self.ssh_dir`` -- defense in depth against a symlinked ssh_dir
           entry, using the same containment technique
           ``_has_untracked_conflicting_file`` applies for Bug #1519.
        """
        if not key_name or key_name != Path(key_name).name:
            logger.warning(
                "SSHKeyManager: cluster key name %r is not a bare filename -- "
                "excluding it from this node's key list",
                key_name,
            )
            return None

        private_path = self.ssh_dir / key_name
        public_path = self.ssh_dir / f"{key_name}.pub"
        ssh_dir_resolved = self.ssh_dir.resolve()

        if not (
            private_path.resolve().parent == ssh_dir_resolved
            and public_path.resolve().parent == ssh_dir_resolved
        ):
            logger.warning(
                "SSHKeyManager: cluster key name %r does not resolve to a direct "
                "child of %s -- excluding it from this node's key list",
                key_name,
                self.ssh_dir,
            )
            return None

        return str(private_path), str(public_path)

    def _merge_cluster_managed_keys(
        self, local_keys: List[KeyMetadata]
    ) -> List[KeyMetadata]:
        """Union node-local managed keys with the shared cluster backend's keys.

        Bug #1524: without this, "managed vs unmanaged" was decided purely from
        node-local state, so a key created on one cluster node was reported
        managed there and unmanaged on every other node -- for the identical
        key, in the identical cluster, at the identical moment.  Every mutating
        operation in this class (create_key, assign_key_to_host, delete_key)
        already treats the shared backend as cluster truth; the read path must
        agree with them.

        A locally-tracked key keeps its local record verbatim -- only names the
        node does not know locally are added.  Those cluster-only entries are
        rebased onto THIS node's materialized paths (``ssh_dir/<name>``), since
        the originating node's recorded ``private_path`` is meaningless here;
        that is the same convention ``SSHKeySyncService`` uses when it writes
        the key files and the ``~/.ssh/config`` IdentityFile lines.

        Solo mode (no ``_pg_backend``) returns ``local_keys`` unchanged, so
        behavior there is byte-identical to before this fix.

        Raises:
            Exception: whatever the backend raised.  Mirrors this class's
                existing policy for the shared backend (log then re-raise):
                silently degrading to the node-local view is exactly the
                divergence this method exists to prevent.
        """
        pg_backend = self._pg_backend
        if pg_backend is None:
            return local_keys

        try:
            cluster_rows = pg_backend.list_keys()
        except Exception:
            logger.exception(
                "SSHKeyManager: failed to list keys from PG backend",
            )
            raise

        merged = list(local_keys)
        known_names = {key.name for key in merged}
        for row in cluster_rows:
            # known_names holds strings, so a non-string name never matches
            # here and falls through to the validation below.
            if row.get("name") in known_names:
                continue
            metadata = self._cluster_row_to_local_metadata(row)
            if metadata is None:
                continue
            known_names.add(metadata.name)
            merged.append(metadata)

        return merged

    def _cluster_row_to_local_metadata(self, row: dict) -> Optional[KeyMetadata]:
        """Rebase ONE shared-backend key row onto THIS node's materialized paths.

        The originating node's recorded ``private_path``/``public_path`` are
        meaningless here, so they are replaced with ``ssh_dir/<name>`` -- the
        same convention ``SSHKeySyncService`` uses when it writes the key files
        and the ``~/.ssh/config`` IdentityFile lines.

        Returns None when the row cannot be trusted: a non-string name, or a
        name ``_local_materialized_paths`` refuses as a path component.  Keeping
        that validation in one place is why every cluster read -- the list path
        and the single-key lookups alike -- goes through this method.
        """
        name = row.get("name")
        if not isinstance(name, str):
            logger.warning(
                "SSHKeyManager: cluster key row has a non-string name (%r) -- "
                "excluding it from this node's key list",
                name,
            )
            return None

        local_paths = self._local_materialized_paths(name)
        if local_paths is None:
            return None

        metadata = self._key_metadata_from_backend(row)
        metadata.private_path, metadata.public_path = local_paths
        return metadata

    def _cluster_managed_key_metadata(self, key_name: str) -> Optional[KeyMetadata]:
        """Look ONE key up in the shared cluster backend (Bug #1526).

        Bug #1524 made the list path union the shared backend's keys into this
        node's view.  Without this method the single-key paths
        (``get_public_key``, ``assign_key_to_host``) still answered from
        node-local state alone, so the SAME node reported a key as ``managed``
        and simultaneously denied its existence -- whenever the key had not been
        materialized here yet (before ``SSHKeySyncService.sync()`` next runs, or
        on a node that never independently pulled it down).

        Returns None in solo mode (no ``_pg_backend``), when the shared backend
        does not know the name, or when the row fails
        ``_cluster_row_to_local_metadata``'s validation.  Callers translate None
        into the same ``KeyNotFoundError`` they raised before this fix, so solo
        behavior is byte-identical.

        Raises:
            Exception: whatever the backend raised.  Mirrors this class's
                existing policy for the shared backend (log then re-raise):
                silently degrading to the node-local view is exactly the
                divergence this method exists to prevent.
        """
        pg_backend = self._pg_backend
        if pg_backend is None:
            return None

        try:
            row = pg_backend.get_key(key_name)
        except Exception:
            logger.exception(
                "SSHKeyManager: failed to read key '%s' from PG backend", key_name
            )
            raise

        if not row:
            return None

        return self._cluster_row_to_local_metadata(row)

    @staticmethod
    def _key_metadata_from_backend(data: dict) -> "KeyMetadata":
        """
        Construct a KeyMetadata from a dict returned by the SQLite/PG backend.

        Strips 'private_key': it is an internal encrypted blob for cluster sync
        (Bug #1072 Chunk 1) and is not a field on KeyMetadata.
        """
        return KeyMetadata(**{k: v for k, v in data.items() if k != "private_key"})

    def get_public_key(self, key_name: str) -> str:
        """
        Get the public key content for copy/paste.

        Args:
            key_name: Name of the key

        Returns:
            Public key string
        """

        if self._use_sqlite and self._sqlite_backend is not None:
            # SQLite backend (Story #702)
            key_data = self._sqlite_backend.get_key(key_name)
            if key_data is None:
                return self._cluster_public_key(key_name)
            public_path_str = str(key_data["public_path"])
        else:
            # JSON file storage (backward compatible)
            metadata = self._load_metadata(key_name)
            if metadata is None:
                return self._cluster_public_key(key_name)
            public_path_str = metadata.public_path

        public_path = Path(public_path_str)
        if public_path.exists():
            return public_path.read_text().strip()

        raise PublicKeyNotFoundError(f"Public key file missing: {public_path_str}")

    def _cluster_public_key(self, key_name: str) -> str:
        """Public key of a cluster-managed key this node does not track locally.

        Bug #1526.  Prefers this node's own materialized ``<name>.pub`` file when
        the sync service has already written it; otherwise answers from the
        shared backend's ``public_key`` column, which is cluster truth for
        exactly this value -- a public key is not a secret, and that row is the
        very record ``SSHKeySyncService`` writes the file from.

        Raises:
            KeyNotFoundError: unknown locally AND in the cluster.  Also the
                unconditional solo-mode outcome, so that path is unchanged.
            PublicKeyNotFoundError: the key exists cluster-wide but no public
                material is available on this node yet.  Deliberately a
                different failure from "no such key": callers act on them
                differently.
        """
        metadata = self._cluster_managed_key_metadata(key_name)
        if metadata is None:
            raise KeyNotFoundError(f"Key not found: {key_name}")

        public_path = Path(metadata.public_path)
        if public_path.exists():
            return public_path.read_text().strip()

        if metadata.public_key:
            return metadata.public_key.strip()

        raise PublicKeyNotFoundError(f"Public key file missing: {metadata.public_path}")

    def _update_ssh_config(self) -> None:
        """Update SSH config with all managed key-host mappings."""
        all_keys = self._list_keys_internal()

        entries: List[HostEntry] = []
        for metadata in all_keys.managed:
            for hostname in metadata.hosts:
                entries.append(
                    HostEntry(
                        host=hostname,
                        hostname=hostname,
                        key_path=metadata.private_path,
                    )
                )

        # Parse existing config to preserve user section
        parsed = self.config_manager.parse_config(self.config_path)

        # Write updated config
        self.config_manager.write_config(self.config_path, parsed, entries)

    def _save_metadata(self, metadata: KeyMetadata) -> None:
        """Save key metadata to JSON file."""
        if not self.metadata_dir.exists():
            self.metadata_dir.mkdir(parents=True, mode=0o700)

        metadata_path = self.metadata_dir / f"{metadata.name}.json"
        data = asdict(metadata)
        metadata_path.write_text(json.dumps(data, indent=2))
        os.chmod(metadata_path, 0o600)

    def _load_metadata(self, key_name: str) -> Optional[KeyMetadata]:
        """Load key metadata from JSON file."""
        metadata_path = self.metadata_dir / f"{key_name}.json"
        if not metadata_path.exists():
            return None

        try:
            data = json.loads(metadata_path.read_text())
            return KeyMetadata(**data)
        except (json.JSONDecodeError, TypeError):
            return None
