"""Golden-repo SCIP lifecycle funnel (EVO-65021, stream-05).

Single funnel through which BOTH golden-repo SCIP entrypoints route so the
config-selected backend (stream-01 factory) covers both:

* ``global_repos/refresh_scheduler.py`` — the golden-repo index job's
  ``enable_scip`` step (was a synchronous ``cidx scip generate`` subprocess);
* ``server/services/activated_repo_index_manager.py`` — ``_execute_scip_indexing``
  (also a ``cidx scip generate`` subprocess).

Behaviour by backend (:func:`funnel_scip_generation`):

* ``scip_backend='local'`` (default) → :data:`ScipDelegationOutcome.NOT_DELEGATED`;
  the caller runs its existing local subprocess path **unchanged**, and the
  Claude-Code self-heal (``scip_self_healing.py``) stays available for it.
* ``scip_backend='delegated'`` → the delegated build is enqueued on the
  serialized :class:`DelegatedScipBuildQueue` and the caller does NOT run a local
  build. The delegated build *supersedes* the Claude-Code self-heal (the sandbox
  build IS the resolution; there is no toolchain in the indexing pod for
  Claude-Code to fix anyway — EVO-65020).

FAIL-SOFT is the whole point (contract §6 + the sandbox contract diff): under
``delegated``, a Phase-1 TS/JS-only sandbox returns exit 69 for every non-TS
golden repo, which :class:`DelegatedIndexer` maps to a FAILED ``IndexerResult``.
That FAILED result MUST NOT fail the enclosing golden-repo index job. This funnel
therefore:

* never raises into the index pipeline (every path is wrapped);
* on any gap (no golden alias, no retained ``workspace_id``, an unreachable
  sandbox at index time, or any unexpected error) logs + meters the skip and
  returns :data:`ScipDelegationOutcome.SKIPPED`, leaving the repo at the existing
  ``"skipped: No SCIP index available"`` state;
* the FAILED build itself is fail-soft-dropped inside the queue worker
  (``_process``), not here — the funnel only enqueues.
"""

from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from code_indexer.server.services.scip_resolution_queue import (
    DelegatedScipBuild,
    DelegatedScipBuildQueue,
    get_delegated_scip_build_queue,
    record_scip_metric,
)
from code_indexer.utils.git_runner import get_current_commit

logger = logging.getLogger(__name__)

# Sidecar file under ``.code-indexer/scip/`` recording the commit the SCIP DBs
# were built at, for the idempotency/freshness check (task #6).
_BUILD_COMMIT_MARKER = ".build_commit"


class ScipDelegationOutcome(Enum):
    """Result of routing one repo's SCIP generation through the funnel."""

    NOT_DELEGATED = "not_delegated"  # local backend → caller runs its local path
    ENQUEUED = "enqueued"  # delegated build queued
    DEDUPED = "deduped"  # same repo+commit already queued/in-flight
    FRESH = "fresh"  # .scip.db already matches the current commit
    SKIPPED = "skipped"  # fail-soft: no alias / no workspace_id / error


# --------------------------------------------------------------------------- #
# Freshness / idempotency (task #6)
# --------------------------------------------------------------------------- #
def _scip_db_exists(scip_dir: Path) -> bool:
    """True iff at least one built ``index.scip.db`` exists under *scip_dir*."""
    if not scip_dir.exists():
        return False
    for _ in scip_dir.rglob("index.scip.db"):
        return True
    return False


def scip_db_is_fresh(repo_root: Path, scip_dir: Path) -> bool:
    """True iff the built SCIP DBs already match ``repo_root``'s current commit.

    Requires (a) a resolvable HEAD, (b) at least one ``index.scip.db`` present,
    and (c) the ``.build_commit`` marker equal to HEAD. Any missing piece → not
    fresh (rebuild). Pins the build to the same workspace snapshot cidx indexes.
    """
    commit = get_current_commit(repo_root)
    if not commit:
        return False
    if not _scip_db_exists(scip_dir):
        return False
    marker = scip_dir / _BUILD_COMMIT_MARKER
    try:
        recorded = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    # bool() guards warn_return_any: get_current_commit may be seen as Any.
    return bool(recorded == commit)


