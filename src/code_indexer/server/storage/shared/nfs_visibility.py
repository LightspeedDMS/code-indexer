"""NFS read-after-create visibility barrier (Bug #1084 regression fix).

Why this exists
---------------
Bug #1084 introduced the canonical versioned-snapshot layout
``{mount}/.versioned/{ns}/v_<ts>``. On a cow-daemon (and ONTAP/FlexClone)
deployment the snapshot is created on a host's LOCAL filesystem and reached by
the scheduler node over NFS. The new canonical path nests under a freshly
created ``.versioned/`` + ``.versioned/{ns}/`` parent chain that the
scheduler's NFS client has never looked up, so its dcache holds a NEGATIVE
entry. A deep ``chdir`` / ``subprocess.run(cwd=...)`` lookup then ENOENTs even
though the directory already exists on the create side — a classic
read-after-create NFS dcache race that surfaced as ``FileNotFoundError`` from
``refresh_scheduler._create_snapshot``.

NFS close-to-open consistency means an explicit lookup (``stat``) on a parent
directory refreshes its dcache so newly created children become visible. A bare
``os.path.exists(dest)`` alone can keep returning a cached NEGATIVE result, so
we MUST stat the parent chain — mount root, ``.versioned``, ``.versioned/{ns}``
— before (and on every retry alongside) the leaf check.

The local backend does NOT need this barrier: ``cp`` writes the directory on the
same node that reads it, so it is immediately visible (no NFS round trip, no
remote dcache to bust).

Anti-fallback (#2): if the path never becomes visible within the bounded
deadline we RAISE — we never silently return an invisible path.
Anti-unbounded-loop (#14): the poll loop is bounded by a monotonic deadline.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# Bounded read-after-create visibility deadline (seconds). Named constant — no
# magic literal buried at the call site. Real NFS propagation after a successful
# create is typically sub-second; 15s is a generous ceiling before failing loud.
NFS_VISIBILITY_TIMEOUT_SECONDS: float = 15.0

# Backoff bounds for the poll interval (seconds). Short initial interval keeps
# the common fast-propagation case snappy; capped so the loop stays responsive.
_INITIAL_POLL_INTERVAL_SECONDS: float = 0.1
_MAX_POLL_INTERVAL_SECONDS: float = 0.5


def _parent_chain(path: str) -> list[str]:
    """Return the ancestor directories of *path* (root-first) plus *path* itself.

    Stat-ing each entry root-first refreshes the NFS dcache lookup along the
    whole chain so a not-yet-visible leaf created under a brand-new parent
    becomes resolvable.
    """
    p = Path(path)
    chain = [str(ancestor) for ancestor in reversed(p.parents)]
    chain.append(str(p))
    return chain


def wait_for_nfs_visibility(
    path: str,
    *,
    timeout: float = NFS_VISIBILITY_TIMEOUT_SECONDS,
    isdir_fn: Callable[[str], bool] = os.path.isdir,
    stat_fn: Callable[[str], object] = os.stat,
    monotonic_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Block until *path* is a visible directory, busting the NFS negative dcache.

    Stats the parent chain (mount root -> ``.versioned`` -> ``{ns}`` -> leaf) to
    force fresh NFS lookups, then checks ``isdir_fn(path)``. Repeats with a short
    growing backoff until the path is visible or the monotonic deadline expires.

    Args:
        path: Absolute directory path that should become visible.
        timeout: Maximum seconds to wait before raising (bounded — #14).
        isdir_fn: Predicate that reports whether *path* is a directory.
        stat_fn: Stat callable used to refresh dcache on each chain entry.
        monotonic_fn: Monotonic clock source (injectable for tests).
        sleep_fn: Sleep callable (injectable for tests — no real sleeps in tests).

    Raises:
        RuntimeError: If *path* does not become visible within *timeout*
            (anti-fallback #2 — never silently return an invisible path).
    """
    chain = _parent_chain(path)
    deadline = monotonic_fn() + timeout
    interval = _INITIAL_POLL_INTERVAL_SECONDS
    attempts = 0

    while True:
        # Best-effort dcache busting: stat each ancestor (root-first) then leaf.
        # A stat that raises (ENOENT on a not-yet-propagated parent) is expected
        # and ignored — it still issues the NFS lookup that refreshes the cache.
        for entry in chain:
            try:
                stat_fn(entry)
            except OSError:
                pass

        attempts += 1
        if isdir_fn(path):
            if attempts > 1:
                logger.info(
                    "NFS read-after-create: '%s' became visible after %d poll(s)",
                    path,
                    attempts,
                )
            return

        if monotonic_fn() >= deadline:
            raise RuntimeError(
                f"NFS read-after-create visibility timeout: '{path}' did not "
                f"become visible within {timeout}s ({attempts} polls). The "
                f"create side reported success but the path is not yet resolvable "
                f"over NFS on this node."
            )

        sleep_fn(interval)
        interval = min(interval * 2, _MAX_POLL_INTERVAL_SECONDS)
