"""The clone seam: create and delete copy-on-write clones of repositories.

Selected by ``clone_backend``: ``local`` (``cp --reflink``), ``ontap`` (ONTAP
FlexClone), ``cow-daemon`` (the CoW daemon over NFS) or ``delegated``
(workspace-mcp). Entry-point group ``code_indexer.clone_backend``, with the
mode chosen by entry name. Every provider is a factory taking ONE argument,
:class:`CloneBackendSettings`: what the configured mode needs from the core
(its config section, the versioned base) and the core runtime services it
runs on (:class:`CloneServices`), so a clone backend imports this SDK only.

Moved from ``code_indexer.server.storage.shared.clone_backend`` (and the
deletion vocabulary from ``code_indexer.global_repos.snapshot_deletion_errors``,
the cancellation error from ``server/utils/cancellable_subprocess.py``), which
re-export these names.
"""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional, Protocol, Union, runtime_checkable

#: How often a clone that runs a killable job polls its cancel check (seconds).
SHORT_POLL_SECONDS = 2.0

#: The configured values ``clone_backend`` may take.
VALID_CLONE_BACKENDS = ("local", "ontap", "cow-daemon", "delegated")


class CloneDestinationExistsError(FileExistsError):
    """``create_clone_at_path`` refused an already-existing destination.

    A dedicated subtype (not a bare FileExistsError) so callers can catch it
    SPECIFICALLY and never delete the destination: it is a pre-existing
    directory this clone attempt never touched, not a partial clone of its own
    making.
    """


@dataclass(frozen=True)
class WorkingCopyStat:
    """What the clone service reports for an activated working-copy path."""

    exists: bool
    is_dir: bool