def record_scip_build_commit(scip_dir: Path, commit: Optional[str]) -> None:
    """Persist *commit* as the freshness marker after a successful build."""
    if not commit:
        return
    try:
        scip_dir.mkdir(parents=True, exist_ok=True)
        (scip_dir / _BUILD_COMMIT_MARKER).write_text(commit, encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "EVO-65021: could not write SCIP freshness marker at %s: %s",
            scip_dir,
            exc,
        )


def remove_scip_build_commit(scip_dir: Path) -> None:
    """Delete the freshness marker (EVO-65021 MAJOR-5).

    Called on a ``clear=True`` rebuild so the stale marker cannot short-circuit
    the delegated build to ``FRESH``, and so a rebuild that then fails cannot be
    reported fresh off the old marker.
    """
    marker = scip_dir / _BUILD_COMMIT_MARKER
    try:
        marker.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning(
            "EVO-65021: could not remove SCIP freshness marker at %s: %s",
            scip_dir,
            exc,
        )


# --------------------------------------------------------------------------- #
# The single funnel (tasks #1–#5)
# --------------------------------------------------------------------------- #
def _resolve_golden_repo_manager(golden_repo_manager: Optional[Any]) -> Any:
    if golden_repo_manager is not None:
        return golden_repo_manager
    # Lazy import: avoids a module-load dependency on the heavy repositories
    # package (and any import cycle through app state).
    from code_indexer.server.repositories.golden_repo_manager import (
        get_golden_repo_manager,
    )

    return get_golden_repo_manager()


