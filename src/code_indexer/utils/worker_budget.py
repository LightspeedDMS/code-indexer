"""Splitting a node-wide budget across the uvicorn worker processes on it.

The server runs under ``uvicorn --workers N``. Every worker is a separate
process with its own imports, its own module globals and its own thread pools,
so a limit expressed once in config is applied N times on the node. On
``tools-*`` that is x2 per pod and x12 across the 6-replica StatefulSet -- which
is how a "5 concurrent background jobs" setting becomes 60 threads competing for
one Postgres and one NFS mount.

The arithmetic is trivial; the reason this is a shared module is that the
*worker count* is not. There is no single reliable source for it:

* ``get_applied_worker_count()`` reads the live systemd ExecStart and then
  applied_launch.json. A Kubernetes pod has neither, so on the cluster -- the one
  deployment where this matters most -- it always answers 1 and divides nothing.
* ``config.json`` *may* carry ``workers``. This module used to claim it was
  "the rung that works inside a Kubernetes pod", which was wrong: the live pod's
  ``/data/config.json`` has no ``workers`` key, so the lookup returned None and
  every budget divided by 1. Kept as a source for deployments that do set it.
* **the process command line** -- ``--workers N``, read from /proc. This is the
  one that actually works in a pod, because it is what launched the process and
  so cannot drift from reality.

So all three are consulted and the LARGEST wins. config.json is the target an admin
saved; the applied resolver is what the running unit was launched with, and a
restart may not have happened yet. The larger count yields the smaller
per-process bound, which is the only direction that cannot exceed the node's CPU
and memory. Guessing high costs a queued job; guessing low costs the
oversubscription this module exists to remove.

Originally written inside ``xray/rust_backend.py`` for the rustc compile budget
(the only budget that was divided at all). Lifted here, unchanged in behaviour,
when the background-job pools and the refresh budget needed the same treatment --
and placed in ``code_indexer/utils`` rather than under ``server/`` because the
xray path is also reachable from the CLI and must not pull in the server layer at
import time.

Nothing here raises: ``detect_worker_count`` runs at module import in at least
one caller, where an exception would be an unimportable package rather than a
mis-sized pool.

See ``docs/background-work-distribution.md`` (rule R4) for when to apply this.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Where the server config file lives. The cluster's initContainer seeds
# $CIDX_SERVER_DATA_DIR/config.json from the ConfigMap; the VM deployment uses
# ~/.cidx-server/config.json.
_CONFIG_JSON_DIR_ENVS: Tuple[str, ...] = ("CIDX_SERVER_DATA_DIR",)
_CIDX_SERVER_DIR_NAME = ".cidx-server"
_CONFIG_JSON_NAME = "config.json"

WorkerCountSource = Callable[[], Optional[int]]


def coerce_worker_count(value: Any) -> Optional[int]:
    """Return ``value`` as a worker count, or ``None`` if it is not usable.

    ``bool`` is rejected explicitly: it is an ``int`` subclass, so ``True`` would
    otherwise silently resolve to "1 worker" and divide nothing.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 1 else None


def read_config_json_workers(
    extra_dir_envs: Iterable[str] = (),
) -> Optional[int]:
    """Read ``workers`` from the server config file, or ``None``.

    ``extra_dir_envs`` adds caller-specific directories to the search, ahead of
    the default home-directory fallback -- xray passes its cache-dir env so this
    module need not know about xray.

    Never raises: a missing, unreadable or malformed file is "unknown".
    """
    candidates = []
    for env_name in (*_CONFIG_JSON_DIR_ENVS, *extra_dir_envs):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            candidates.append(Path(raw).expanduser())
    candidates.append(Path.home() / _CIDX_SERVER_DIR_NAME)

    for directory in candidates:
        path = directory / _CONFIG_JSON_NAME
        try:
            if not path.is_file():
                continue
            workers = coerce_worker_count(json.loads(path.read_text()).get("workers"))
        except Exception as exc:  # unreadable/malformed -> unknown, not fatal
            logger.debug("worker_budget: could not read %s: %s", path, exc)
            continue
        if workers is not None:
            return workers
    return None


