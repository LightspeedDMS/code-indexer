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

Two-pronged barrier (staging-proven hardening)
----------------------------------------------
1. **Ancestor stat (negative-lookup bust).** NFS close-to-open consistency
   means an explicit lookup (``stat``/GETATTR) on a parent directory refreshes
   its dcache so a not-yet-resolved leaf becomes visible. We stat the parent
   chain — mount root, ``.versioned``, ``.versioned/{ns}`` — before (and on
   every retry alongside) the leaf check, busting NEGATIVE entries along the
   path.
2. **Parent READDIR (directory-entry-cache bust).** Staging PROVED that under
   CONCURRENT load — a large reflink (langfuse 3.3 GB) creating at the same
   time as another create — a freshly-created child can stay invisible for
   >15s. The NFS client's DIRECTORY ATTRIBUTE / entry cache for the parent is
   stale, and a bare GETATTR on the parent does NOT refresh its entry list. A
   READDIR RPC (``os.listdir`` / ``os.scandir``) on the IMMEDIATE parent
   forces the client to refresh the directory-entry cache, so a child the
   create side already produced becomes resolvable immediately. We therefore
   READDIR the immediate parent on every poll. The READDIR is tolerant of
   ``FileNotFoundError``/``OSError`` — the parent itself may momentarily be
   not-yet-visible, in which case we keep polling.

The local backend does NOT need this barrier: ``cp`` writes the directory on the
same node that reads it, so it is immediately visible (no NFS round trip, no
remote dcache to bust).

Runtime timeout knob
--------------------
The deadline is a runtime / Web-UI-tunable config value
(``ServerConfig.nfs_visibility_timeout_seconds``, default 60.0 — a generous
safety net for extreme contention; the READDIR fix makes the common case fast).
``_configured_visibility_timeout()`` reads it, falling back to the module
constant when the config service is unavailable (e.g. before lifespan wiring) or
the configured value is non-positive. The module constant
:data:`NFS_VISIBILITY_TIMEOUT_SECONDS` is the fallback/default only.

Anti-fallback (#2): if the path never becomes visible within the bounded
deadline we RAISE — we never silently return an invisible path.
Anti-unbounded-loop (#14): the poll loop is bounded by a monotonic deadline.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


def get_config_service() -> Any:
    """Return the runtime config service (lazy import).

    This low-level shared util must NOT pull the config-service dependency
    (and its transitive crypto stack) at import time — that would bloat the
    ``clone_backend`` re-export chain and the module's import graph. The import
    is deferred to call time. Patchable as ``nfs_visibility.get_config_service``
    in tests.
    """
    from code_indexer.server.services.config_service import (
        get_config_service as _get,
    )

    return _get()


# Bounded read-after-create visibility deadline (seconds). Named constant — no
# magic literal buried at the call site, and the FALLBACK/DEFAULT for the runtime
# ``nfs_visibility_timeout_seconds`` knob. Real NFS propagation after a successful
# create is typically sub-second, but staging proved that under concurrent reflink
# load a child can take >15s to propagate; 60s is a generous ceiling before
# failing loud (the READDIR cache-bust below makes the common case fast).
NFS_VISIBILITY_TIMEOUT_SECONDS: float = 60.0

# Backoff bounds for the poll interval (seconds). Each poll now issues a READDIR
# RPC on the parent (real cost), so we use a deliberately coarse 0.2 -> 0.5s
# cadence rather than thousands of sub-millisecond micro-polls.
_INITIAL_POLL_INTERVAL_SECONDS: float = 0.2
_MAX_POLL_INTERVAL_SECONDS: float = 0.5


def _configured_visibility_timeout() -> float:
    """Return the runtime-configured visibility timeout, or the safe fallback.

    Reads ``ServerConfig.nfs_visibility_timeout_seconds`` (runtime / Web UI
    tunable). Falls back to :data:`NFS_VISIBILITY_TIMEOUT_SECONDS` when the config
    service is not yet available (e.g. CLI / pre-lifespan) or the configured value
    is non-positive (a zero/negative deadline would instantly time out).
    """
    try:
        value = float(get_config_service().get_config().nfs_visibility_timeout_seconds)
    except Exception:
        return NFS_VISIBILITY_TIMEOUT_SECONDS
    if value <= 0.0:
        return NFS_VISIBILITY_TIMEOUT_SECONDS
    return value


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
    listdir_fn: Callable[[str], list] = os.listdir,
    monotonic_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Block until *path* is a visible directory, busting the NFS caches.

    On each poll: stat the parent chain (mount root -> ``.versioned`` -> ``{ns}``
    -> leaf) to bust NEGATIVE lookups, AND READDIR the immediate parent (force a
    directory-entry-cache refresh so a child the create side already produced
    becomes resolvable under concurrent-load staleness), then check
    ``isdir_fn(path)``. Repeats with a short growing backoff until the path is
    visible or the monotonic deadline expires.

    Args:
        path: Absolute directory path that should become visible.
        timeout: Maximum seconds to wait before raising (bounded — #14).
        isdir_fn: Predicate that reports whether *path* is a directory.
        stat_fn: Stat callable used to refresh dcache on each chain entry.
        listdir_fn: Readdir callable used to refresh the immediate parent's
            directory-entry cache each poll (injectable for tests).
        monotonic_fn: Monotonic clock source (injectable for tests).
        sleep_fn: Sleep callable (injectable for tests — no real sleeps in tests).

    Raises:
        RuntimeError: If *path* does not become visible within *timeout*
            (anti-fallback #2 — never silently return an invisible path).
    """
    chain = _parent_chain(path)
    parent = str(Path(path).parent)
    deadline = monotonic_fn() + timeout
    interval = _INITIAL_POLL_INTERVAL_SECONDS
    attempts = 0

    while True:
        # Negative-lookup bust: stat each ancestor (root-first) then leaf. A stat
        # that raises (ENOENT on a not-yet-propagated parent) is expected and
        # ignored — it still issues the NFS lookup that refreshes the cache.
        for entry in chain:
            try:
                stat_fn(entry)
            except OSError:
                pass

        # Directory-entry-cache bust: READDIR the immediate parent. This is the
        # load-bearing fix for the concurrent-reflink staleness — a GETATTR on the
        # parent does NOT refresh its entry list, but a READDIR does. Tolerant of
        # the parent itself being momentarily not-yet-visible.
        try:
            listdir_fn(parent)
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
