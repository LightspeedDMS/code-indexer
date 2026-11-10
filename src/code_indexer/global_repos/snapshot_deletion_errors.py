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
The types are part of the clone seam's delete contract and live in the SDK
(``cidx_sdk.seams.clone``), so a clone backend raises them without importing
the core. This module re-exports them, so existing imports and ``except``
clauses see the same classes. It stays a dependency-free leaf under
``global_repos``: ``cleanup_manager`` is CLI-reachable, and importing
``code_indexer.server.storage.shared.*`` would drag the server telemetry
stack into its import graph (Bug #1468).
"""

from __future__ import annotations

from cidx_sdk.seams.clone import (
    SnapshotDeleteError as SnapshotDeleteError,
    SnapshotInUseError as SnapshotInUseError,
    remove_tree_already_gone_ok as remove_tree_already_gone_ok,
)
