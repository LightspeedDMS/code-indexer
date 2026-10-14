"""
Clone backend abstraction for versioned snapshot creation and deletion.

Provides a Protocol-based interface (CloneBackend) with three implementations:

- LocalCloneBackend: filesystem Copy-on-Write via ``cp --reflink=auto``
- OntapCloneBackend: delegates to OntapFlexCloneClient for ONTAP volumes
- CowDaemonBackend: REST client for the CoW Storage Daemon

Story #510 — CloneBackend Abstraction and CoW Daemon Integration.
"""

from __future__ import annotations
from code_indexer.utils.remote_endpoint import RemoteServiceEndpoint

import errno
from dataclasses import dataclass
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, Optional, Tuple

from code_indexer.global_repos.snapshot_deletion_errors import (
    SnapshotDeleteError,
    SnapshotInUseError,
)
from code_indexer.server.storage.shared.nfs_visibility import (
    _configured_visibility_timeout,
    wait_for_nfs_visibility,
)
from code_indexer.server.utils.cancellable_subprocess import (
    SHORT_POLL_SECONDS,
    SubprocessCancelledError,
    run_cancellable_subprocess,
)
from code_indexer.utils.subprocess_diagnostics import redact_url_credentials

if TYPE_CHECKING:
    from requests import Response  # pragma: no cover

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

#: Bug #1844: errnos meaning "the tree could not be removed because something
#: still holds it", as opposed to "this delete is broken". ENOTEMPTY is the
#: shape an NFS silly-rename leaves behind (unlinking a file the client still
#: holds open renames it to .nfsXXXX, so the parent is not empty at rmdir
#: time); EBUSY and ETXTBSY are the local equivalents. Deliberately identical
#: to the CoW daemon's own _RETRYABLE_DELETE_ERRNOS so both ends of the REST
#: call agree on what "still in use" means.
_RETRYABLE_DELETE_ERRNOS = frozenset({errno.ENOTEMPTY, errno.EBUSY, errno.ETXTBSY})


def _validate_path_component(value: str, field: str) -> None:
    """Raise ValueError if *value* contains characters unsafe for path construction."""
    if not _SAFE_PATH_COMPONENT_RE.match(value):
        raise ValueError(
            f"{field} contains invalid characters. "
            f"Only alphanumeric, '.', '_', and '-' are allowed. Got: {value!r}"
        )


class CloneDestinationExistsError(FileExistsError):
    """Raised when create_clone_at_path refuses an already-existing
    DIRECTORY destination.

    A DEDICATED subtype (not a bare FileExistsError) so callers such as
    ActivatedRepoManager._clone_with_copy_on_write can catch it
    SPECIFICALLY, before their generic `except Exception` clause, and
    skip the destructive shutil.rmtree(dest_path)/orphan-cleanup-grace-
    loop path entirely -- dest_path here is a PRE-EXISTING directory
    that was never touched by this clone attempt, not a partial/failed
    clone of THIS attempt's own making, so it must never be deleted.
    """


@dataclass(frozen=True)
class WorkingCopyStat:
    """What workspace-mcp reports for an activated working-copy path."""

    exists: bool
    is_dir: bool


