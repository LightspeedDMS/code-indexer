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
  Activated (per-user) repos also return ``NOT_DELEGATED`` even under the
  ``delegated`` backend — they have no retained ``workspace_id`` and the
  delegated backend cannot address a per-user checkout, so the local subprocess
  path (which honours ``--clear``) is the only path that can index them.
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

from code_indexer.scip.paths import scip_index_dir
import logging
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from code_indexer.server.services.index_freshness import BUILD_COMMIT_MARKER

# The shared reader, bound to the name this module has always used so the existing
# patch seam (and every call site) is unchanged. It avoids a `git rev-parse`
# subprocess per repo on the common clone layout -- the sweep walks ~900 repos per
# tick -- and falls back to the subprocess for layouts it does not recognise.
from code_indexer.server.services.index_freshness import (
    current_commit as get_current_commit,
)
from code_indexer.server.services.scip_resolution_queue import (
    DelegatedScipBuild,
    DelegatedScipBuildQueue,
    get_delegated_scip_build_queue,
    record_scip_metric,
)

logger = logging.getLogger(__name__)

# Sidecar file under ``.code-indexer/scip/`` recording the commit the SCIP DBs
# were built at, for the idempotency/freshness check (task #6). The NAME lives in
# index_freshness so FTS uses the SAME convention rather than a second one; the
# richer rules below (no-index token, DB-shape awareness) stay here because only
# SCIP has them.
_BUILD_COMMIT_MARKER = BUILD_COMMIT_MARKER

