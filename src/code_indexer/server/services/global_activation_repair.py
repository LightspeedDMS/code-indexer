"""issue-64616: activate golden repos that are registered but not global.

A golden repo is queryable as ``<alias>-global`` only once its global
activation has run: an alias pointer plus a ``global_repos`` row. The add
does both right after it inserts the ``golden_repos_metadata`` row, so the
two exist together -- except for rows another writer inserted. The issue-65484
registry reconcile backfills a row for every indexed clone with no row, and
that clone is also every add still in flight (the add builds the index
BEFORE it inserts its row) and every in-place clone a removal left for its
owner. The backfill inserted the row; the add then failed on the duplicate
key before reaching its activation, or there was no add at all. Either way
the row stayed with no ``-global`` entry: registered, but never queryable.

This pass heals that shape and is the activation step for rows the backfill
adds from now on:

* candidates: golden rows (not ``local://``: those live on one pod) with no
  ``<alias>-global`` registry row;
* index: the store's current published version is reused; otherwise the
  index already on disk is published (under the local store: used in place).
  Nothing is ever re-indexed. A row with neither is reported, loudly;
* activation: ``GoldenRepoManager._activate_globally``, the add's own path,
  followed by the add's cidx-meta description hook (fail-soft).

Safe across racing pods. Activation is not idempotent under contention: two
pods activating one alias race the index store's compare-and-swap, and the
loser's failure cleanup deletes the winner's pointer and row. So the whole
pass runs under ONE cluster-wide single-flight claim
(``register_job_if_no_conflict``); a pod that loses the claim skips the cycle.
Each candidate is re-read from the registry before it is activated, and any
alias with an add or removal in flight is left to that job.

Stale alias pointers (``<name>-global`` with neither a golden row nor a
registry row) are reported every cycle. Deleting them is opt-in
(``CIDX_STALE_GLOBAL_ALIAS_CLEANUP_ENABLED=true``) and guarded: an
authoritative re-read must confirm both rows absent, no add or removal may be
in flight, and the pointer must be older than ``STALE_ALIAS_MIN_AGE_SECONDS``.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Set, Tuple

from code_indexer.server.services.job_tracker import DuplicateJobError
from code_indexer.server.utils.index_command_layout import is_pod_local_repo

if TYPE_CHECKING:
    from code_indexer.global_repos.alias_manager import AliasManager
    from code_indexer.global_repos.global_activation import GlobalActivator
    from code_indexer.server.repositories.golden_repo_manager import (
        GoldenRepoManager,
    )

logger = logging.getLogger(__name__)

GLOBAL_SUFFIX = "-global"

#: Job types whose in-flight run owns an alias's registration: the reconcile
#: neither backfills nor activates an alias while one of them is active.
REGISTRY_LIFECYCLE_OPERATIONS = (
    "add_golden_repo",
    "remove_golden_repo",
    "rename_golden_repo",
)

#: The cluster-wide single-flight claim for one repair pass.
REPAIR_OPERATION_TYPE = "golden_global_activation_repair"
REPAIR_SENTINEL_ALIAS = "__golden_global_activation_repair__"
REPAIR_SUBMITTER = "system-reconcile"

#: Activations per pass. Each one may publish an index (an upload, no
#: embedding), so a large backlog drains over several reconcile cycles.
MAX_PER_CYCLE_ENV = "CIDX_GLOBAL_ACTIVATION_REPAIR_MAX_PER_CYCLE"
DEFAULT_MAX_PER_CYCLE = 50

STALE_ALIAS_CLEANUP_ENV = "CIDX_STALE_GLOBAL_ALIAS_CLEANUP_ENABLED"
#: A pointer younger than this may belong to an activation or a removal that
#: is still running; it is never treated as stale.
STALE_ALIAS_MIN_AGE_SECONDS = 3600


#: Aliases whose activation raised on this pod; they are tried after every
#: other candidate next cycle. Process-local by design: it only orders work.
_FAILED_LAST_CYCLE: Set[str] = set()


class NoIndexError(RuntimeError):
    """The repo has neither a published index version nor one on disk."""


@dataclass
class ActivationRepairResult:
    """What one repair pass found and did."""

    candidates: List[str] = field(default_factory=list)
    activated: List[str] = field(default_factory=list)
    published: List[str] = field(default_factory=list)
    skipped_no_index: List[str] = field(default_factory=list)
    skipped_in_flight: List[str] = field(default_factory=list)
    deferred: List[str] = field(default_factory=list)
    failed: Dict[str, str] = field(default_factory=dict)
    stale_aliases: List[str] = field(default_factory=list)
    stale_aliases_removed: List[str] = field(default_factory=list)
    single_flight_skipped: bool = False

    def counts(self) -> Dict[str, int]:
        """The counts merged into the registry reconcile's result."""
        return {
            "activation_candidates": len(self.candidates),
            "activated_global": len(self.activated),
            "activation_published": len(self.published),
            "activation_skipped_no_index": len(self.skipped_no_index),
            "activation_skipped_in_flight": len(self.skipped_in_flight),
            "activation_deferred": len(self.deferred),
            "activation_failed": len(self.failed),
            "stale_global_aliases": len(self.stale_aliases),
            "stale_global_aliases_removed": len(self.stale_aliases_removed),
            "activation_single_flight_skipped": int(self.single_flight_skipped),
        }


