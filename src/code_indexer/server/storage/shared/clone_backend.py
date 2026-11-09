"""
Clone backend seam: the core's side.

The clone seam (``cidx_sdk.seams.clone``) has four modes, each a provider on
``code_indexer.clone_backend`` registered under the mode's name and reached
through the seam resolver (``server/services/clone_backend_plugin.py``):

- ``local``, ``ontap``, ``cow-daemon`` -- in ``cidx-backend-workspace``
  (``cidx_backend_workspace.clone``);
- ``delegated`` -- :class:`DelegatedCloneBackend` below, the workspace-mcp
  clone-path client. It is the workspace-mcp delegate's and moves to
  ``cidx-delegate-workspace`` when that distribution exists; until then the
  core distribution registers it.

This module also holds the core runtime every clone backend runs on
(:func:`core_clone_services`: the cancellable process runner, credential
redaction and the NFS read-after-create barrier), and re-exports the moved
names so existing imports, isinstance checks and test patches keep working.

Story #510 -- CloneBackend Abstraction and CoW Daemon Integration.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Callable, List, Optional, cast

from code_indexer.utils.remote_endpoint import RemoteServiceEndpoint

# The seam Protocol and its DTOs live in the SDK (``cidx_sdk.seams.clone``);
# re-exported so existing imports and isinstance checks see the same classes.
from cidx_sdk.seams.clone import (
    RETRYABLE_DELETE_ERRNOS,
    SHORT_POLL_SECONDS,
    CloneBackend as CloneBackend,
    CancellableRunner,
    CloneBackendSettings,
    CloneDestinationExistsError as CloneDestinationExistsError,
    CloneServices,
    WorkingCopyStat as WorkingCopyStat,
)

# The local, ONTAP and CoW-daemon implementations live in their own
# distribution (packages/backends/cidx-backend-workspace) and are selected by
# the seam resolver under their mode's name; re-exported for existing imports.
from cidx_backend_workspace.clone import (
    CowDaemonBackend as CowDaemonBackend,
    LocalCloneBackend as LocalCloneBackend,
    OntapCloneBackend as OntapCloneBackend,
    set_default_clone_services,
)
from code_indexer.server.storage.shared.nfs_visibility import (
    _configured_visibility_timeout as _configured_visibility_timeout,
    wait_for_nfs_visibility,
)
from code_indexer.server.utils.cancellable_subprocess import (
    SubprocessCancelledError,
    run_cancellable_subprocess,
)
from code_indexer.utils.subprocess_diagnostics import redact_url_credentials

if TYPE_CHECKING:
    import subprocess

    from requests import Response  # pragma: no cover

    from code_indexer.server.utils.config_manager import (
        CowDaemonConfig,
        OntapConfig,
    )  # pragma: no cover


logger = logging.getLogger(__name__)

#: Bug #1844 retryable-delete errnos; the seam's, from the SDK.
_RETRYABLE_DELETE_ERRNOS = RETRYABLE_DELETE_ERRNOS

__all__ = [
    "CloneBackend",
    "CloneBackendFactory",
    "CloneDestinationExistsError",
    "CowDaemonBackend",
    "DelegatedCloneBackend",
    "LocalCloneBackend",
    "OntapCloneBackend",
    "WorkingCopyStat",
    "core_clone_services",
    "make_delegated_clone_backend",
    "redact_url_credentials",
    "run_cancellable_subprocess",
    "wait_for_nfs_visibility",
]


# ---------------------------------------------------------------------------
# The core runtime handed to every clone backend
# ---------------------------------------------------------------------------


#: This module's import name. The runtime below reads its helpers from the
#: module registered under it, at call time: a patch of
#: ``clone_backend.wait_for_nfs_visibility`` (or of the runner, the timeout)
#: is honoured, and a second copy of this file imported under another name
#: (the test suite's ``src.`` prefix) cannot leave the backends bound to the
#: copy nobody patches.
_CANONICAL_NAME = "code_indexer.server.storage.shared.clone_backend"


def _this_module() -> ModuleType:
    return import_module(_CANONICAL_NAME)


def _visibility_waiter(path: str) -> None:
    module = _this_module()
    module.wait_for_nfs_visibility(
        path, timeout=module._configured_visibility_timeout()
    )


def _run_cancellable(
    args: List[str],
    *,
    cwd: Optional[str] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    poll_interval: float = SHORT_POLL_SECONDS,
    timeout: Optional[float] = None,
) -> "subprocess.CompletedProcess[str]":
    runner: CancellableRunner = _this_module().run_cancellable_subprocess
    return runner(
        args,
        cwd=cwd,
        cancel_check=cancel_check,
        poll_interval=poll_interval,
        timeout=timeout,
    )


def _redact(text: object) -> str:
    redacted: str = _this_module().redact_url_credentials(text)
    return redacted


_CORE_CLONE_SERVICES = CloneServices(
    run_cancellable=_run_cancellable,
    redact=_redact,
    visibility_waiter=_visibility_waiter,
)


def core_clone_services() -> CloneServices:
    """The core runtime a clone backend runs on (Bug #1342 cancellable
    copies, credential redaction, the Bug #1084 NFS visibility barrier)."""
    return _CORE_CLONE_SERVICES


# A clone backend class constructed directly through this module's names
# (tests, older call sites) runs on the core runtime too.
set_default_clone_services(_CORE_CLONE_SERVICES)


# ---------------------------------------------------------------------------
# DelegatedCloneBackend
# ---------------------------------------------------------------------------


#: How workspace-mcp words "that path does not exist" in an error body.
_MISSING_PATH_MARKERS = ("ENOENT", "no such file", "does not exist")


def reflink_copy_into_new_directory(
    source_path: str, dest: Path, timeout: Optional[int], label: str
) -> None:
    """Copy the tree at ``source_path`` to ``dest``, which must not exist.

    ``dest`` is reserved first with one ``mkdir`` (atomic across processes
    and nodes, NFSv3 included), so two creators that picked the same name
    can never copy into one tree: the second raises
    CloneDestinationExistsError and touches nothing. On any copy failure the
    reserved ``dest`` is removed and the error re-raised."""
    try:
        os.mkdir(dest)
    except FileExistsError as exc:
        raise CloneDestinationExistsError(
            f"{label}: destination '{dest}' already exists; refusing to copy into it"
        ) from exc
    # ^ external `cp -a src/. dest` copies the contents and gives dest the source directory's attributes
    try:
        subprocess.run(
            ["cp", "--reflink=auto", "-a", f"{source_path}/.", str(dest)],
            check=True,
            capture_output=True,
            timeout=timeout,
        )
    except BaseException:
        try:
            shutil.rmtree(dest)
            logger.info(
                "%s: removed partial clone '%s' after a failed copy", label, dest
            )
        except OSError as cleanup_error:
            logger.error(
                "%s: could not remove partial clone '%s': %s: %s",
                label,
                dest,
                type(cleanup_error).__name__,
                cleanup_error,
            )
        raise


#: The body of the framework's 404 for a route this workspace-mcp lacks.
_MOVE_ROUTE_ABSENT = "Cannot POST /clone-path/move"


def _names_a_missing_path(body: str) -> bool:
    """Whether a workspace-mcp error body says the PATH is missing."""
    lowered = body.lower()
    return any(marker.lower() in lowered for marker in _MISSING_PATH_MARKERS)


class DelegatedCloneBackend:
    """Clone backend that delegates the activated-repo copy-on-write clone to
    workspace-mcp instead of running any filesystem operation inside cidx.

    issue-64615 / issue-64616: the server process must NOT perform filesystem
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
        # do NOT copy a working tree — compose the local mode's backend (through
        # the seam resolver) rather than duplicate its logic, and keep
        # create_clone_at_path the only delegated, working-tree-copying method.
        from code_indexer.server.services.clone_backend_plugin import (
            local_clone_backend,
        )

        local = local_clone_backend(versioned_base)
        if not isinstance(local, LocalCloneBackend):
            raise TypeError(
                "DelegatedCloneBackend composes the local clone mode's "
                f"LocalCloneBackend; the resolver returned {type(local).__name__}"
            )
        self._local: LocalCloneBackend = local
        # Read-after-create barrier: workspace-mcp creates the clone on the
        # shared volume; this pod reaches it and may hold a negative dcache
        # entry for the brand-new path. Wait for visibility before returning so
        # no downstream consumer ENOENTs on it. Injectable for tests.
        self._visibility_waiter: Callable[[str], None] = (
            visibility_waiter if visibility_waiter is not None else _visibility_waiter
        )

    def _requests(self) -> ModuleType:
        """Return the ``requests`` module (lazy-imported to keep startup fast)."""
        import requests

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
        error raises — NO silent local fallback (issue-64615). poll_interval is
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
        """Remove the working copy at *path*; False when it was already absent.

        Already absent includes workspace-mcp answering 404 for the PATH
        (ENOENT, issue-64616): a second deleter of the same path (another pod)
        finds it gone, which is the outcome it asked for, never a failure. A
        404 that does not name a missing path (a route this workspace-mcp
        lacks) still raises: it says nothing about the copy."""
        effective_timeout = timeout if timeout is not None else self._timeout
        response = self._post(
            "/clone-path/delete", {"path": path}, effective_timeout, "clone-path/delete"
        )
        if response.status_code == 404 and _names_a_missing_path(response.text):
            return False
        body = self._json_object(response, "/clone-path/delete")
        deleted = body.get("deleted")
        if not isinstance(deleted, bool):
            raise RuntimeError(
                "DelegatedCloneBackend: workspace-mcp clone-path/delete returned "
                f"no deleted boolean (keys {sorted(body)})"
            )
        return deleted

    def move_working_copy(
        self, source_path: str, dest_path: str, timeout: Optional[int] = None
    ) -> bool:
        """Rename *source_path* to *dest_path* on workspace-mcp's storage
        (``POST /clone-path/move {from, to}`` -> ``{from, to, moved: true}``:
        atomic, within one root and one filesystem, never a copy). False when
        this workspace-mcp has no move route -- the framework's own 404
        (``Cannot POST /clone-path/move``), never workspace-mcp's 404 for a
        missing ``from``, which raises: the caller copies instead. A 409 (the
        target exists, or the paths are on different roots or filesystems)
        raises :class:`CloneDestinationExistsError`; every other failure
        (400, 401, 403, 503) raises."""
        effective_timeout = timeout if timeout is not None else self._timeout
        response = self._post(
            "/clone-path/move",
            {"from": source_path, "to": dest_path},
            effective_timeout,
            "clone-path/move",
        )
        if response.status_code == 404 and _MOVE_ROUTE_ABSENT in response.text:
            return False
        if response.status_code == 409:
            raise CloneDestinationExistsError(
                f"DelegatedCloneBackend: workspace-mcp refused to move '{source_path}' "
                f"to '{dest_path}' (the target exists, or the two are on different "
                "roots or filesystems)"
            )
        self._raise_for_status(response, "clone-path/move")
        body = self._json_object(response, "/clone-path/move")
        if body.get("moved") is not True:
            raise RuntimeError(
                "DelegatedCloneBackend: workspace-mcp clone-path/move did not "
                f"answer moved: true (keys {sorted(body)})"
            )
        return True

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
        return self._json_object(self._post(route, body, timeout, route), route)

    def _json_object(self, response: "Response", route: str) -> dict:
        """The JSON object of a successful answer; anything else raises."""
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
# Entry point for the delegated mode, and the name-based factory
# ---------------------------------------------------------------------------


def make_delegated_clone_backend(settings: CloneBackendSettings) -> CloneBackend:
    """``clone_backend=delegated``: workspace-mcp's clone-path (issue-64616)."""
    if settings.endpoint is None:
        raise ValueError(
            "endpoint_config is required when clone_backend_type='delegated'"
        )
    return DelegatedCloneBackend(
        config=settings.endpoint, versioned_base=settings.versioned_base
    )


class CloneBackendFactory:
    """Creates the CloneBackend for a mode, through the seam resolver."""

    _VALID_BACKENDS = ("local", "ontap", "cow-daemon", "delegated")

    @staticmethod
    def create(
        clone_backend_type: str,
        versioned_base: str = "",
        ontap_config: Optional["OntapConfig"] = None,
        cow_daemon_config: Optional["CowDaemonConfig"] = None,
        endpoint_config: Optional[RemoteServiceEndpoint] = None,
    ) -> CloneBackend:
        """Instantiate the provider installed for *clone_backend_type*.

        Parameters
        ----------
        clone_backend_type:
            One of ``"local"``, ``"ontap"``, ``"cow-daemon"``, or
            ``"delegated"``.
        versioned_base:
            Base directory for the local backend (also the composed local
            backend inside DelegatedCloneBackend).
        ontap_config:
            The ``ontap`` section, required when ``clone_backend_type="ontap"``.
        cow_daemon_config:
            The ``cow_daemon`` section, required for ``"cow-daemon"``.
        endpoint_config:
            Anything exposing ``endpoint`` and ``api_key`` for the service that
            performs the clone (see ``utils.remote_endpoint``); required for
            ``"delegated"``.

        Raises
        ------
        ValueError
            When ``clone_backend_type`` is not one of the valid options, or
            when a required config object is missing for the chosen backend.
        SeamUnavailableError
            When no module (or more than one) provides the mode.
        """
        if clone_backend_type not in CloneBackendFactory._VALID_BACKENDS:
            raise ValueError(
                f"Unsupported clone_backend type: '{clone_backend_type}'. "
                f"Valid options: {', '.join(CloneBackendFactory._VALID_BACKENDS)}"
            )
        from code_indexer.server.services.clone_backend_plugin import (
            resolve_clone_backend,
        )

        settings = CloneBackendSettings(
            services=_CORE_CLONE_SERVICES,
            versioned_base=versioned_base,
            ontap=ontap_config,
            cow_daemon=cow_daemon_config,
            endpoint=endpoint_config,
        )
        return cast(CloneBackend, resolve_clone_backend(clone_backend_type, settings))