# Token appended to the marker (``<commit> no-index``) when a fresh-eligible
# build produced NO index DB at all — i.e. every discovered project was an
# unsupported language (exit 69) under the Phase-1 sandbox image. Such a repo
# has nothing to index at this commit and retrying will not change that, so the
# freshness check must treat it as fresh WITHOUT requiring an ``index.scip.db``
# on disk (there will never be one), otherwise it re-enqueues forever. A build
# that DID produce an index writes the bare commit and still requires the DB to
# be present (so a later DB loss forces a rebuild).
_NO_INDEX_TOKEN = "no-index"


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

    Requires (a) a resolvable HEAD and (b) the ``.build_commit`` marker's commit
    equal to HEAD. In the normal case it ALSO requires (c) at least one
    ``index.scip.db`` present, so a later DB loss forces a rebuild. The one
    exception is a marker tagged ``no-index`` (a fresh-eligible build in which
    every project was an unsupported language, so no DB was or ever will be
    produced): that is fresh WITHOUT a DB, otherwise the repo re-enqueues a
    doomed rebuild every cycle. Any missing piece → not fresh (rebuild).
    """
    commit = get_current_commit(repo_root)
    if not commit:
        return False
    marker = scip_dir / _BUILD_COMMIT_MARKER
    try:
        recorded = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not recorded:
        return False
    parts = recorded.split()
    recorded_commit = parts[0]
    if recorded_commit != commit:
        return False
    no_index = len(parts) > 1 and parts[1] == _NO_INDEX_TOKEN
    if no_index:
        # Expected-empty build already done at this commit — nothing to rebuild.
        return True
    # EVO-66190: a root-buildable repo's build lands the TOP-LEVEL index.scip.db. A stale
    # per-subproject layout (nested DBs only, no root DB) must NOT read as fresh — else the
    # funnel answers FRESH off the old partial build and skips the rebuild that the
    # reconcile's (now shape-aware) _scip_present correctly enqueues, silently no-op'ing it.
    # Mirror that shape-awareness here so both gates agree on when a top-level DB is required.
    from code_indexer.scip.discovery import has_root_build_file

    if has_root_build_file(repo_root):
        return (scip_dir / "index.scip.db").is_file()
    # bool() guards warn_return_any: _scip_db_exists returns a plain bool.
    return bool(_scip_db_exists(scip_dir))


def record_scip_build_commit(
    scip_dir: Path, commit: Optional[str], *, produced_index: bool = True
) -> None:
    """Persist *commit* as the freshness marker after a fresh-eligible build.

    When *produced_index* is ``False`` (the build produced no ``index.scip.db``
    because every project was an unsupported language), the marker is tagged
    ``no-index`` so :func:`scip_db_is_fresh` treats the repo as fresh without a
    DB on disk (there will never be one) instead of thrashing.
    """
    if not commit:
        return
    content = commit if produced_index else f"{commit} {_NO_INDEX_TOKEN}"
    try:
        scip_dir.mkdir(parents=True, exist_ok=True)
        (scip_dir / _BUILD_COMMIT_MARKER).write_text(content, encoding="utf-8")
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
        Golden-repo alias sent to the sandbox under the delegated backend.
        ``None``/empty → delegated fail-soft skip. This MUST be a **golden**
        alias (bare, no ``-global`` suffix): under Option C (EVO-65365) the
        delegated backend hands the bare alias to the sandbox, which resolves it
        to the filesystem path ``<GOLDEN_REPOS_ROOT>/<alias>``. A ``-global``
        registry alias would resolve to a non-existent path → a generic failed
        build (not a clean typed skip), so the golden entrypoint normalizes the
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
        to build in. Under the delegated backend this returns ``NOT_DELEGATED``
        so the caller runs its existing local subprocess path (which can index
        the per-user checkout and honours ``--clear``) rather than resolving
        through the golden manager and building the wrong workspace, or dropping
        the index entirely (CRITICAL-2 / MAJOR).
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

        # EVO-65021 CRITICAL-2 / MAJOR: an activated (per-user) alias is NOT a
        # golden alias. Activated repos carry no retained workspace_id of their
        # own (ActivatedRepoManager stores only user_alias/golden_repo_alias/
        # repo_path), so resolving through the golden manager would build the
        # GOLDEN workspace — never indexing the user's private branch/state,
        # while colliding on the golden repo's own alias@commit dedupe key. The
        # delegated backend therefore has nothing to offer an activated repo.
        #
        # Return NOT_DELEGATED (not SKIPPED) so the caller runs its EXISTING
        # local ``cidx scip generate --project <repo_path>`` subprocess — that
        # on-disk per-user checkout is real and the local path is the only code
        # that can index it (and it already honours ``--clear``). Returning
        # SKIPPED here would suppress that fallback and leave the repo with no
        # SCIP index at all while reporting ``{"success": True}``.
        if activated_repo:
            logger.info(
                "EVO-65021: activated repo '%s' has no retained workspace_id; "
                "using the local SCIP path (delegated backend cannot address a "
                "per-user checkout)",
                alias,
            )
            record_scip_metric("delegated_not_supported_activated_repo")
            return ScipDelegationOutcome.NOT_DELEGATED

        if not alias:
            logger.warning(
                "EVO-65021 fail-soft: delegated SCIP requested but no golden "
                "alias is available for %s; skipping (repo stays 'skipped: No "
                "SCIP index available')",
                repo_root,
            )
            record_scip_metric("delegated_skipped_no_alias")
            return ScipDelegationOutcome.SKIPPED

        scip_dir = scip_index_dir(repo_root)
        if clear:
            # MAJOR-5: a clear rebuild must never be answered FRESH. Drop the
            # stale marker and skip the freshness short-circuit entirely.
            remove_scip_build_commit(scip_dir)
        elif scip_db_is_fresh(repo_root, scip_dir):
            logger.info(
                "EVO-65021: SCIP DBs already fresh for '%s'; skipping delegated build",
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
            # DEFENSIVE guard (stream-01 seam #3). Under Option C (EVO-65365) the
            # production builder (``build_scip_generator_for_golden_repo``) always
            # returns a generator for a golden repo — it addresses by bare alias
            # and no longer resolves a workspace_id — so a None here does NOT mean
            # "missing workspace_id"; it means the injected ``generator_builder``
            # seam returned None (a builder-seam/config anomaly, or a test). Kept
            # so such an anomaly fails soft (skip SCIP) rather than crashing the
            # index job.
            logger.warning(
                "EVO-65365 fail-soft: SCIP generator_builder returned None for "
                "'%s' (builder-seam/config anomaly — the Option C production "
                "builder always returns a generator); skipping delegated SCIP "
                "(repo stays 'skipped: No SCIP index available')",
                alias,
            )
            record_scip_metric("delegated_skipped_builder_returned_none")
            return ScipDelegationOutcome.SKIPPED

        commit = get_current_commit(repo_root) or ""
        dedupe_key = f"{alias}@{commit}"
        queue = build_queue or get_delegated_scip_build_queue()

        def _on_success(result: Any) -> None:
            # A fresh-eligible build with zero produced indexes (every project an
            # unsupported language) is tagged ``no-index`` so freshness does not
            # require an index.scip.db that will never exist.
            produced_index = bool(getattr(result, "successful_projects", 0) > 0)
            record_scip_build_commit(scip_dir, commit, produced_index=produced_index)

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


def funnel_golden_scip(
    alias: str, repo_root: Any, config: Any
) -> ScipDelegationOutcome:
    """Funnel a GOLDEN-repo SCIP generation, normalizing the alias first.

    The single home of the EVO-65021 CRITICAL-1 invariant, shared by every
    golden-repo entrypoint (``refresh_scheduler._run_source_scip``,
    ``golden_repo_manager._run_scip_index``). Under Option C (EVO-65365) the
    delegated backend addresses a golden repo by its BARE alias — the sandbox
    resolves it to the filesystem path ``<GOLDEN_REPOS_ROOT>/<alias>``. These
    entrypoints are handed the ``-global``-suffixed REGISTRY alias, which would
    resolve to a non-existent path (``.../<alias>-global``) → a generic failed
    build instead of a clean typed skip. Normalize to bare before funnelling, or
    every golden repo silently loses SCIP under ``scip_backend='delegated'``.

    This is GOLDEN entrypoints only — the activated (per-user) entrypoint passes
    ``activated_repo=True`` and does NOT normalize (an activated alias lives in a
    different namespace), so it calls :func:`funnel_scip_generation` directly.
    """
    bare = alias.removesuffix("-global")
    return funnel_scip_generation(alias=bare, repo_root=Path(repo_root), config=config)


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