def _reject_existing_directory_destination(dest_path: str, backend_name: str) -> None:
    """Refuse to clone into an already-existing DIRECTORY destination.

    ``cp --reflink=auto -a src existing_dir`` copies src INTO existing_dir
    as a subdirectory and exits 0 instead of failing -- exactly the
    behaviour that would let a traversed
    activation destination "succeed" instead of erroring loudly (e.g. a
    traversed activation landing inside the shared golden-repos tree).

    Deliberately scoped to an existing DIRECTORY only: refresh_integrity_gate.py's
    restore_chunks_db_via_reflink / restore_metadata_files_via_reflink
    intentionally clone a healthy file ONTO an existing corrupt/stale
    regular file (self-heal, overwrite-in-place) -- ordinary `cp -a`
    file-to-file semantics, not a merge, and must remain allowed.
    """
    if os.path.isdir(dest_path):
        raise CloneDestinationExistsError(
            f"{backend_name}.create_clone_at_path: destination '{dest_path}' "
            "already exists as a directory -- refusing to clone into it "
            "(cp -a would silently merge into it instead of failing)"
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
        cancel_check: Optional[Callable[[], bool]] = None,
        poll_interval: float = SHORT_POLL_SECONDS,
    ) -> str:
        """Clone source_path to caller-specified dest_path. Returns dest_path.

        Bug #1342: cancel_check is an optional zero-arg callable returning
        True when the owning job has been cancelled. Implementations that
        spawn a killable local subprocess (LocalCloneBackend) or poll a
        remote job (CowDaemonBackend) MUST consult it and raise
        SubprocessCancelledError promptly when it fires, instead of blocking
        until natural completion. None disables cancellation (default,
        preserves prior behavior).
        """
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
        cancel_check: Optional[Callable[[], bool]] = None,
        poll_interval: float = SHORT_POLL_SECONDS,
    ) -> str:
        """Clone source_path to caller-specified dest_path. Returns dest_path.

        Bug #1342: runs `cp` via run_cancellable_subprocess (its own process
        group, start_new_session=True) instead of a plain blocking
        subprocess.run. When cancel_check fires, the entire `cp` process
        group is killed and SubprocessCancelledError propagates -- callers
        (ActivatedRepoManager._clone_with_copy_on_write) already rmtree the
        partial dest_path on any exception from this method. The existing
        `timeout` deadline (Bug #1285's cow_clone_timeout) is preserved
        alongside cancel_check -- either one can end the clone.

        Refuses an already-existing
        DIRECTORY destination before running `cp` at all -- see
        _reject_existing_directory_destination for the exact rationale. An
        existing regular file destination remains allowed (overwrite,
        refresh_integrity_gate.py's restore use case).
        """
        _reject_existing_directory_destination(dest_path, "LocalCloneBackend")
        attr_flag = "-a" if preserve_attrs else "-r"
        logger.info(
            "LocalCloneBackend: creating clone at path '%s' from '%s'",
            dest_path,
            source_path,
        )
        result = run_cancellable_subprocess(
            ["cp", "--reflink=auto", attr_flag, source_path, dest_path],
            cwd=None,
            cancel_check=cancel_check,
            poll_interval=poll_interval,
            timeout=timeout,
        )
        if result.returncode != 0:
            # Preserve the previous subprocess.run(check=True) contract.
            raise subprocess.CalledProcessError(
                result.returncode,
                result.args,
                output=redact_url_credentials(result.stdout),
                stderr=redact_url_credentials(result.stderr),
            )
        return dest_path

    def delete_clone(self, clone_path: str) -> bool:
        """Remove the clone directory tree.

        Returns True when deletion succeeded or the path did not exist.

        Bug #1844: an OSError is RAISED (classified), never reported by
        returning False. The old falsey return was discarded by
        ``CleanupManager._delete_index``, so a failed deletion was recorded
        as a success -- the path left the cleanup queue and its durable
        pending-deletion row was deleted, leaking the directory permanently
        and silently. Messi Rule #13.
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
            errno_name = (
                errno.errorcode.get(exc.errno, str(exc.errno))
                if exc.errno is not None
                else type(exc).__name__
            )
            message = f"Failed to delete clone '{clone_path}': {exc}"
            if exc.errno in _RETRYABLE_DELETE_ERRNOS:
                raise SnapshotInUseError(
                    message, errno_name=errno_name, detail=str(exc)
                ) from exc
            raise SnapshotDeleteError(
                message, errno_name=errno_name, detail=str(exc)
            ) from exc

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
        self,
        flexclone_client: "OntapFlexCloneClient",
        mount_point: str,
        visibility_waiter: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._client = flexclone_client
        self._mount_point = mount_point.rstrip("/")
        # Bug #1084 read-after-create barrier: FlexClone volumes are NFS-mounted,
        # so a freshly junctioned clone may not be visible on this node yet.
        # Wait for the mount path before returning. Injectable for tests.
        self._visibility_waiter: Callable[[str], None] = (
            visibility_waiter
            if visibility_waiter is not None
            else (
                lambda path: wait_for_nfs_visibility(
                    path, timeout=_configured_visibility_timeout()
                )
            )
        )

    def create_clone(self, _source_path: str, namespace: str, name: str) -> str:
        """Create a FlexClone volume and return its mount path.

        *_source_path* is unused: ONTAP clones from the parent volume
        configured on the OntapFlexCloneClient, not from a filesystem path.
        """
        junction_path = f"/{name}"
        self._client.create_clone(name, junction_path=junction_path)
        clone_path = f"{self._mount_point}/{name}"
        # Bug #1084: the FlexClone junction is reached over NFS — wait until the
        # mount path is visible on this node before handing it back.
        self._visibility_waiter(clone_path)
        return clone_path

    def create_clone_at_path(
        self,
        source_path: str,
        dest_path: str,
        preserve_attrs: bool = True,
        timeout: Optional[int] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        poll_interval: float = SHORT_POLL_SECONDS,
    ) -> str:
        """Not supported by ONTAP backend (uses volume-level cloning, not path-specified).

        cancel_check/poll_interval accepted for CloneBackend Protocol
        conformance (Bug #1342) but unused -- this method always raises.
        """
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

    def __init__(
        self,
        config: "CowDaemonConfig",
        visibility_waiter: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._daemon_url = config.daemon_url.rstrip("/")
        self._api_key = config.api_key
        self._mount_point = config.mount_point.rstrip("/")
        self._poll_interval = config.poll_interval_seconds
        self._timeout = config.timeout_seconds
        # Bug #1513: per-HTTP-call timeout (connect+read), applied to EVERY
        # requests.* call below. Distinct from self._timeout (the overall
        # job-completion poll deadline) -- this bounds each individual round
        # trip so a lost/dropped daemon response fails loudly and fast
        # instead of hanging the caller forever (production symptom: a
        # CLOSE-WAIT connection and a repo-activation job stuck at 40%
        # indefinitely, with no error ever surfaced).
        self._request_timeout = config.request_timeout_seconds
        self._daemon_storage_path = (config.daemon_storage_path or "").rstrip("/")
        # Bug #1084 read-after-create barrier: the daemon creates the snapshot
        # on its local XFS; this (scheduler) node reaches it over NFS and may
        # have a NEGATIVE dcache entry for the brand-new canonical parent chain.
        # Wait for the returned dest to be visible before handing it back so no
        # consumer (snapshot create, activated-repo clone, ...) ENOENTs on it.
        # Injectable for tests (no real NFS, no real sleeps).
        self._visibility_waiter: Callable[[str], None] = (
            visibility_waiter
            if visibility_waiter is not None
            else (
                lambda path: wait_for_nfs_visibility(
                    path, timeout=_configured_visibility_timeout()
                )
            )
        )

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
        """Sanitize a namespace/name identifier for the CoW daemon.

        The daemon's own server-side validation requires namespace/name
        values to contain only alphanumeric characters, hyphens, and
        underscores. Real cluster usernames are email addresses (e.g.
        ``Jane.Doe@example.com``), so any character outside that
        allowed set -- not just dots -- must be replaced, or the daemon
        rejects the request with HTTP 400 VALIDATION_ERROR (Issue #1465).
        Every disallowed character maps to a single underscore.
        """
        return re.sub(r"[^A-Za-z0-9_-]", "_", alias)

    def _translate_to_daemon_path(self, cidx_path: str) -> str:
        """Translate CIDX-view path (under mount_point) to daemon-local path (under daemon_storage_path).

        Required because:
        1. Daemon's _validate_dest_path resolves and validates against its local storage_path; CIDX
           paths via NFS mount are rejected as "not under storage_path".
        2. For true XFS reflink, the daemon's cp must use its local-XFS paths on both sides.

        When daemon_storage_path is empty (not configured):
        - If mount_point is also unset, or cidx_path does not resolve under mount_point,
          translation is a genuine no-op (nothing to translate) -- returns cidx_path unchanged.
        - If cidx_path DOES resolve under mount_point, translation IS required (the daemon
          only accepts paths under its own local storage root) but impossible without a
          configured daemon_storage_path. Bug #1320: silently returning the untranslatable
          mount-point path here caused the CoW daemon to reject it with HTTP 400
          PATH_NOT_ALLOWED at publish time. Raise a clear, actionable configuration error
          instead of silently passing through a path the daemon is guaranteed to reject.

        Raises ValueError if cidx_path is not under mount_point (caller must use a path within the mount).
        """
        # Bug #1046: resolve symlinks before path-prefix check, and accept
        # paths under EITHER mount_point or daemon_storage_path.
        #
        # On NFS-client nodes (e.g. cluster node 20/22), the golden-repos
        # symlink resolves under mount_point (/mnt/cow-storage/...), needs
        # translation to daemon_storage_path form.
        #
        # On the CoW daemon host (e.g. cluster node 23), the same logical
        # filesystem is reached via daemon_storage_path directly
        # (/home/opuser/cow-storage/...) — the symlink resolves there
        # because the local layout points to the source rather than the
        # bind mount. Such paths are ALREADY in daemon-local form, return
        # as-is without re-prefixing.
        resolved = os.path.realpath(cidx_path)
        path_under_mount_point = self._mount_point and (
            resolved.startswith(self._mount_point + "/")
            or resolved == self._mount_point
        )

        if not self._daemon_storage_path:
            if path_under_mount_point:
                raise ValueError(
                    f"CoW daemon_storage_path is not configured; cannot translate "
                    f"'{cidx_path}' (resolved '{resolved}') under mount_point "
                    f"'{self._mount_point}' to a daemon-local path. Set "
                    f"cow_daemon.daemon_storage_path in config.json to the CoW "
                    f"daemon's local storage root."
                )
            return cidx_path

        if (
            resolved.startswith(self._daemon_storage_path + "/")
            or resolved == self._daemon_storage_path
        ):
            return resolved
        if path_under_mount_point:
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
        (e.g. ``home/opuser/cow-storage/ns/name`` without leading slash, or
        ``/home/opuser/cow-storage/ns/name`` with leading slash).

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
        """Create a versioned snapshot at the CANONICAL layout (Bug #1084).

        Routes through :meth:`create_clone_at_path` with the canonical
        destination ``{mount_point}/.versioned/{sanitized_ns}/{sanitized_name}``
        so that every new cow-daemon snapshot has the same shape as the local
        backend (``.../.versioned/{ns}/v_<ts>``) and is therefore recognized by
        the single canonical predicate. The daemon registers clone identity
        ``(ns, name)`` from the dest parent-dir / leaf names (proven by the
        activated-repos flow, Bug #1052), so the daemon's SQLite registry is
        unaffected by the extra ``.versioned`` path segment.

        Sanitizes namespace and name (dots->underscores) so aliases containing
        dots (e.g. ``langfuse_Claude_Code_jane.doe_...``) pass daemon
        validation. Returns the canonical CIDX mount-point path.
        """
        sanitized_namespace = self._sanitize_identifier(namespace)
        sanitized_name = self._sanitize_identifier(name)
        canonical_dest = (
            f"{self._mount_point}/.versioned/{sanitized_namespace}/{sanitized_name}"
        )
        logger.info(
            "CowDaemonBackend.create_clone: source=%s namespace=%s (sanitized from %s) "
            "name=%s -> canonical dest=%s",
            source_path,
            sanitized_namespace,
            namespace,
            sanitized_name,
            canonical_dest,
        )
        return self.create_clone_at_path(
            source_path, canonical_dest, preserve_attrs=True, timeout=timeout
        )

    def create_clone_at_path(
        self,
        source_path: str,
        dest_path: str,
        preserve_attrs: bool = True,
        timeout: Optional[int] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        poll_interval: float = SHORT_POLL_SECONDS,
    ) -> str:
        """POST to create a clone at caller-specified dest_path. Returns dest_path (CIDX-view path).

        Bug #1342: cancel_check is forwarded to _poll_job. When it fires
        before the remote job completes, best-effort DELETE the (possibly
        still-provisioning) remote clone before re-raising
        SubprocessCancelledError, so a cancelled cluster activation does not
        leak a daemon-side snapshot. A cleanup failure is logged and never
        masks the cancellation. poll_interval exists for CloneBackend
        Protocol symmetry with LocalCloneBackend; the daemon keeps its own
        exponential-backoff poll cadence regardless.

        Refuses an already-existing
        DIRECTORY destination BEFORE any daemon call, mirroring
        LocalCloneBackend -- this codebase's own filesystem view of
        dest_path (over the same NFS mount the daemon serves) is checked
        client-side rather than trusting the remote daemon to enforce it.
        """
        _reject_existing_directory_destination(dest_path, "CowDaemonBackend")
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
            # Bug #1343 AC-3: forward preserve_attrs so the daemon can
            # preserve working-tree mtimes (mirrors LocalCloneBackend's
            # `cp --reflink=auto -a`). Without this, the daemon had no
            # signal at all and the cluster activation path could not
            # benefit from the checkStat=minimal fix applied downstream
            # in ActivatedRepoManager -- an mtime-churning copy re-hashes
            # the whole tree regardless of checkStat.
            "preserve_attrs": preserve_attrs,
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
            timeout=self._request_timeout,
        )
        response.raise_for_status()

        job_id = response.json()["job_id"]
        effective_timeout = timeout if timeout is not None else self._timeout
        try:
            self._poll_job(job_id, effective_timeout, cancel_check=cancel_check)
        except SubprocessCancelledError:
            try:
                self.delete_clone(dest_path)
            except Exception as cleanup_exc:  # noqa: BLE001 -- best-effort
                logger.warning(
                    "CowDaemonBackend: best-effort cleanup of cancelled clone "
                    "'%s' (job %s) failed: %s",
                    dest_path,
                    job_id,
                    cleanup_exc,
                )
            raise
        # Bug #1084: the daemon reported success on ITS local XFS, but this node
        # reads the snapshot over NFS. Bust the negative dcache and wait until the
        # canonical dest is visible here before returning — otherwise downstream
        # subprocess.run(cwd=dest) ENOENTs in a read-after-create race.
        self._visibility_waiter(dest_path)
        return dest_path  # Return CIDX-view path; caller does file ops via NFS mount

    def _poll_job(
        self,
        job_id: str,
        timeout: Optional[float] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> str:
        """Poll GET /api/v1/jobs/{job_id} until completed, cancelled, or timeout.

        Bug #1342: cancel_check is consulted once per iteration, BEFORE the
        HTTP GET, so a cancellation is detected without waiting on the
        (possibly slow) daemon round-trip. Raises SubprocessCancelledError
        when it fires. None (default) preserves prior behavior exactly.
        """
        requests = self._requests()
        effective_timeout = timeout if timeout is not None else self._timeout
        deadline = time.monotonic() + effective_timeout
        interval = self._poll_interval

        while time.monotonic() < deadline:
            if cancel_check is not None and cancel_check():
                raise SubprocessCancelledError(
                    f"CoW daemon job {job_id} cancelled during execution "
                    "(job cancellation requested)"
                )
            resp = requests.get(
                f"{self._daemon_url}/api/v1/jobs/{job_id}",
                headers=self._headers(),
                timeout=self._request_timeout,
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
        """DELETE /api/v1/clones/{namespace}/{name}. 404 counts as success (idempotent).

        Derives the daemon clone identity ``(namespace, name)`` from the
        mount-relative path. Handles BOTH the canonical Bug #1084 shape
        ``{mount}/.versioned/{ns}/{name}`` (the leading ``.versioned`` segment is
        skipped) and the legacy shape ``{mount}/{ns}/{name}`` (transition).
        """
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

        parts = list(relative.parts)
        # Bug #1084: canonical snapshots live under a leading ".versioned"
        # segment. Strip it so (namespace, name) match the daemon registry
        # identity, which is keyed on the dest parent-dir / leaf names.
        if parts and parts[0] == ".versioned":
            parts = parts[1:]
        if len(parts) < 2:
            raise ValueError(
                f"clone_path '{clone_path}' must have at least namespace/name under mount_point"
            )
        namespace = parts[0]
        name = parts[1]

        resp = requests.delete(
            f"{self._daemon_url}/api/v1/clones/{namespace}/{name}",
            headers=self._headers(),
            timeout=self._request_timeout,
        )
        if resp.status_code == 404 or resp.ok:
            return True

        # Bug #1844: classify instead of raise_for_status(). That call
        # produced an HTTPError carrying only the status line and the URL,
        # so the daemon's real cause never left the daemon host -- which is
        # why this bug's root cause could not be established from here at
        # all -- and a transient "still in use" was indistinguishable from a
        # permanently broken delete, so both got the same five-attempt
        # budget and the snapshot was abandoned either way.
        errno_name, detail = self._parse_daemon_error(resp)
        message = (
            f"CoW daemon refused DELETE of clone '{namespace}/{name}' "
            f"(HTTP {resp.status_code}, errno={errno_name}): {detail}"
        )
        if resp.status_code == 409:
            raise SnapshotInUseError(message, errno_name=errno_name, detail=detail)
        raise SnapshotDeleteError(message, errno_name=errno_name, detail=detail)

    @staticmethod
    def _parse_daemon_error(resp: "Response") -> Tuple[Optional[str], str]:
        """Extract ``(errno_name, detail)`` from a CoW daemon error response.

        The daemon's ``http_exception_handler`` returns ``exc.detail`` as the
        WHOLE body when it is a dict, so an error body is flat:
        ``{"error", "code", "errno", "detail"}``. A body that is not JSON (a
        reverse proxy's HTML error page, say) must still yield something
        actionable rather than be swallowed, so the raw text is used as the
        detail in that case.
        """
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return None, (resp.text or "").strip()[:500]
        errno_name = body.get("errno")
        detail = body.get("detail") or body.get("error") or str(body)
        return errno_name, str(detail)

    def list_clones(self, namespace: str) -> List[dict]:
        """GET /api/v1/clones?namespace={namespace}. Returns list of clone dicts.

        Sanitizes the namespace (dots->underscores) so dotted aliases round-trip
        symmetrically with create/delete (Bug #1084 Phase A2).
        """
        requests = self._requests()
        sanitized_namespace = self._sanitize_identifier(namespace)
        resp = requests.get(
            f"{self._daemon_url}/api/v1/clones",
            params={"namespace": sanitized_namespace},
            headers=self._headers(),
            timeout=self._request_timeout,
        )
        resp.raise_for_status()
        return list(resp.json())

    def clone_exists(self, namespace: str, name: str) -> bool:
        """GET /api/v1/clones/{namespace}/{name}. Returns True=200, False=404.

        Sanitizes identifiers (dots->underscores) for symmetry with create/delete
        (Bug #1084 Phase A2).
        """
        requests = self._requests()
        sanitized_namespace = self._sanitize_identifier(namespace)
        sanitized_name = self._sanitize_identifier(name)
        resp = requests.get(
            f"{self._daemon_url}/api/v1/clones/{sanitized_namespace}/{sanitized_name}",
            headers=self._headers(),
            timeout=self._request_timeout,
        )
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True


# ---------------------------------------------------------------------------
# DelegatedCloneBackend
# ---------------------------------------------------------------------------


class DelegatedCloneBackend:
    """Clone backend that delegates the activated-repo copy-on-write clone to
    workspace-mcp instead of running any filesystem operation inside cidx.

    EVO-64615 / EVO-64616: the server process must NOT perform filesystem
    clones directly — that responsibility belongs to workspace-mcp (which owns
    the CoW/reflink primitive and the storage layout). This backend forwards
    the ONE filesystem-mutating Protocol method — ``create_clone_at_path`` —
    to workspace-mcp over HTTP (``POST {endpoint}/clone-path``), and
    composes a :class:`LocalCloneBackend` for the read-only / snapshot-registry
    methods (``create_clone``, ``delete_clone``, ``list_clones``,
    ``clone_exists``), which operate on the shared, already-visible mount and
    perform no cidx-side working-tree copy.

    Fail-loud contract: any HTTP/transport failure raises — there is NO silent
    fallback to a local copy (that would reintroduce the very cidx-side
    filesystem work this backend exists to remove). Callers
    (ActivatedRepoManager) already rmtree the partial dest on any exception.

    Auth: ``Authorization: Bearer {api_key}`` on every request.
    """

    def __init__(
        self,
        config: RemoteServiceEndpoint,
        versioned_base: str = "",
        visibility_waiter: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._endpoint = (config.endpoint or "").rstrip("/")
        self._api_key = config.api_key
        # The endpoint Protocol carries only endpoint/api_key; the rest is
        # WorkspaceMcpConfig's, read with its defaults when absent.
        self._mount_point = (getattr(config, "mount_point", "") or "").rstrip("/")
        self._timeout: Optional[int] = getattr(config, "timeout_seconds", None)
        self._poll_interval = getattr(config, "poll_interval_seconds", None)
        # Snapshot-registry / delete operations run against the shared mount and
        # do NOT copy a working tree — compose LocalCloneBackend rather than
        # duplicate its logic (and keep create_clone_at_path the only delegated,
        # working-tree-copying method).
        self._local = LocalCloneBackend(versioned_base=versioned_base)
        # Read-after-create barrier: workspace-mcp creates the clone on the
        # shared volume; this pod reaches it and may hold a negative dcache
        # entry for the brand-new path. Wait for visibility before returning so
        # no downstream consumer ENOENTs on it. Injectable for tests.
        self._visibility_waiter: Callable[[str], None] = (
            visibility_waiter
            if visibility_waiter is not None
            else (
                lambda path: wait_for_nfs_visibility(
                    path, timeout=_configured_visibility_timeout()
                )
            )
        )

    def _requests(self):  # type: ignore[return]  # lazy import avoids hard dep
        """Return the ``requests`` module (lazy-imported to keep startup fast)."""
        import requests  # noqa: PLC0415

        return requests

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}"}

    def create_clone(
        self, source_path: str, namespace: str, name: str, timeout: Optional[int] = None
    ) -> str:
        """Create a canonical snapshot at the shared ``.versioned`` layout.

        Delegates to the composed LocalCloneBackend: snapshots live on the
        shared mount and this is a registry-shaped copy, not a per-session
        activated-repo working-tree clone. Kept local so snapshot identity and
        layout stay identical to every other backend.
        """
        return self._local.create_clone(source_path, namespace, name, timeout=timeout)

    def create_clone_at_path(
        self,
        source_path: str,
        dest_path: str,
        preserve_attrs: bool = True,
        timeout: Optional[int] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        poll_interval: float = SHORT_POLL_SECONDS,
    ) -> str:
        """Delegate the activated-repo CoW clone to workspace-mcp. Returns dest_path.

        POSTs ``{source_path, dest_path, preserve_attrs}`` to
        ``{endpoint}/clone-path``. The endpoint performs the reflink copy
        (near-instant) and responds synchronously with ``{"dest_path": ...,
        "method": ...}``. cancel_check is honored before and after the request
        (the copy itself is not mid-flight cancellable, but the caller rmtrees
        dest on the raised SubprocessCancelledError). Any non-2xx or transport
        error raises — NO silent local fallback (EVO-64615). poll_interval is
        accepted for CloneBackend Protocol symmetry and unused here.
        """
        self._request_clone(
            source_path,
            dest_path,
            preserve_attrs=preserve_attrs,
            timeout=timeout,
            cancel_check=cancel_check,
        )
        # dest is on the shared volume; wait until visible on this pod before
        # returning so downstream subprocess.run(cwd=dest) does not ENOENT.
        self._visibility_waiter(dest_path)
        return dest_path

    # -- working copies on a pod WITHOUT the repository volume ---------------
    #
    # Under ``content_backend='delegated'`` an activated repository's working
    # copy exists only on workspace-mcp's storage, at the same absolute path.
    # These three calls are the whole lifecycle cidx needs: create (never over
    # an existing copy), ask whether one exists, remove one. None of them looks
    # at this pod's disk -- there is nothing to wait to become visible.

    def clone_working_copy(
        self,
        source_path: str,
        dest_path: str,
        timeout: Optional[int] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> str:
        """Create the activated working copy at *dest_path*; returns it.

        ``if_absent``: an existing destination is refused by workspace-mcp
        (409) and raised as :class:`CloneDestinationExistsError`, so a second
        activation can never replace a user's copy. No visibility wait: this
        pod does not hold the volume.
        """
        self._request_clone(
            source_path,
            dest_path,
            preserve_attrs=True,
            timeout=timeout,
            cancel_check=cancel_check,
            if_absent=True,
        )
        return dest_path

    def stat_working_copy(self, path: str) -> WorkingCopyStat:
        """Whether *path* exists on workspace-mcp's storage, and is a directory."""
        body = self._post_json("/clone-path/stat", {"path": path}, self._timeout)
        exists = body.get("exists")
        is_dir = body.get("is_dir")
        if not isinstance(exists, bool) or not isinstance(is_dir, bool):
            raise RuntimeError(
                "DelegatedCloneBackend: workspace-mcp clone-path/stat returned "
                f"no exists/is_dir booleans (keys {sorted(body)})"
            )
        return WorkingCopyStat(exists=exists, is_dir=is_dir)

    def delete_working_copy(self, path: str, timeout: Optional[int] = None) -> bool:
        """Remove the working copy at *path*; False when it was already absent."""
        effective_timeout = timeout if timeout is not None else self._timeout
        body = self._post_json("/clone-path/delete", {"path": path}, effective_timeout)
        deleted = body.get("deleted")
        if not isinstance(deleted, bool):
            raise RuntimeError(
                "DelegatedCloneBackend: workspace-mcp clone-path/delete returned "
                f"no deleted boolean (keys {sorted(body)})"
            )
        return deleted

    def _request_clone(
        self,
        source_path: str,
        dest_path: str,
        *,
        preserve_attrs: bool,
        timeout: Optional[int],
        cancel_check: Optional[Callable[[], bool]],
        if_absent: bool = False,
    ) -> None:
        """POST the clone; cancel_check is honored before and after it."""
        if cancel_check is not None and cancel_check():
            raise SubprocessCancelledError(
                "activated-repo clone cancelled before workspace-mcp request"
            )
        body: dict = {
            "source_path": source_path,
            "dest_path": dest_path,
            "preserve_attrs": preserve_attrs,
        }
        if if_absent:
            body["if_absent"] = True
        effective_timeout = timeout if timeout is not None else self._timeout
        logger.info(
            "DelegatedCloneBackend: requesting workspace-mcp clone at '%s' from '%s'",
            dest_path,
            source_path,
        )
        response = self._post("/clone-path", body, effective_timeout, "clone")
        if if_absent and response.status_code == 409:
            raise CloneDestinationExistsError(
                f"DelegatedCloneBackend: destination '{dest_path}' already exists "
                "on workspace-mcp -- refusing to clone over it"
            )
        self._raise_for_status(response, "clone")
        # The remote clone completed; honor a cancellation that arrived while we
        # waited. The caller removes dest_path on this exception.
        if cancel_check is not None and cancel_check():
            raise SubprocessCancelledError(
                "activated-repo clone cancelled during workspace-mcp request"
            )
        method = ""
        try:
            method = str((response.json() or {}).get("method", ""))
        except ValueError:  # pragma: no cover - defensive; non-JSON 2xx body
            pass
        logger.info(
            "DelegatedCloneBackend: workspace-mcp clone complete (method=%s) at '%s'",
            method,
            dest_path,
        )

    def _post(
        self, route: str, body: dict, timeout: Optional[int], what: str
    ) -> "Response":
        """POST *body* to *route*; a transport failure raises (no fallback)."""
        requests = self._requests()
        try:
            response: "Response" = requests.post(
                f"{self._endpoint}{route}",
                json=body,
                headers=self._headers(),
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise RuntimeError(
                f"DelegatedCloneBackend: workspace-mcp {what} request to "
                f"'{self._endpoint}{route}' failed: {exc}"
            ) from exc
        return response

    @staticmethod
    def _raise_for_status(response: "Response", what: str) -> None:
        if response.status_code >= 300:
            raise RuntimeError(
                f"DelegatedCloneBackend: workspace-mcp {what} failed "
                f"(HTTP {response.status_code}): {response.text[:300]}"
            )

    def _post_json(self, route: str, body: dict, timeout: Optional[int]) -> dict:
        """POST and return the JSON object answer; anything else raises."""
        response = self._post(route, body, timeout, route)
        self._raise_for_status(response, route)
        try:
            answer = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"DelegatedCloneBackend: workspace-mcp {route} returned non-JSON"
            ) from exc
        if not isinstance(answer, dict):
            raise RuntimeError(
                f"DelegatedCloneBackend: workspace-mcp {route} returned "
                f"{type(answer).__name__}, not an object"
            )
        return answer

    def delete_clone(self, clone_path: str) -> bool:
        """Remove the clone directory tree on the shared mount (no cidx copy)."""
        return self._local.delete_clone(clone_path)

    def list_clones(self, namespace: str) -> List[dict]:
        """List snapshots under ``.versioned/{namespace}`` on the shared mount."""
        return self._local.list_clones(namespace)

    def clone_exists(self, namespace: str, name: str) -> bool:
        """Return True when the snapshot directory is present on the shared mount."""
        return self._local.clone_exists(namespace, name)


# ---------------------------------------------------------------------------
# CloneBackendFactory
# ---------------------------------------------------------------------------


class CloneBackendFactory:
    """Creates the appropriate CloneBackend from configuration values."""

    _VALID_BACKENDS = ("local", "ontap", "cow-daemon", "delegated")

    @staticmethod
    def create(
        clone_backend_type: str,
        versioned_base: str = "",
        ontap_config: Optional["OntapConfig"] = None,
        cow_daemon_config: Optional["CowDaemonConfig"] = None,
        endpoint_config: Optional[RemoteServiceEndpoint] = None,
    ) -> CloneBackend:
        """Instantiate and return the requested CloneBackend.

        Parameters
        ----------
        clone_backend_type:
            One of ``"local"``, ``"ontap"``, ``"cow-daemon"``, or
            ``"delegated"``.
        versioned_base:
            Base directory for LocalCloneBackend (also the composed local
            backend inside DelegatedCloneBackend).
        ontap_config:
            OntapConfig instance, required when ``clone_backend_type="ontap"``.
        cow_daemon_config:
            CowDaemonConfig instance, required when ``clone_backend_type="cow-daemon"``.
        endpoint_config:
            Anything exposing ``endpoint`` and ``api_key`` for the service that
            performs the clone (see ``utils.remote_endpoint``); required for
            ``"delegated"``.

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

        if clone_backend_type == "delegated":
            if endpoint_config is None:
                raise ValueError(
                    "endpoint_config is required when clone_backend_type='delegated'"
                )
            return DelegatedCloneBackend(
                config=endpoint_config, versioned_base=versioned_base
            )

        raise ValueError(
            f"Unsupported clone_backend type: '{clone_backend_type}'. "
            f"Valid options: {', '.join(CloneBackendFactory._VALID_BACKENDS)}"
        )
