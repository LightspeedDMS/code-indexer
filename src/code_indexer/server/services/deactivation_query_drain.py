"""wait_for_activated_repo_query_drain() -- Story #1458 AC13.

Deactivation's bounded QueryTracker-refcount-aware drain before purging the
consolidated clone. Reuses the SAME bounded-wait-then-proceed shape Story
#1457 AC11 establishes for in-repo temporal reclamation: a config-sourced
bound (Web-UI-configurable, no env var; mirrors `snapshot_retention_keep_
last`/`snapshot_min_retention_age_seconds`, default ~30s), and on expiry
LOG a WARNING naming the path + elapsed wait and PROCEED with the purge --
never an unbounded/blocking wait that could wedge deactivation.

Scope is SAME-WORKER-PROCESS, per Story #1457 AC14: this drain protects an
in-flight reader on the SAME worker process that holds the refcount; a
reader on a different worker/node falls into the SAME accepted bounded/
graceful residual (its query fails, is logged, other work is unaffected;
data is not lost -- the golden repo's data is untouched by an activated-
clone deactivation). No cross-worker/cross-node refcount backend is built
for this.
"""

from __future__ import annotations

import logging
import math
import os
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from code_indexer.server.services.config_service import get_config_service

logger = logging.getLogger(__name__)

#: Bounded poll granularity (Messi Rule #14: the loop terminates via the
#: monotonic deadline check below, never unconditionally).
_POLL_INTERVAL_SECONDS = 0.05

#: Codex MEDIUM finding (round 5): absolute safety ceiling on the drain
#: wait, independent of what config or a caller-supplied override says.
#: A misconfigured (or non-finite -- inf/-inf/NaN, e.g. from a bad
#: config-read) max_wait_seconds must never let this "bounded wait"
#: become genuinely unbounded -- that would defeat this module's own
#: core guarantee (see module docstring) that deactivation is never
#: wedged indefinitely.
_ABSOLUTE_MAX_DRAIN_WAIT_SECONDS = 300.0

_GLOBAL_ALIAS_SUFFIX = "-global"


class RepositoryDeactivatingError(Exception):
    """Raised by :func:`track_activated_repo_query` when a query attempts
    to start against a path that is currently marked quiescing (Codex
    round-6 HIGH finding #6b -- the real admission barrier).

    Deactivation marks a path quiescing BEFORE draining/renaming, so any
    query that would otherwise race in AFTER the drain has already
    observed zero refcount is refused here instead of being admitted --
    closing the residual "drain sees zero, then a late query starts,
    then rename proceeds, then the query reads a path that's already
    gone" race that reordering alone could not close.
    """


def build_activated_repo_refcount_key(
    activated_repos_dir: str, username: str, repository_alias: str
) -> str:
    """The SAME path-key format `wait_for_activated_repo_query_drain`
    polls and `_do_deactivate_single`/composite construct -- byte-for-byte
    identical (QueryTracker does EXACT-STRING matching, no normalization).
    """
    return os.path.join(activated_repos_dir, username, str(repository_alias))