def funnel_scip_generation(
    *,
    alias: Optional[str],
    repo_root: Path,
    config: Any,
    clear: bool = False,
    activated_repo: bool = False,
    golden_repo_manager: Optional[Any] = None,
    build_queue: Optional[DelegatedScipBuildQueue] = None,
    generator_builder: Optional[Callable[..., Any]] = None,
) -> ScipDelegationOutcome:
    """Route one repo's SCIP generation through the config-selected backend.

    Parameters
    ----------
    alias:
        Golden-repo alias (needed to resolve the sandbox ``workspace_id`` under
        the delegated backend). ``None``/empty → delegated fail-soft skip.
        This MUST be a **golden** alias (bare, no ``-global`` suffix): the
        delegated resolver is ``GoldenRepoManager.resolve_workspace_id``, which
        is keyed by the bare golden alias. The golden entrypoint normalizes the
        ``-global`` registry alias to bare before calling here (CRITICAL-1).
    repo_root:
        Repo root (== the sandbox ``/workspace`` mount for golden repos).
    config:
        Server config exposing ``scip_backend`` and (delegated) ``workspace_mcp``.
    clear:
        When ``True`` (a rebuild-from-scratch request, e.g.
        ``trigger_reindex(..., clear=True)``), bypass the ``.build_commit``
        freshness short-circuit and drop the stale marker so the delegated build
        actually re-runs and a failed rebuild cannot be reported ``FRESH``
        (MAJOR-5).
    activated_repo:
        ``True`` when called from the activated-repo (per-user) entrypoint. An
        activated alias lives in a different namespace from the golden aliases
        this funnel resolves, and activated repos carry no retained
        ``workspace_id`` of their own, so there is no correct sandbox workspace
        to build in. Under the delegated backend this fail-softs to ``SKIPPED``
        rather than resolving through the golden manager and building the wrong
        workspace (CRITICAL-2).
    golden_repo_manager, build_queue, generator_builder:
        Injection seams for tests; default to the process singletons and to
        ``build_scip_generator_for_golden_repo`` (stream-01 seam #3).

    Returns
    -------
    ScipDelegationOutcome
        ``NOT_DELEGATED`` for the local backend (caller runs its own path); one
        of ``ENQUEUED`` / ``DEDUPED`` / ``FRESH`` / ``SKIPPED`` for delegated.
        NEVER raises — the enclosing index job must not be failed by SCIP.
    """
    try:
        backend = getattr(config, "scip_backend", "local") or "local"
        if backend != "delegated":
            return ScipDelegationOutcome.NOT_DELEGATED

        repo_root = Path(repo_root)

        # EVO-65021 CRITICAL-2: an activated (per-user) alias is NOT a golden
        # alias. Activated repos carry no retained workspace_id of their own
        # (ActivatedRepoManager stores only user_alias/golden_repo_alias/
        # repo_path), so resolving through the golden manager would either build
        # the GOLDEN workspace — never indexing the user's private branch/state,
        # while colliding on the golden repo's own alias@commit dedupe key — or,
        # for a custom ``--as`` alias, miss and skip. Until an activated-repo
        # workspace story exists, fail soft to SKIPPED rather than silently
        # building the wrong workspace.
        if activated_repo:
            logger.warning(
                "EVO-65021 fail-soft: delegated SCIP is not supported for "
                "activated repo '%s' — activated repos have no retained "
                "workspace_id; skipping (repo stays 'skipped: No SCIP index "
                "available')",
                alias,
            )
            record_scip_metric("delegated_skipped_activated_repo")
            return ScipDelegationOutcome.SKIPPED

        if not alias:
            logger.warning(
                "EVO-65021 fail-soft: delegated SCIP requested but no golden "
                "alias is available for %s; skipping (repo stays 'skipped: No "
                "SCIP index available')",
                repo_root,
            )
            record_scip_metric("delegated_skipped_no_alias")
            return ScipDelegationOutcome.SKIPPED

        scip_dir = repo_root / ".code-indexer" / "scip"
        if clear:
            # MAJOR-5: a clear rebuild must never be answered FRESH. Drop the
            # stale marker and skip the freshness short-circuit entirely.
            remove_scip_build_commit(scip_dir)
        elif scip_db_is_fresh(repo_root, scip_dir):
            logger.info(
                "EVO-65021: SCIP DBs already fresh for '%s'; skipping delegated "
                "build",
                alias,
            )
            record_scip_metric("delegated_fresh")
            return ScipDelegationOutcome.FRESH

        grm = _resolve_golden_repo_manager(golden_repo_manager)
        builder = generator_builder or _default_generator_builder
        generator = builder(
            config,
            repo_root,
            golden_repo_manager=grm,
            alias=alias,
        )
        if generator is None:
            # stream-01 seam #3 fail-soft: no retained workspace_id for this repo.
            logger.warning(
                "EVO-65021 fail-soft: no retained workspace_id for '%s'; "
                "skipping delegated SCIP (repo stays 'skipped: No SCIP index "
                "available')",
                alias,
            )
            record_scip_metric("delegated_skipped_no_workspace_id")
            return ScipDelegationOutcome.SKIPPED

        commit = get_current_commit(repo_root) or ""
        dedupe_key = f"{alias}@{commit}"
        queue = build_queue or get_delegated_scip_build_queue()

        def _on_success(_result: Any) -> None:
            record_scip_build_commit(scip_dir, commit)

        build = DelegatedScipBuild(
            alias=alias,
            dedupe_key=dedupe_key,
            run_build=generator.generate,
            on_success=_on_success,
        )
        enqueued = queue.enqueue(build)
        if enqueued:
            record_scip_metric("delegated_enqueued")
            logger.info(
                "EVO-65021: enqueued delegated SCIP build for '%s' (%s); index "
                "job continues without blocking on the build",
                alias,
                dedupe_key,
            )
            return ScipDelegationOutcome.ENQUEUED
        return ScipDelegationOutcome.DEDUPED

    except Exception as exc:  # THE fail-soft guarantee — never raise into the job
        logger.warning(
            "EVO-65021 fail-soft: delegated SCIP funnel error for '%s' (%s): %s; "
            "repo stays 'skipped: No SCIP index available'",
            alias,
            repo_root,
            exc,
            exc_info=True,
        )
        record_scip_metric("delegated_skipped_error")
        return ScipDelegationOutcome.SKIPPED


def _default_generator_builder(
    config: Any,
    repo_root: Path,
    *,
    golden_repo_manager: Any,
    alias: str,
) -> Any:
    """Default generator builder: stream-01 seam #3 (lazy-imported)."""
    from code_indexer.server.startup.scip_backend_wiring import (
        build_scip_generator_for_golden_repo,
    )

    return build_scip_generator_for_golden_repo(
        config,
        repo_root,
        golden_repo_manager=golden_repo_manager,
        alias=alias,
    )
