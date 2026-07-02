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
from typing import Any, Dict, Set

logger = logging.getLogger(__name__)


class SSHKeySyncService:
    """Syncs SSH keys from a backend (PG/SQLite) to local filesystem."""

    def __init__(self, ssh_keys_backend: Any, ssh_dir: str = "~/.ssh") -> None:
        """
        Initialize the sync service.

        Args:
            ssh_keys_backend: Any object with a list_keys() method that returns
                              a list of dicts with at least: name, private_key
                              (or public_key), private_path, public_path.
            ssh_dir: Directory to write SSH key files into.
                     Defaults to ~/.ssh (expanded at init time).
        """
        self._backend = ssh_keys_backend
        self._ssh_dir = Path(ssh_dir).expanduser()
        self._manifest_file = self._ssh_dir / ".cidx-ssh-keys.json"

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
                private_key = key_data.get("private_key") or key_data.get(
                    "private_key_content"
                )
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

        return {
            "written": written,
            "removed": removed,
            "unchanged": unchanged,
            "errors": errors,
        }

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
