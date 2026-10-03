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
* ``config.json`` carries ``workers``, seeded into the pod from the ConfigMap,
  whose value the overlay requires to stay in lockstep with the ``--workers`` it
  passes in the container args. That is the rung that works in a pod, and the one
  that does not exist on a developer's laptop.

So both are consulted and the LARGER wins. config.json is the target an admin
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
        sources = (read_applied_worker_count, read_config_json_workers)
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
