"""
SSH Key Cluster Sync Service.

Story #428: Per-node sync service that reads SSH key metadata from PostgreSQL
(or any SSHKeysBackend) and writes key files to local ~/.ssh/.

Tracks which keys it manages via a manifest JSON file so it can remove stale
entries on the next sync without touching keys it never created.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Set

from code_indexer.server.services.ssh_config_manager import (
    HostEntry,
    SSHConfigManager,
)

logger = logging.getLogger(__name__)


class SSHKeySyncService:
    """Syncs SSH keys from a backend (PG/SQLite) to local filesystem."""

    def __init__(
        self,
        ssh_keys_backend: Any,
        ssh_dir: str = "~/.ssh",
        fernet: Any = None,
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
        """
        self._backend = ssh_keys_backend
        self._ssh_dir = Path(ssh_dir).expanduser()
        self._manifest_file = self._ssh_dir / ".cidx-ssh-keys.json"
        self._config_path = self._ssh_dir / "config"
        self._config_manager = SSHConfigManager()
        self._fernet = fernet

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

        # Remove stale keys — managed by us but no longer in backend
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

        # Update manifest with current backend names
        self._update_manifest(backend_names)

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

    def _get_managed_keys(self) -> Set[str]:
        """
        Read manifest of CIDX-managed key names.

        Returns:
            Set of key names previously written by this service.
            Returns empty set if manifest does not exist or is unreadable.
        """
        if not self._manifest_file.exists():
            return set()
        try:
            data = json.loads(self._manifest_file.read_text())
            return set(data.get("keys", []))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"Cannot read SSH key manifest {self._manifest_file}: {exc}")
            return set()

    def _update_manifest(self, keys: Set[str]) -> None:
        """
        Persist the set of CIDX-managed key names to the manifest file.

        Args:
            keys: Set of key names currently managed by this service.
        """
        try:
            data = {"keys": sorted(keys)}
            self._manifest_file.write_text(json.dumps(data, indent=2))
            os.chmod(self._manifest_file, 0o600)
        except OSError as exc:
            logger.error(f"Failed to update SSH key manifest: {exc}")