@runtime_checkable
class CloneBackend(Protocol):
    """Structural interface for clone lifecycle operations."""

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

        ``cancel_check`` is an optional zero-arg callable returning True when
        the owning job has been cancelled. An implementation that spawns a
        killable process or polls a remote job MUST consult it and raise
        promptly when it fires. None disables cancellation.
        """
        ...  # pragma: no cover

    def delete_clone(self, clone_path: str) -> bool:
        """Delete a clone by its absolute path. True on success or if already absent."""
        ...  # pragma: no cover

    def list_clones(self, namespace: str) -> List[dict]:
        """Return a list of clone dicts for the given namespace."""
        ...  # pragma: no cover

    def clone_exists(self, namespace: str, name: str) -> bool:
        """Return True if the clone identified by namespace/name exists."""
        ...  # pragma: no cover


# ── cancellation and deletion: what a clone call raises ─────────────────────


class SubprocessCancelledError(RuntimeError):
    """The owning job's ``cancel_check`` fired: the clone (or the process doing
    it) was stopped. Callers remove the partial destination on it."""


#: Bug #1844: errnos meaning "the tree could not be removed because something
#: still holds it", as opposed to "this delete is broken". ENOTEMPTY is the
#: shape an NFS silly-rename leaves behind; EBUSY and ETXTBSY are the local
#: equivalents. Identical to the CoW daemon's own retryable set so both ends of
#: its REST call agree on what "still in use" means.
RETRYABLE_DELETE_ERRNOS = frozenset({errno.ENOTEMPTY, errno.EBUSY, errno.ETXTBSY})

#: rmtree passes before a tree that keeps vanishing under it is a failure.
_CONCURRENT_DELETE_ATTEMPTS = 3


class SnapshotDeleteError(Exception):
    """A clone (versioned snapshot) could not be deleted.

    ``errno_name`` and ``detail`` carry the backend's OWN reported cause when
    it supplied one (the CoW daemon reports both over HTTP; the local backend
    the kernel errno), so the reason appears in this side's log instead of
    only on the storage host. ``delete_clone`` raises it; it never reports a
    failed deletion by returning False.
    """

    def __init__(
        self,
        message: str,
        *,
        errno_name: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        self.errno_name = errno_name
        self.detail = detail
        super().__init__(message)


class SnapshotInUseError(SnapshotDeleteError):
    """The delete was refused because something still holds the snapshot.

    "Not yet", not "broken": the identical delete succeeds once the holder
    lets go, so callers retry it later WITHOUT charging a failure budget. A
    caller that distinguishes the two must catch this one first.
    """


def remove_tree_already_gone_ok(path: Union[str, Path]) -> None:
    """``shutil.rmtree(path)``, where what another deleter removed first is
    not an error.

    Two pods can delete the same snapshot; the second rmtree then finds the
    tree -- or part of it, mid-walk -- gone. A tree that no longer exists is
    deleted; one still there after an ENOENT is finished on the next pass.
    Every other OSError raises unchanged (the caller classifies it), as does
    an ENOENT that persists while the tree still exists.
    """
    for attempt in range(_CONCURRENT_DELETE_ATTEMPTS):
        try:
            shutil.rmtree(str(path))
            return
        except FileNotFoundError:
            if not os.path.lexists(path):
                return
            if attempt == _CONCURRENT_DELETE_ATTEMPTS - 1:
                raise


# ── what a clone backend receives from the core ─────────────────────────────


class CancellableRunner(Protocol):
    """Runs a process cooperatively cancellable by ``cancel_check``.

    The core's implementation runs the child in its own process group and
    kills the whole group when ``cancel_check`` fires, raising
    :class:`SubprocessCancelledError`; ``timeout`` (seconds, None: none) is
    an overall deadline. Output is TEXT and returned, never raised.
    """

    def __call__(
        self,
        args: List[str],
        *,
        cwd: Optional[str] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        poll_interval: float = SHORT_POLL_SECONDS,
        timeout: Optional[float] = None,
    ) -> "subprocess.CompletedProcess[str]": ...


@dataclass(frozen=True)
class CloneServices:
    """The core runtime a clone backend runs on.

    Attributes:
        run_cancellable: how a local copy process is run (Bug #1342).
        redact: masks credentials in text a backend puts in an exception.
        visibility_waiter: ``waiter(path)`` returns once *path*, created by
            another host (the CoW daemon, ONTAP, workspace-mcp), is visible
            on this node (Bug #1084 read-after-create barrier); raises when
            it never becomes visible.
    """

    run_cancellable: CancellableRunner
    redact: Callable[[object], str]
    visibility_waiter: Callable[[str], None]


class OntapSettings(Protocol):
    """The ``ontap`` config section the ``ontap`` mode reads."""

    @property
    def endpoint(self) -> str: ...

    @property
    def admin_user(self) -> str: ...

    @property
    def admin_password(self) -> str: ...

    @property
    def svm_name(self) -> str: ...

    @property
    def parent_volume(self) -> str: ...

    @property
    def mount_point(self) -> str: ...


class CowDaemonSettings(Protocol):
    """The ``cow_daemon`` config section the ``cow-daemon`` mode reads."""

    @property
    def daemon_url(self) -> str: ...

    @property
    def api_key(self) -> str: ...

    @property
    def mount_point(self) -> str: ...

    @property
    def poll_interval_seconds(self) -> float: ...

    @property
    def timeout_seconds(self) -> float: ...

    @property
    def request_timeout_seconds(self) -> float: ...

    @property
    def daemon_storage_path(self) -> Optional[str]: ...


@dataclass(frozen=True)
class CloneBackendSettings:
    """The one argument every clone provider factory takes.

    Attributes:
        services: the core runtime (:class:`CloneServices`).
        versioned_base: where ``local`` keeps ``.versioned/<ns>/<name>``.
        ontap: the ``ontap`` section (required by ``ontap``).
        cow_daemon: the ``cow_daemon`` section (required by ``cow-daemon``).
        endpoint: the remote clone service's endpoint section, anything with
            ``endpoint`` and ``api_key`` (required by ``delegated``).

    A factory raises ``ValueError`` when the section its mode needs is None.
    """

    services: CloneServices
    versioned_base: str = ""
    ontap: Optional[OntapSettings] = None
    cow_daemon: Optional[CowDaemonSettings] = None
    endpoint: Optional[Any] = None


#: A clone provider: ``factory(settings) -> CloneBackend``.
CloneBackendFactoryCallable = Callable[[CloneBackendSettings], CloneBackend]