#: How far up the parent chain to look for ``--workers``. A uvicorn worker's
#: parent is the master, so 1 hop suffices; a couple more costs nothing and
#: survives a wrapper process (tini, a shell) being inserted.
_ARGV_MAX_PARENT_HOPS = 4


def _argv_of(pid: str, proc_root: Path) -> list:
    raw = (proc_root / pid / "cmdline").read_bytes()
    return [p for p in raw.decode("utf-8", "replace").split("\0") if p]


def _ppid_of(pid: str, proc_root: Path) -> Optional[str]:
    for line in (proc_root / pid / "status").read_text().splitlines():
        if line.startswith("PPid:"):
            return line.split()[1]
    return None


#: Token shapes that mean "this command line LAUNCHED the cidx server".
#:
#: Checked as a module path or a script path, never as a bare substring. A
#: substring test on "code_indexer" matches any command line that merely
#: mentions it -- ``pytest /src/code_indexer/tests --workers 4``,
#: ``uvicorn other.app --workers 8 --reload-dir /src/code_indexer``, or anything
#: run from a directory with that name. The end-to-end test in this repo was
#: itself such a false positive: a script called ``code_indexer_server_main.py``
#: that is not a server at all.
#:
#: Covers every real launcher:
#:   python -m code_indexer.server.main --workers N      (the pod)
#:   uvicorn code_indexer.server.app:app --workers N
#:   python /…/site-packages/code_indexer/server/main.py --workers N
_SERVER_MODULE_PREFIX = "code_indexer.server."
_SERVER_SCRIPT_SUFFIXES = (
    "code_indexer/server/main.py",
    "code_indexer\\server\\main.py",
)


def _is_server_argv(argv: list) -> bool:
    """Whether this command line actually invokes the cidx server."""
    for part in argv:
        if part.startswith(_SERVER_MODULE_PREFIX):
            return True
        if part.endswith(_SERVER_SCRIPT_SUFFIXES):
            return True
    return False


def _workers_in_argv(argv: list) -> Optional[int]:
    """``--workers N`` or ``--workers=N`` from one command line, else None.

    Returns None for a command line that carries the flag but did not launch
    our server -- see :data:`_SERVER_MODULE_PREFIX`.
    """
    if not _is_server_argv(argv):
        return None
    for i, part in enumerate(argv):
        if part == "--workers" and i + 1 < len(argv):
            try:
                return coerce_worker_count(int(argv[i + 1]))
            except ValueError:
                return None
        if part.startswith("--workers="):
            try:
                return coerce_worker_count(int(part.split("=", 1)[1]))
            except ValueError:
                return None
    return None


