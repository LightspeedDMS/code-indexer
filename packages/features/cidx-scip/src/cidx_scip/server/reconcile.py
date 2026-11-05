"""the periodic SCIP backfill of golden repositories.

Moved out of the core's golden repo manager (``reconcile_scip_from_filesystem``)
and its lifespan loop in modular phase 4d: the ``scip`` feature contributes it
as a recurring background job (``feature.background_jobs()``), registered only
while ``CIDX_SCIP_AUTO_RECONCILE_ENABLED=true``. The scan, the cooldown marker
and the per-cycle bound are the core's shared golden-index backfill engine;
the SCIP-specific present/eligible/enqueue are :class:`ScipBackfillPolicy`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from cidx_scip.paths import SCIP_DB_FILENAME, scip_index_dir
from cidx_scip.server import _core

logger = logging.getLogger(__name__)

#: Opt-in switch (default off): each enqueue triggers a real delegated build.
ENABLED_ENV_VAR = "CIDX_SCIP_AUTO_RECONCILE_ENABLED"
INTERVAL_ENV_VAR = "CIDX_SCIP_RECONCILE_INTERVAL_SECONDS"
MAX_PER_CYCLE_ENV_VAR = "CIDX_SCIP_RECONCILE_MAX_PER_CYCLE"
LANGUAGES_ENV_VAR = "CIDX_SCIP_LANGUAGES"
COOLDOWN_ENV_VAR = "CIDX_SCIP_ATTEMPT_COOLDOWN_SECONDS"

DEFAULT_INTERVAL_SECONDS = 1800
DEFAULT_MAX_PER_CYCLE = 5
DEFAULT_LANGUAGES = "python,typescript,go"
DEFAULT_COOLDOWN_SECONDS = 86400

_COUNT_KEYS = (
    "scanned",
    "scip_present",
    "not_discoverable",
    "language_skipped",
    "cooldown_skipped",
    "enqueued",
    "errors",
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def reconcile_enabled() -> bool:
    """Whether the operator opted in (``CIDX_SCIP_AUTO_RECONCILE_ENABLED``)."""
    return os.getenv(ENABLED_ENV_VAR, "false").lower() == "true"


def reconcile_interval_seconds() -> int:
    """Seconds between two backfill cycles (``CIDX_SCIP_RECONCILE_INTERVAL_SECONDS``)."""
    return _env_int(INTERVAL_ENV_VAR, DEFAULT_INTERVAL_SECONDS)


def reconcile_max_per_cycle() -> int:
    """Builds enqueued per cycle at most (``CIDX_SCIP_RECONCILE_MAX_PER_CYCLE``)."""
    return _env_int(MAX_PER_CYCLE_ENV_VAR, DEFAULT_MAX_PER_CYCLE)


def scip_present(repo_dir: Path) -> bool:
    """True if the repo already holds the SCIP index the CURRENT discovery produces.

    a repo with a ROOT build file is indexed as a single root project
    whose build lands the TOP-LEVEL ``scip/index.scip.db`` -- so for those,
    presence REQUIRES that top-level DB. This stops a STALE per-subproject
    layout (nested DBs left by the old per-subproject discovery, with no root DB)
    from being counted "present" and never rebuilt. A repo with NO root build
    file is indexed per-subproject (nested by design), so any ``.scip.db`` under
    the scip dir counts.
    """
    from cidx_scip.server.repo_reads import repo_has_root_build_file

    scip_dir = scip_index_dir(repo_dir)
    if not scip_dir.is_dir():
        return False
    if repo_has_root_build_file(repo_dir):
        return (scip_dir / SCIP_DB_FILENAME).is_file()
    for _ in scip_dir.rglob("*.scip.db"):
        return True
    return False


def backfill_entries(golden_repo_manager: Any) -> Optional[List[Tuple[str, Path]]]:
    """The ``(alias, repo_path)`` pairs the SCIP backfill scans; None = the
    golden-repos directory.

    With a local content backend the directory IS the set of golden repos
    (each holds its clone). With a delegated one this pod holds only
    index-state dirs, so the registry is scanned instead, sorted by alias,
    with each repo's registered alias and clone path. ``local://`` repos
    (cidx-meta, uploads) are cidx's own files, never SCIP-indexed.
    """
    content_backend = getattr(golden_repo_manager, "_content_backend", None)
    if content_backend is None or content_backend.is_local:
        return None
    repos = sorted(
        golden_repo_manager.list_golden_repos(), key=lambda r: str(r.get("alias", ""))
    )
    return [
        (str(r["alias"]), Path(r["clone_path"]))
        for r in repos
        if r.get("alias")
        and r.get("clone_path")
        and not str(r.get("repo_url") or "").startswith("local://")
    ]


def reconcile_scip_from_filesystem(
    golden_repo_manager: Any, config: Any, max_enqueue: int = DEFAULT_MAX_PER_CYCLE
) -> Dict[str, int]:
    """backfill SCIP indexes for SCIP-eligible golden repos that have none.

    For each golden repo with no ``.scip.db``, run the SCIP project discovery
    (build-file based); when it finds a project in a SCIP-ENABLED language,
    enqueue a delegated SCIP build via the shared funnel (``funnel_golden_scip``).
    Once the build lands, the core's index-flag reconcile sets
    ``enable_scip=True`` on the next refresh.

    Conservative and opt-in, because each enqueue triggers a real build:
    gated by ``CIDX_SCIP_AUTO_RECONCILE_ENABLED``; language-gated by
    ``CIDX_SCIP_LANGUAGES`` (default ``python,typescript,go``); bounded to
    *max_enqueue* per cycle; a per-repo cooldown marker retries a failing build
    at most once per ``CIDX_SCIP_ATTEMPT_COOLDOWN_SECONDS``.

    Returns counts: scanned / scip_present / not_discoverable /
    language_skipped / cooldown_skipped / enqueued / errors.
    """
    counts = {key: 0 for key in _COUNT_KEYS}
    if not reconcile_enabled():
        return counts

    from cidx_scip.server.backfill_policy import ScipBackfillPolicy
    from cidx_scip.server.repo_reads import head_memo

    enabled_langs = {
        lang.strip().lower()
        for lang in os.getenv(LANGUAGES_ENV_VAR, DEFAULT_LANGUAGES).split(",")
        if lang.strip()
    }
    cooldown = _env_int(COOLDOWN_ENV_VAR, DEFAULT_COOLDOWN_SECONDS)
    policy = ScipBackfillPolicy(golden_repo_manager, enabled_langs, config)
    engine = _core.golden_index_backfill(str(golden_repo_manager.golden_repos_dir))
    # One HEAD per repo per cycle: presence and freshness both ask it.
    with head_memo():
        result = engine.run_cycle(
            policy,
            max_enqueue=max_enqueue,
            cooldown_seconds=cooldown,
            entries=backfill_entries(golden_repo_manager),
        )
    counts["scanned"] = result.scanned
    counts["scip_present"] = result.present
    counts["not_discoverable"] = policy.not_discoverable
    counts["language_skipped"] = policy.language_skipped
    counts["cooldown_skipped"] = result.cooldown_skipped
    counts["enqueued"] = result.enqueued
    counts["errors"] = result.errors
    logger.info("SCIP reconcile from filesystem complete: %s", counts)
    return counts