def lifecycle_job_in_flight(job_tracker: Optional[Any], alias: str) -> bool:
    """True when an add, removal or rename of *alias* is pending or running
    anywhere in the cluster. No tracker (solo/CLI): nothing else can be running.
    Raises when the job store cannot be read."""
    if job_tracker is None:
        return False
    return any(
        job_tracker.active_job_id(operation, alias)
        for operation in REGISTRY_LIFECYCLE_OPERATIONS
    )


def max_per_cycle() -> int:
    """``CIDX_GLOBAL_ACTIVATION_REPAIR_MAX_PER_CYCLE``; the default when unset
    or not a positive integer (logged)."""
    raw = os.environ.get(MAX_PER_CYCLE_ENV)
    if raw is None:
        return DEFAULT_MAX_PER_CYCLE
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        logger.warning(
            "%s=%r is not a positive integer; using %d",
            MAX_PER_CYCLE_ENV,
            raw,
            DEFAULT_MAX_PER_CYCLE,
        )
        return DEFAULT_MAX_PER_CYCLE
    return value


def stale_alias_cleanup_enabled() -> bool:
    """``CIDX_STALE_GLOBAL_ALIAS_CLEANUP_ENABLED`` (default off: report only)."""
    return os.environ.get(STALE_ALIAS_CLEANUP_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def find_activation_candidates(
    rows: List[Dict[str, Any]], global_alias_names: Set[str]
) -> List[Dict[str, Any]]:
    """Golden rows with no ``<alias>-global`` registry row, sorted by alias.
    ``local://`` rows are left out (their index is on one pod only), as are
    rows missing a clone path or URL (nothing to activate from)."""
    return sorted(
        (
            row
            for row in rows
            if row.get("alias")
            and row.get("clone_path")
            and row.get("repo_url")
            and not is_pod_local_repo(row.get("repo_url"), row.get("alias"))
            and f"{row['alias']}{GLOBAL_SUFFIX}" not in global_alias_names
        ),
        key=lambda row: str(row["alias"]),
    )


def resolve_index_fields(
    manager: "GoldenRepoManager", clone_path: str
) -> Tuple[Optional[Dict[str, str]], bool]:
    """The alias-pointer fields recording *clone_path*'s index, and whether
    this call published it.

    Delegated store: the store's current version when there is one (nothing
    uploaded), else the on-disk index is published. Local store: the on-disk
    index is read in place (no fields). Raises ``NoIndexError`` when there is
    no index to point at -- the repo would need a re-index, which this pass
    never starts.
    """
    from code_indexer.server.routers.repository_health import detect_semantic_index
    from code_indexer.server.startup.index_store_wiring import server_index_store
    from code_indexer.utils.index_store import (
        index_store_alias,
        published_index_fields,
    )

    store = server_index_store()
    if not store.is_local:
        store_alias = index_store_alias(Path(clone_path).name)
        current_version_as = getattr(store, "current_version_as", None)
        current = current_version_as(store_alias) if current_version_as else None
        if current:
            return published_index_fields(store_alias, current), False
    if not detect_semantic_index(store.index_root(clone_path) / "index"):
        raise NoIndexError(f"no semantic index published or on disk for {clone_path}")
    if store.is_local:
        return None, False
    return manager._publish_index(clone_path, clone_path), True


def _registry_alias_names(activator: "GlobalActivator") -> Set[str]:
    """Every ``alias_name`` in the global registry. Raises on a read failure:
    an empty answer would make EVERY golden repo a candidate."""
    return {
        str(entry["alias_name"])
        for entry in activator.registry.list_global_repos()
        if entry.get("alias_name")
    }


def _claim(job_tracker: Optional[Any]) -> Optional[str]:
    """Claim the cluster-wide single-flight; its job id (None: no tracker).
    Raises ``DuplicateJobError`` when another pod holds it."""
    if job_tracker is None:
        return None
    job_id = uuid.uuid4().hex
    job_tracker.register_job_if_no_conflict(
        job_id=job_id,
        operation_type=REPAIR_OPERATION_TYPE,
        username=REPAIR_SUBMITTER,
        repo_alias=REPAIR_SENTINEL_ALIAS,
    )
    return job_id


def _activate_one(
    manager: "GoldenRepoManager",
    activator: "GlobalActivator",
    row: Dict[str, Any],
    result: ActivationRepairResult,
) -> bool:
    """Activate one candidate, recording the outcome; never raises.

    Returns whether it spent the cycle's budget: True once an activation was
    attempted (it may publish an index). A repo that was skipped -- already
    activated, add or removal in flight, no index -- costs nothing, so it can
    never starve the repos behind it."""
    alias = str(row["alias"])
    clone_path = str(row["clone_path"])
    try:
        if activator.registry.get_global_repo(f"{alias}{GLOBAL_SUFFIX}") is not None:
            return False  # activated since the listing (an add finished)
        if lifecycle_job_in_flight(manager.job_tracker, alias):
            result.skipped_in_flight.append(alias)
            return False
        fields, published_now = resolve_index_fields(manager, clone_path)
        repo_url = manager._strip_credentials_if_delegated(str(row["repo_url"]))
        manager._activate_globally(
            alias=alias,
            repo_url=repo_url,
            clone_path=clone_path,
            enable_temporal=bool(row.get("enable_temporal") or False),
            temporal_options=row.get("temporal_options"),
            published=fields,
        )
    except NoIndexError as exc:
        result.skipped_no_index.append(alias)
        logger.error("issue-64616: '%s' cannot be activated globally: %s", alias, exc)
        return False
    except Exception as exc:
        result.failed[alias] = str(exc)
        _FAILED_LAST_CYCLE.add(alias)
        logger.error("issue-64616: global activation of '%s' failed: %s", alias, exc)
        return True
    _FAILED_LAST_CYCLE.discard(alias)
    if published_now:
        result.published.append(alias)
    result.activated.append(alias)
    # The add's next step after activation: the cidx-meta description. It
    # never raises (a failure is logged; the activation stands).
    manager._run_meta_description_hook(alias, repo_url, clone_path)
    logger.warning(
        "issue-64616: activated golden repo '%s' globally as '%s%s' from its "
        "existing index (%s)",
        alias,
        alias,
        GLOBAL_SUFFIX,
        "published now" if published_now else "already published or local",
    )
    return True


def _pointer_age_seconds(document: Mapping[str, Any], now: datetime) -> float:
    """Seconds since the pointer was last written (0 when undatable, which
    keeps it out of the cleanup)."""
    stamp = document.get("last_refresh") or document.get("created_at")
    try:
        written = datetime.fromisoformat(str(stamp))
    except ValueError:
        return 0.0
    if written.tzinfo is None:
        written = written.replace(tzinfo=timezone.utc)
    return (now - written).total_seconds()


def find_stale_aliases(
    alias_records: Mapping[str, Mapping[str, Any]],
    golden_aliases: Set[str],
    global_alias_names: Set[str],
) -> List[str]:
    """``<name>-global`` pointers with neither a golden row ``<name>`` nor a
    registry row, sorted."""
    return sorted(
        name
        for name in alias_records
        if name.endswith(GLOBAL_SUFFIX)
        and name not in global_alias_names
        and name[: -len(GLOBAL_SUFFIX)] not in golden_aliases
    )


def _remove_stale_alias(
    manager: "GoldenRepoManager",
    activator: "GlobalActivator",
    alias_manager: "AliasManager",
    name: str,
    document: Mapping[str, Any],
    result: ActivationRepairResult,
) -> None:
    """Delete one stale pointer once every guard holds; never raises."""
    repo_name = name[: -len(GLOBAL_SUFFIX)]
    try:
        if _pointer_age_seconds(document, datetime.now(timezone.utc)) < (
            STALE_ALIAS_MIN_AGE_SECONDS
        ):
            return
        if manager._resolve_golden_repo_authoritative(repo_name) is not None:
            return
        if activator.registry.get_global_repo(name) is not None:
            return
        if lifecycle_job_in_flight(manager.job_tracker, repo_name):
            return
        alias_manager.delete_alias(name)
        if alias_manager.alias_exists(name):
            raise RuntimeError("the pointer is still there after the delete")
    except Exception as exc:
        result.failed[name] = str(exc)
        logger.error("issue-64616: could not remove stale pointer '%s': %s", name, exc)
        return
    result.stale_aliases_removed.append(name)
    logger.warning(
        "issue-64616: removed stale global alias pointer '%s' (no golden row, no "
        "registry row)",
        name,
    )


def _sweep_stale_aliases(
    manager: "GoldenRepoManager",
    activator: "GlobalActivator",
    records: Mapping[str, Mapping[str, Any]],
    result: ActivationRepairResult,
) -> None:
    """Report ``result.stale_aliases``; delete them only when enabled."""
    if not result.stale_aliases:
        return
    if not stale_alias_cleanup_enabled():
        logger.warning(
            "issue-64616: %d global alias pointer(s) have no golden row and no "
            "registry row: %s. Set %s=true to remove them.",
            len(result.stale_aliases),
            ",".join(result.stale_aliases),
            STALE_ALIAS_CLEANUP_ENV,
        )
        return
    for name in result.stale_aliases:
        _remove_stale_alias(
            manager, activator, activator.alias_manager, name, records[name], result
        )


def _run_pass(
    manager: "GoldenRepoManager",
    activator: "GlobalActivator",
    candidates: List[Dict[str, Any]],
    records: Mapping[str, Mapping[str, Any]],
    result: ActivationRepairResult,
) -> None:
    """Attempt up to ``max_per_cycle()`` activations, then sweep pointers.

    Only an attempt spends the budget (see ``_activate_one``), and repos whose
    activation failed in an earlier cycle on this pod go last, so neither a
    block of unindexed repos nor one that keeps failing holds back the rest."""
    limit = max_per_cycle()
    ordered = sorted(
        candidates, key=lambda row: str(row["alias"]) in _FAILED_LAST_CYCLE
    )
    attempts = 0
    for position, row in enumerate(ordered):
        if attempts >= limit:
            result.deferred = [str(r["alias"]) for r in ordered[position:]]
            break
        if _activate_one(manager, activator, row, result):
            attempts += 1
    _sweep_stale_aliases(manager, activator, records, result)


def _report(result: ActivationRepairResult) -> None:
    """Fail loud: anything left unqueryable is logged at ERROR, by name."""
    if result.skipped_no_index:
        logger.error(
            "issue-64616: %d golden repo(s) have no index to activate from and "
            "stay unqueryable until re-indexed: %s",
            len(result.skipped_no_index),
            ",".join(result.skipped_no_index),
        )
    if result.failed:
        logger.error(
            "issue-64616: global activation repair failures: %s",
            "; ".join(f"{name}: {error}" for name, error in result.failed.items()),
        )
    if result.deferred:
        logger.warning(
            "issue-64616: %d golden repo(s) left for the next cycle (%s=%d)",
            len(result.deferred),
            MAX_PER_CYCLE_ENV,
            max_per_cycle(),
        )


def repair_global_activation(
    manager: "GoldenRepoManager", rows: List[Dict[str, Any]]
) -> ActivationRepairResult:
    """Activate every golden row in *rows* that has no ``-global`` registry
    row, from the index it already has. See the module docstring.

    Raises when the global registry cannot be listed (never read as "nothing
    to repair"). Per-repo failures are recorded on the result and logged.
    """
    from code_indexer.global_repos.global_activation import GlobalActivator

    result = ActivationRepairResult()
    activator = GlobalActivator(manager.golden_repos_dir)
    global_alias_names = _registry_alias_names(activator)
    candidates = find_activation_candidates(rows, global_alias_names)
    result.candidates = [str(row["alias"]) for row in candidates]
    # Zero golden rows is indistinguishable from a failed read: no pointer is
    # called stale on that answer.
    records: Mapping[str, Mapping[str, Any]] = (
        activator.alias_manager.list_records() if rows else {}
    )
    result.stale_aliases = find_stale_aliases(
        records,
        {str(row["alias"]) for row in rows if row.get("alias")},
        global_alias_names,
    )
    if not candidates and not (result.stale_aliases and stale_alias_cleanup_enabled()):
        # Nothing to write: report any stale pointers without a claim, so a
        # report-only cycle leaves no job row behind.
        _sweep_stale_aliases(manager, activator, records, result)
        return result

    job_tracker = manager.job_tracker
    try:
        job_id = _claim(job_tracker)
    except DuplicateJobError:
        result.single_flight_skipped = True
        logger.info(
            "issue-64616: another pod is running the global activation repair; "
            "skipping this cycle"
        )
        return result

    try:
        _run_pass(manager, activator, candidates, records, result)
    except Exception as exc:
        if job_tracker is not None and job_id is not None:
            job_tracker.fail_job(job_id, error=str(exc))
        raise
    if job_tracker is not None and job_id is not None:
        job_tracker.complete_job(job_id, result=result.counts())
    _report(result)
    return result
