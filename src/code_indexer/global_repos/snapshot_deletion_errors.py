"""Shared vocabulary for versioned-snapshot deletion failures (Bug #1844).

Why this module exists at all
-----------------------------
Deleting a versioned snapshot crosses three layers -- ``CleanupManager``
(the queue), ``VersionedSnapshotManager`` (the dispatch), and a
``CloneBackend`` (the local rmtree or the CoW daemon's REST call) -- and
before this existed each layer flattened every distinct failure into one
indistinguishable shape:

* ``CowDaemonBackend`` raised a bare ``requests.HTTPError`` whose text is
  only the status line and the URL, so the daemon's real cause never left
  the daemon host;
* ``LocalCloneBackend`` returned ``False`` on any OSError and the caller
  discarded the result, so a failed deletion was recorded as a success
  and the durable pending-deletion row was dropped (a permanent, silent
  leak -- Messi Rule #13);
* ``CleanupManager`` therefore applied ONE policy (five attempts in
  roughly sixteen seconds, then abandon the path) to a transient
  "something still holds this open" and to a permanently broken delete
  alike. Neither ever converged, and the disk was never reclaimed.

Two types are enough to fix that, so there are exactly two.

Placement
---------
This is a dependency-free leaf module and it lives under ``global_repos``
rather than ``server/storage/shared`` deliberately: ``cleanup_manager``
is CLI-reachable, and importing ``code_indexer.server.storage.shared.*``
would drag ``database_manager`` and ``snapshot_manager`` (hence the
server telemetry stack) into its import graph -- the exact regression
class Bug #1468 exists to prevent.
"""

from __future__ import annotations

from typing import Optional


class SnapshotDeleteError(Exception):
    """A versioned snapshot could not be deleted.

    ``errno_name`` and ``detail`` carry the backend's OWN reported cause
    when it supplied one (the CoW daemon reports both over HTTP; the
    local backend reports the kernel errno directly), so the reason
    appears in this side's log instead of only on the storage host.
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

    This is "not yet", not "broken": the identical delete succeeds once
    the holder lets go. Callers must therefore retry it later WITHOUT
    charging the attempt to a failure budget -- charging it is what made
    a still-referenced snapshot get abandoned after five attempts and
    leak its disk forever.

    Being a subclass of :class:`SnapshotDeleteError` means a caller that
    only cares that "the delete did not happen" needs no change; a caller
    that distinguishes the two MUST catch this one first.
    """