@contextmanager
def track_activated_repo_query(
    query_tracker: Optional[Any],
    activated_repo_manager: Optional[Any],
    username: str,
    repository_alias: Optional[str],
) -> Iterator[None]:
    """Codex Finding #7 (HIGH): wrap ONE activated-repo query read with
    QueryTracker refcounting, so deactivation's bounded drain
    (`wait_for_activated_repo_query_drain`) has something real to observe
    for THIS caller too -- previously only MCP search.py's
    `_search_activated_repo` wired this, leaving the REST
    (`inline_query.py`) and wiki (`wiki/routes.py`) query entry points able
    to read a clean (zero) refcount while a deactivation purges the
    activated clone's `chunks.db` mid-query.

    Fail-open / true no-op (never constructs a key, never touches the
    tracker) when: ``query_tracker`` is None (no tracker configured, e.g.
    solo/CLI), ``activated_repo_manager`` is None, ``repository_alias`` is
    falsy (e.g. an omni/cross-repo query with no single target alias), or
    ``repository_alias`` ends with ``-global`` -- golden-repo queries are a
    SEPARATE, already-covered refcounting concern
    (`_execute_tracked_search`/`_search_global_repo`), out of scope for
    this activated-repo-specific helper.
    """
    refcount_key: Optional[str] = None
    if (
        query_tracker is not None
        and activated_repo_manager is not None
        and repository_alias
        and not str(repository_alias).endswith(_GLOBAL_ALIAS_SUFFIX)
    ):
        refcount_key = build_activated_repo_refcount_key(
            activated_repo_manager.activated_repos_dir,
            username,
            repository_alias,
        )
        # Codex round-6 HIGH finding #6b: the real admission barrier.
        # Codex round-8 HIGH finding: the check (is_quiescing) and the
        # act (increment_ref) MUST be a single atomic operation -- two
        # separate lock-acquiring calls left a TOCTOU window where
        # another thread could mark this path quiescing in the gap
        # between the check and the increment, and the increment would
        # still succeed. `try_increment_ref_if_not_quiescing` performs
        # both under ONE lock acquisition, closing that race, while
        # still closing the original round-6 residual late-admission
        # race that drain-then-rename reordering alone left open.
        #
        # `is False` (never bare truthiness / `is not True`): a genuine
        # QueryTracker.try_increment_ref_if_not_quiescing() always
        # returns a real Python bool, but MANY pre-existing tests pass a
        # bare, unconfigured MagicMock() as query_tracker (e.g. via an
        # unconfigured app.state.query_tracker) -- unittest.mock.
        # MagicMock instances return another (truthy, but not `is
        # False`) Mock for any unconfigured method call. `is False`
        # only matches the real boolean singleton, so an unconfigured
        # mock is treated as "admitted" (the established fail-open
        # default for this widespread test-double pattern), never as
        # "refused".
        if query_tracker.try_increment_ref_if_not_quiescing(refcount_key) is False:
            raise RepositoryDeactivatingError(
                f"Refusing to admit a new activated-repo query for "
                f"{refcount_key!r} -- this repository is currently being "
                f"deactivated."
            )
    try:
        yield
    finally:
        if refcount_key is not None:
            # refcount_key is only ever set inside the branch above that
            # already proved query_tracker is not None -- mypy cannot
            # carry that narrowing across the intervening yield, so make
            # the invariant explicit rather than silencing the checker.
            assert query_tracker is not None
            query_tracker.decrement_ref(refcount_key)


def wait_for_activated_repo_query_drain(
    query_tracker: Optional[Any],
    refcount_key: str,
    *,
    max_wait_seconds: Optional[float] = None,
) -> None:
    """Bounded wait for ``query_tracker`` to report zero active refcounts
    for ``refcount_key`` before the caller proceeds with a destructive
    purge.

    Args:
        query_tracker: The server's QueryTracker (``app.state.query_
            tracker``) -- the SAME instance the activated-repo query read
            path increments/decrements under this same key. None (no
            tracker configured, e.g. solo/CLI) is a fail-open no-op.
        refcount_key: The ORIGINAL, pre-rename activated-repo path string
            (exact-string match -- QueryTracker does no normalization).
            Callers MUST capture this BEFORE any Phase-1 rename, matching
            the SAME key format the activated-repo query read path uses.
        max_wait_seconds: Explicit bound override. When omitted, reads
            ``deactivation_query_drain_max_wait_seconds`` from the live
            config service (Runtime / Web UI configurable, re-read on
            every call -- no server restart required).
    """
    if query_tracker is None:
        return

    if max_wait_seconds is None:
        max_wait_seconds = (
            get_config_service().get_config().deactivation_query_drain_max_wait_seconds
        )

    # Codex MEDIUM finding (round 5): clamp to a sane finite bound BEFORE
    # the <=0 check -- a non-finite value (inf/-inf/NaN, e.g. from a bad
    # config-read or a caller-supplied override) or an excessively large
    # finite value must never let this "bounded wait" become genuinely
    # unbounded.
    if not math.isfinite(max_wait_seconds):
        logger.warning(
            "Deactivation query drain: max_wait_seconds=%r is not finite -- "
            "clamping to the safety ceiling of %.1fs",
            max_wait_seconds,
            _ABSOLUTE_MAX_DRAIN_WAIT_SECONDS,
        )
        max_wait_seconds = _ABSOLUTE_MAX_DRAIN_WAIT_SECONDS
    elif max_wait_seconds > _ABSOLUTE_MAX_DRAIN_WAIT_SECONDS:
        max_wait_seconds = _ABSOLUTE_MAX_DRAIN_WAIT_SECONDS

    if max_wait_seconds <= 0:
        return

    start = time.monotonic()
    deadline = start + max_wait_seconds

    while query_tracker.get_ref_count(refcount_key) > 0:
        if time.monotonic() >= deadline:
            logger.warning(
                "Deactivation query drain expired after %.2fs for %r "
                "(refcount=%d) -- proceeding with purge anyway",
                time.monotonic() - start,
                refcount_key,
                query_tracker.get_ref_count(refcount_key),
            )
            return
        time.sleep(min(_POLL_INTERVAL_SECONDS, max(0.0, deadline - time.monotonic())))