def read_argv_workers(
    proc_root: Path = Path("/proc"),
    pid: str = "self",
    max_hops: int = _ARGV_MAX_PARENT_HOPS,
) -> Optional[int]:
    """Read ``--workers N`` from this process's command line, or an ancestor's.

    This is the rung that actually works in a pod, and the reason it exists:
    the other two do not.

    * ``get_applied_worker_count()`` reads systemd/applied_launch.json. A pod has
      neither, so it answers 1.
    * ``config.json`` was documented here as "the rung that works inside a
      Kubernetes pod, whose ``workers`` the overlay keeps in lockstep with the
      ``--workers`` it passes in the container args". **That was never true.**
      Checked on a live pod: the container args are
      ``... --workers 2`` while ``/data/config.json`` has no ``workers`` key at
      all, so the lookup returned None and every budget divided by 1 -- the
      cluster being the one deployment the division was written for.

    The command line cannot drift from reality the way a parallel config key
    can, because it IS what launched the process.

    Only a command line that actually INVOKES the cidx server counts (module
    or script path, not a substring): the walk would otherwise take the first
    ``--workers`` it met, so an unrelated ancestor carrying one -- or any
    process run from a directory named code_indexer -- would set the budget
    for a cidx process beneath it.

    The parent walk is required, not defensive: uvicorn spawns its workers
    through multiprocessing, so a worker's OWN argv is
    ``python -B -c from multiprocessing.spawn import spawn_main; ...`` and the
    flag lives on the master. Observed live: master pid 7 carries
    ``--workers 2``; workers pid 9/10 carry ``spawn_main``.

    Linux-only (/proc). Returns None anywhere else, and never raises -- this
    runs at module import.
    """
    try:
        current: Optional[str] = pid
        # Bounded by max_hops, which is also what makes a PPid cycle safe: a pid
        # whose parent is itself simply exhausts the hops and answers None. The
        # bound is the guard -- an unbounded walk here spins forever on such a
        # cycle, which is not hypothetical: a leftover mutation turned this into
        # `while True` and hung the test suite.
        for _ in range(max_hops + 1):
            if current is None:
                return None
            found = _workers_in_argv(_argv_of(current, proc_root))
            if found is not None:
                return found
            nxt = _ppid_of(current, proc_root)
            if nxt is None or nxt == "0":
                return None
            current = nxt
        return None
    except Exception as exc:  # noqa: BLE001 -- unreadable /proc is "unknown"
        logger.debug("worker_budget: argv worker-count lookup failed: %s", exc)
        return None


def read_applied_worker_count() -> Optional[int]:
    """Read the APPLIED worker count via the repo's existing resolver.

    Lazily imported so this module stays usable from the CLI/solo path without
    eagerly pulling in server-layer modules.

    Returns ``None`` only when the resolver is unavailable or raises. It cannot
    distinguish "no source found" from "this node really runs one worker" -- the
    resolver collapses both to 1 -- which is why :func:`detect_worker_count` takes
    the MAX across sources rather than the first answer: a 1 from here must never
    out-vote a config.json that says 2.
    """
    try:
        from code_indexer.server.services.applied_worker_count import (  # noqa: PLC0415,E501
            get_applied_worker_count,
        )

        return coerce_worker_count(int(get_applied_worker_count()))
    except Exception as exc:
        logger.debug("worker_budget: applied worker count unavailable: %s", exc)
        return None


def detect_worker_count(
    sources: Optional[Sequence[WorkerCountSource]] = None,
) -> int:
    """Number of uvicorn worker processes sharing this node.

    Takes the LARGER of its sources when they disagree -- see the module
    docstring for why that asymmetry is the safe one.

    ``sources`` exists so a caller can supply its own lookups (xray adds its
    cache dir to the config.json search). Defaults to 1 -- no division, i.e.
    today's behaviour -- and never raises.
    """
    if sources is None:
        sources = (
            read_argv_workers,
            read_applied_worker_count,
            read_config_json_workers,
        )
    counts = []
    for source in sources:
        try:
            value = coerce_worker_count(source())
        except Exception as exc:  # a lookup must never break import
            logger.debug("worker_budget: worker-count source failed: %s", exc)
            continue
        if value is not None:
            counts.append(value)
    return max(counts) if counts else 1


def per_process_budget(budget: int, workers: int) -> int:
    """Split a node-wide ``budget`` across ``workers`` processes.

    Parameter named ``budget`` (not ``node_budget``) because callers pass it by
    keyword and the xray compile-budget tests already spell it that way.

    Floored at 1 in both directions:

    * a bound of 0 is not a smaller bound, it is a deadlock -- a zero-size worker
      pool accepts jobs and runs none of them, and a zero refresh budget means no
      repo is ever refreshed. Both present as a hang, not as a limit.
    * a worker count of 0 or less divides by nothing rather than raising or
      silently gutting the pool. "Unknown" must mean "unchanged".
    """
    return max(1, budget // max(1, workers))
