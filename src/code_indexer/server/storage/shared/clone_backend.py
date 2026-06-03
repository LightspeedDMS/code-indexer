"""
Clone backend abstraction for versioned snapshot creation and deletion.

Provides a Protocol-based interface (CloneBackend) with three implementations:

- LocalCloneBackend: filesystem Copy-on-Write via ``cp --reflink=auto``
- OntapCloneBackend: delegates to OntapFlexCloneClient for ONTAP volumes
- CowDaemonBackend: REST client for the CoW Storage Daemon

Story #510 — CloneBackend Abstraction and CoW Daemon Integration.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from code_indexer.server.utils.config_manager import (
        CowDaemonConfig,
        OntapConfig,
    )  # pragma: no cover
    from code_indexer.server.storage.shared.ontap_flexclone_client import (
        OntapFlexCloneClient,
    )  # pragma: no cover

try:
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover  # Python < 3.8 fallback (not expected but safe)
    from typing_extensions import Protocol, runtime_checkable  # type: ignore  # pragma: no cover

logger = logging.getLogger(__name__)

# Maximum exponential-backoff ceiling for poll interval (seconds)
_MAX_COW_DAEMON_POLL_INTERVAL_SECONDS = 30

# Allowed characters for namespace and name path components (no traversal chars)
_SAFE_PATH_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_path_component(value: str, field: str) -> None:
    """Raise ValueError if *value* contains characters unsafe for path construction."""
    if not _SAFE_PATH_COMPONENT_RE.match(value):
        raise ValueError(
            f"{field} contains invalid characters. "
            f"Only alphanumeric, '.', '_', and '-' are allowed. Got: {value!r}"
        )


# ---------------------------------------------------------------------------
# Protocol definition
# ---------------------------------------------------------------------------


@runtime_checkable
class CloneBackend(Protocol):
    """Structural interface for clone lifecycle operations.

    All implementations must provide create_clone, delete_clone, list_clones,
    and clone_exists.  The Protocol uses structural subtyping (typing.Protocol),
    NOT inheritance from abc.ABC.
    """

    def create_clone(self, source_path: str, namespace: str, name: str) -> str:
        """Create a clone and return its absolute filesystem path."""
        ...  # pragma: no cover

    def create_clone_at_path(
        self,
        source_path: str,
        dest_path: str,
        preserve_attrs: bool = True,
        timeout: Optional[int] = None,
    ) -> str:
        """Clone source_path to caller-specified dest_path. Returns dest_path."""
        ...  # pragma: no cover

    def delete_clone(self, clone_path: str) -> bool:
        """Delete a clone by its absolute path. Returns True on success or if already absent."""
        ...  # pragma: no cover

    def list_clones(self, namespace: str) -> List[dict]:
        """Return a list of clone dicts for the given namespace."""
        ...  # pragma: no cover

    def clone_exists(self, namespace: str, name: str) -> bool:
        """Return True if the clone identified by namespace/name exists."""
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# LocalCloneBackend
# ---------------------------------------------------------------------------


class LocalCloneBackend:
    """Filesystem CoW clone backend using ``cp --reflink=auto``.

    Clones are stored under ``{versioned_base}/.versioned/{namespace}/{name}``
    when versioned_base is provided.  Callers that only use
    ``create_clone_at_path()`` may construct ``LocalCloneBackend()`` without
    supplying *versioned_base*; calling ``create_clone()`` or ``list_clones()``
    without it raises ``RuntimeError``.
    """

    def __init__(self, versioned_base: Optional[str] = None) -> None:
        self._versioned_base = versioned_base

    def _clone_path(self, namespace: str, name: str) -> Path:
        if self._versioned_base is None:
            raise RuntimeError(
                "LocalCloneBackend.create_clone() requires versioned_base. "
                "Constructed without versioned_base — use create_clone_at_path() instead."
            )
        _validate_path_component(namespace, "namespace")
        _validate_path_component(name, "name")
        return Path(self._versioned_base) / ".versioned" / namespace / name

    def create_clone(
        self, source_path: str, namespace: str, name: str, timeout: Optional[int] = None
    ) -> str:
        """Create a CoW directory clone using ``cp --reflink=auto -a``."""
        dest = self._clone_path(namespace, name)
        dest.parent.mkdir(parents=True, exist_ok=True)

        logger.info(
            "LocalCloneBackend: creating clone '%s/%s' from '%s'",
            namespace,
            name,
            source_path,
        )
        subprocess.run(
            ["cp", "--reflink=auto", "-a", source_path, str(dest)],
            check=True,
            capture_output=True,
            timeout=timeout,
        )
        return str(dest)

    def create_clone_at_path(
        self,
        source_path: str,
        dest_path: str,
        preserve_attrs: bool = True,
        timeout: Optional[int] = None,
    ) -> str:
        """Clone source_path to caller-specified dest_path. Returns dest_path."""
        attr_flag = "-a" if preserve_attrs else "-r"
        logger.info(
            "LocalCloneBackend: creating clone at path '%s' from '%s'",
            dest_path,
            source_path,
        )
        subprocess.run(
            ["cp", "--reflink=auto", attr_flag, source_path, dest_path],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return dest_path

    def delete_clone(self, clone_path: str) -> bool:
        """Remove the clone directory tree.

        Returns True when deletion succeeded or the path did not exist.
        Returns False when an OSError other than ENOENT occurs.
        """
        path = Path(clone_path)
        if not path.exists():
            return True
        try:
            shutil.rmtree(str(path))
            return True
        except OSError as exc:
            logger.error(
                "LocalCloneBackend: failed to delete clone '%s': %s",
                clone_path,
                exc,
            )
            return False

    def list_clones(self, namespace: str) -> List[dict]:
        """Return one dict per subdirectory of ``.versioned/{namespace}/``."""
        if self._versioned_base is None:
            raise RuntimeError(
                "LocalCloneBackend.list_clones() requires versioned_base. "
                "Constructed without versioned_base — use create_clone_at_path() instead."
            )
        _validate_path_component(namespace, "namespace")
        ns_dir = Path(self._versioned_base) / ".versioned" / namespace
        if not ns_dir.exists():
            return []
        result = []
        for entry in ns_dir.iterdir():
            if entry.is_dir():
                result.append(
                    {
                        "namespace": namespace,
                        "name": entry.name,
                        "clone_path": str(entry),
                    }
                )
        return result

    def clone_exists(self, namespace: str, name: str) -> bool:
        """Return True when the clone directory is present on disk."""
        return self._clone_path(namespace, name).exists()


# ---------------------------------------------------------------------------
# OntapCloneBackend
# ---------------------------------------------------------------------------


class OntapCloneBackend:
    """Clone backend backed by ONTAP FlexClone volumes.

    Delegates all operations to an :class:`OntapFlexCloneClient`.
    The *mount_point* is where ONTAP volumes are NFS-mounted.
    """

    def __init__(
        self, flexclone_client: "OntapFlexCloneClient", mount_point: str
    ) -> None:
        self._client = flexclone_client
        self._mount_point = mount_point.rstrip("/")

    def create_clone(self, _source_path: str, namespace: str, name: str) -> str:
        """Create a FlexClone volume and return its mount path.

        *_source_path* is unused: ONTAP clones from the parent volume
        configured on the OntapFlexCloneClient, not from a filesystem path.
        """
        junction_path = f"/{name}"
        self._client.create_clone(name, junction_path=junction_path)
        return f"{self._mount_point}/{name}"

    def create_clone_at_path(
        self,
        source_path: str,
        dest_path: str,
        preserve_attrs: bool = True,
        timeout: Optional[int] = None,
    ) -> str:
        """Not supported by ONTAP backend (uses volume-level cloning, not path-specified)."""
        raise NotImplementedError(
            "OntapCloneBackend does not support create_clone_at_path; "
            "ONTAP uses volume-level FlexClone, not caller-specified paths."
        )

    def delete_clone(self, clone_path: str) -> bool:
        """Delete the FlexClone volume whose name is the basename of *clone_path*."""
        clone_name = Path(clone_path).name
        return bool(self._client.delete_clone(clone_name))

    def list_clones(self, namespace: str) -> List[dict]:
        """List FlexClone volumes (delegated to client; ONTAP ignores namespace)."""
        records = self._client.list_clones()
        return list(records)

    def clone_exists(self, namespace: str, name: str) -> bool:
        """Return True when the volume exists according to ONTAP."""
        info = self._client.get_volume_info(name)
        return info is not None


# ---------------------------------------------------------------------------
# CowDaemonBackend
# ---------------------------------------------------------------------------


class CowDaemonBackend:
    """Clone backend that talks to the CoW Storage Daemon via REST.

    The daemon exposes:
    - POST /api/v1/clones — async create, returns job_id
    - GET  /api/v1/jobs/{job_id} — poll for completion
    - DELETE /api/v1/clones/{namespace}/{name} — delete (404 = success)
    - GET /api/v1/clones?namespace={ns} — list
    - GET /api/v1/clones/{namespace}/{name} — 200=exists, 404=missing

    Auth: ``Authorization: Bearer {api_key}`` on every request.
    """

    def __init__(self, config: "CowDaemonConfig") -> None:
        self._daemon_url = config.daemon_url.rstrip("/")
        self._api_key = config.api_key
        self._mount_point = config.mount_point.rstrip("/")
        self._poll_interval = config.poll_interval_seconds
        self._timeout = config.timeout_seconds
        self._daemon_storage_path = (config.daemon_storage_path or "").rstrip("/")

    # Lazy import; direct type annotation is not possible without making requests
    # a hard dependency at import time.
    def _requests(self):  # type: ignore[return]  # lazy import avoids hard dep
        """Return the ``requests`` module (lazy-imported to keep startup fast)."""
        import requests  # noqa: PLC0415

        return requests

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}"}

    @staticmethod
    def _sanitize_identifier(alias: str) -> str:
        """Daemon rejects dots in namespace/name. Replace with underscores."""
        return alias.replace(".", "_")

    def _translate_to_daemon_path(self, cidx_path: str) -> str:
        """Translate CIDX-view path (under mount_point) to daemon-local path (under daemon_storage_path).

        Required because:
        1. Daemon's _validate_dest_path resolves and validates against its local storage_path; CIDX
           paths via NFS mount are rejected as "not under storage_path".
        2. For true XFS reflink, the daemon's cp must use its local-XFS paths on both sides.

        When daemon_storage_path is empty (not configured), returns cidx_path unchanged (backward compat).
        Raises ValueError if cidx_path is not under mount_point (caller must use a path within the mount).
        """
        if not self._daemon_storage_path:
            return cidx_path
        # Bug #1046: resolve symlinks before path-prefix check, and accept
        # paths under EITHER mount_point or daemon_storage_path.
        #
        # On NFS-client nodes (e.g. cluster node 20/22), the golden-repos
        # symlink resolves under mount_point (/mnt/cow-storage/...), needs
        # translation to daemon_storage_path form.
        #
        # On the CoW daemon host (e.g. cluster node 23), the same logical
        # filesystem is reached via daemon_storage_path directly
        # (/home/jsbattig/cow-storage/...) — the symlink resolves there
        # because the local layout points to the source rather than the
        # bind mount. Such paths are ALREADY in daemon-local form, return
        # as-is without re-prefixing.
        resolved = os.path.realpath(cidx_path)
        if (
            resolved.startswith(self._daemon_storage_path + "/")
            or resolved == self._daemon_storage_path
        ):
            return resolved
        if (
            resolved.startswith(self._mount_point + "/")
            or resolved == self._mount_point
        ):
            return self._daemon_storage_path + resolved[len(self._mount_point) :]
        raise ValueError(
            f"CowDaemonBackend.create_clone_at_path: path '{cidx_path}' "
            f"(resolved '{resolved}') is not under mount_point '{self._mount_point}' "
            f"or daemon_storage_path '{self._daemon_storage_path}' "
            f"— cannot translate to daemon view"
        )

    def _translate_from_daemon_path(self, daemon_clone_path: str) -> str:
        """Translate daemon-local clone_path returned by a job back to a CIDX mount-point path.

        The daemon job response contains clone_path as the daemon-local absolute path
        (e.g. ``home/jsbattig/cow-storage/ns/name`` without leading slash, or
        ``/home/jsbattig/cow-storage/ns/name`` with leading slash).

        When daemon_storage_path is configured we strip it and replace with mount_point.
        When daemon_storage_path is empty the clone_path is already relative (``ns/name``)
        and we prepend mount_point directly (original behaviour).
        """
        if not self._daemon_storage_path:
            # Original behaviour: clone_path is "namespace/name" relative
            return f"{self._mount_point}/{daemon_clone_path}"
        # Normalise: ensure absolute path form for comparison
        absolute_clone = (
            daemon_clone_path
            if daemon_clone_path.startswith("/")
            else f"/{daemon_clone_path}"
        )
        daemon_prefix = self._daemon_storage_path  # already rstripped of "/"
        if (
            absolute_clone.startswith(daemon_prefix + "/")
            or absolute_clone == daemon_prefix
        ):
            suffix: str = absolute_clone[len(daemon_prefix) :]
            return str(self._mount_point) + suffix
        # Fallback: daemon returned an unexpected path — prefix mount_point as-is
        logger.warning(
            "CowDaemonBackend._translate_from_daemon_path: clone_path '%s' is not under "
            "daemon_storage_path '%s' — returning mount_point-prefixed path",
            daemon_clone_path,
            self._daemon_storage_path,
        )
        return f"{self._mount_point}/{daemon_clone_path}"

    def create_clone(
        self,
        source_path: str,
        namespace: str,
        name: str,
        timeout: Optional[int] = None,
    ) -> str:
        """POST to create a clone and poll until completed. Returns absolute path.

        Sanitizes namespace and name (replaces dots with underscores) so aliases containing
        dots (e.g. langfuse_Claude_Code_seba.battig_lightspeeddms.com) pass daemon validation.
        Daemon stores at {base_path}/{sanitized_ns}/{sanitized_name}; returned path uses
        mount_point view for CIDX-side consumption.
        """
        requests = self._requests()
        sanitized_namespace = self._sanitize_identifier(namespace)
        sanitized_name = self._sanitize_identifier(name)
        body = {
            "source_path": source_path,
            "namespace": sanitized_namespace,
            "name": sanitized_name,
        }
        logger.info(
            "CowDaemonBackend.create_clone: source=%s namespace=%s (sanitized from %s) name=%s",
            source_path,
            sanitized_namespace,
            namespace,
            sanitized_name,
        )
        response = requests.post(
            f"{self._daemon_url}/api/v1/clones",
            json=body,
            headers=self._headers(),
        )
        response.raise_for_status()

        job_id = response.json()["job_id"]
        effective_timeout = timeout if timeout is not None else self._timeout
        clone_path = self._poll_job(job_id, effective_timeout)
        return self._translate_from_daemon_path(clone_path)

    def create_clone_at_path(
        self,
        source_path: str,
        dest_path: str,
        preserve_attrs: bool = True,
        timeout: Optional[int] = None,
    ) -> str:
        """POST to create a clone at caller-specified dest_path. Returns dest_path (CIDX-view path)."""
        requests = self._requests()
        # Translate CIDX-view paths to daemon-local paths so daemon validation passes and reflink works
        daemon_source = self._translate_to_daemon_path(source_path)
        daemon_dest = self._translate_to_daemon_path(dest_path)
        namespace = self._sanitize_identifier(Path(dest_path).parent.name)
        name = self._sanitize_identifier(Path(dest_path).name)
        body = {
            "source_path": daemon_source,
            "namespace": namespace,
            "name": name,
            "dest_path": daemon_dest,
        }
        logger.info(
            "CowDaemonBackend: creating clone at path '%s' (daemon-side '%s') from '%s' (daemon-side '%s')",
            dest_path,
            daemon_dest,
            source_path,
            daemon_source,
        )
        response = requests.post(
            f"{self._daemon_url}/api/v1/clones",
            json=body,
            headers=self._headers(),
        )
        response.raise_for_status()

        job_id = response.json()["job_id"]
        effective_timeout = timeout if timeout is not None else self._timeout
        self._poll_job(job_id, effective_timeout)
        return dest_path  # Return CIDX-view path; caller does file ops via NFS mount

    def _poll_job(self, job_id: str, timeout: Optional[float] = None) -> str:
        """Poll GET /api/v1/jobs/{job_id} until completed or timeout. Returns clone_path."""
        requests = self._requests()
        effective_timeout = timeout if timeout is not None else self._timeout
        deadline = time.monotonic() + effective_timeout
        interval = self._poll_interval

        while time.monotonic() < deadline:
            resp = requests.get(
                f"{self._daemon_url}/api/v1/jobs/{job_id}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status", "")

            if status == "completed":
                return str(data["clone_path"])
            if status == "failed":
                raise RuntimeError(
                    f"CoW daemon job {job_id} failed: {data.get('error', 'unknown error')}"
                )

            time.sleep(interval)
            interval = min(interval * 2, _MAX_COW_DAEMON_POLL_INTERVAL_SECONDS)

        raise TimeoutError(
            f"CoW daemon job {job_id} did not complete within {effective_timeout}s"
        )

    def delete_clone(self, clone_path: str) -> bool:
        """DELETE /api/v1/clones/{namespace}/{name}. 404 counts as success (idempotent)."""
        requests = self._requests()
        mount = Path(self._mount_point)
        path = Path(clone_path)

        # Derive namespace/name from path relative to mount point
        try:
            relative = path.relative_to(mount)
        except ValueError:
            raise ValueError(
                f"clone_path '{clone_path}' is not under mount_point '{self._mount_point}'"
            )

        parts = relative.parts
        if len(parts) < 2:
            raise ValueError(
                f"clone_path '{clone_path}' must have at least namespace/name under mount_point"
            )
        namespace = parts[0]
        name = parts[1]

        resp = requests.delete(
            f"{self._daemon_url}/api/v1/clones/{namespace}/{name}",
            headers=self._headers(),
        )
        if resp.status_code == 404:
            return True
        resp.raise_for_status()
        return True

    def list_clones(self, namespace: str) -> List[dict]:
        """GET /api/v1/clones?namespace={namespace}. Returns list of clone dicts."""
        requests = self._requests()
        resp = requests.get(
            f"{self._daemon_url}/api/v1/clones",
            params={"namespace": namespace},
            headers=self._headers(),
        )
        resp.raise_for_status()
        return list(resp.json())

    def clone_exists(self, namespace: str, name: str) -> bool:
        """GET /api/v1/clones/{namespace}/{name}. Returns True=200, False=404."""
        requests = self._requests()
        resp = requests.get(
            f"{self._daemon_url}/api/v1/clones/{namespace}/{name}",
            headers=self._headers(),
        )
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True


# ---------------------------------------------------------------------------
# CloneBackendFactory
# ---------------------------------------------------------------------------


class CloneBackendFactory:
    """Creates the appropriate CloneBackend from configuration values."""

    _VALID_BACKENDS = ("local", "ontap", "cow-daemon")

    @staticmethod
    def create(
        clone_backend_type: str,
        versioned_base: str = "",
        ontap_config: Optional["OntapConfig"] = None,
        cow_daemon_config: Optional["CowDaemonConfig"] = None,
    ) -> CloneBackend:
        """Instantiate and return the requested CloneBackend.

        Parameters
        ----------
        clone_backend_type:
            One of ``"local"``, ``"ontap"``, or ``"cow-daemon"``.
        versioned_base:
            Base directory for LocalCloneBackend.
        ontap_config:
            OntapConfig instance, required when ``clone_backend_type="ontap"``.
        cow_daemon_config:
            CowDaemonConfig instance, required when ``clone_backend_type="cow-daemon"``.

        Raises
        ------
        ValueError
            When ``clone_backend_type`` is not one of the valid options, or
            when a required config object is missing for the chosen backend.
        """
        if clone_backend_type == "local":
            return LocalCloneBackend(versioned_base=versioned_base)

        if clone_backend_type == "ontap":
            from code_indexer.server.storage.shared.ontap_flexclone_client import (
                OntapFlexCloneClient,
            )

            if ontap_config is None:
                raise ValueError(
                    "ontap_config is required when clone_backend_type='ontap'"
                )
            client = OntapFlexCloneClient(
                endpoint=ontap_config.endpoint,
                username=ontap_config.admin_user,
                password=ontap_config.admin_password,
                svm_name=ontap_config.svm_name,
                parent_volume=ontap_config.parent_volume,
            )
            return OntapCloneBackend(
                flexclone_client=client,
                mount_point=ontap_config.mount_point,
            )

        if clone_backend_type == "cow-daemon":
            if cow_daemon_config is None:
                raise ValueError(
                    "cow_daemon_config is required when clone_backend_type='cow-daemon'"
                )
            return CowDaemonBackend(config=cow_daemon_config)

        raise ValueError(
            f"Unsupported clone_backend type: '{clone_backend_type}'. "
            f"Valid options: {', '.join(CloneBackendFactory._VALID_BACKENDS)}"
        )
