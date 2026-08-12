"""Shared keep-last-N versioned-snapshot retention primitive.

Extracted from `RefreshScheduler._enforce_retention` (Bug #1084 Phase A6)
as a standalone, alias-agnostic function so it can be reused by callers
other than `RefreshScheduler`'s own per-repo semantic-refresh loop.

Story #1457 MEDIUM #14 (2026-07-23 code review): temporal sister-location
aliases (`{repo_alias}-temporal-{embedder_slug}[-{quarter}]`, published
directly via `AliasManager.create_alias`/`swap_alias`) are structurally
invisible to `RefreshScheduler`'s per-repo enumeration loop, so they never
reached `_enforce_retention` before this module existed.
`discover_and_enforce_temporal_retention` closes that gap by discovering
temporal aliases directly from the alias directory and reusing
`enforce_snapshot_retention` per discovered alias.

Bug #1567 Gap 1 (adversarial review of the orphan-sweep reconciler,
`server/services/versioned_snapshot_reconciler.py`): the LIVE retention
path below used to carry the exact three defects that reconciler was
built to avoid --

  A. STRADDLED READ: `current_target` (caller-supplied) and
     `previous_path` (via a SEPARATE `AliasManager.get_previous_path`
     call) came from TWO SEPARATE opens of the alias pointer file, which
     can straddle a concurrent `swap_alias` and land on different
     generations.
  B. WRITE-MODE REDIRECTION: `AliasManager.read_alias` silently redirects
     reads for a `-global` alias with an active write session to its
     write-mode SOURCE path (a live working directory, not a versioned
     snapshot) -- if ever used to resolve the live snapshot, the real
     target/previous would be excluded from the keep set.
  C. NO ts_live ANCHOR: a keep-set of {target, previous, N-newest-on-disk}
     protects the live/in-flight snapshot only by coincidence. Enough
     crash-orphans (each newer than target) consume the "N newest" slots
     and the true live target -- or an in-flight build not yet swapped
     in -- falls outside the window and becomes a delete candidate.

The fix makes this module the SINGLE shared source of truth for the
ts_live-anchored supersession predicate: `read_pointer_target_and_previous`
(one atomic open+json.load, closing hole A), never
`read_alias`/`get_previous_path` (closing hole B), and
`compute_snapshot_deletion_candidates` (anchoring on `ts_live`, closing
hole C). `versioned_snapshot_reconciler.py` imports these SAME functions
rather than reimplementing them -- two independent implementations of
"what is superseded" would drift, and drift is a deletion.

Min-absolute-age note: `compute_snapshot_deletion_candidates`'s age floor
compares `now` against EACH CANDIDATE's OWN creation timestamp (`ts`), not
`ts_live` -- it is a defense against treating a just-created snapshot as
deletable the instant it becomes superseded, independent of
`CleanupManager`'s own, separate, schedule-time retention-age gate
downstream. This mirrors the reconciler's pre-existing, unmodified
algorithm verbatim.

Path-rooting note (cow-daemon): `parse_live_timestamp` is deliberately
ROOT-AGNOSTIC -- it recognizes the canonical `.versioned/{ns}/v_<ts>`
SHAPE regardless of which literal directory the snapshot lives under.
The versioned root is NOT always `golden_repos_dir`: a cow-daemon/
FlexClone-backed `VersionedSnapshotManager` roots snapshots at the clone
backend's own mount point instead (`snapshot_paths.py`'s own documented
convention -- "snapshot_root is golden_repos_dir (local) or mount_point
(cow-daemon/ONTAP)"). A literal `target.parent == golden_repos_dir /
".versioned" / bare_namespace` comparison would ALWAYS fail-closed for
cow-daemon deployments -- safe, but permanently inert on exactly the
fleet this bug was filed against. The structural check below is a
strict, backend-agnostic generalization of that same literal comparison
(any path that would have matched it also matches the structural one),
so it is not a weakening -- a non-`.versioned`-shaped path (e.g. the
master clone) still fails closed exactly as before.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

from code_indexer.server.services.config_service import get_config_service

if TYPE_CHECKING:
    from .alias_manager import AliasManager
    from .cleanup_manager import CleanupManager
    from code_indexer.server.storage.shared.snapshot_manager import (
        VersionedSnapshotManager,
    )

logger = logging.getLogger(__name__)

#: Fallback keep-last-N when the configured value is missing or invalid.
DEFAULT_SNAPSHOT_RETENTION_KEEP_LAST = 3

#: Companion to CleanupManager.MIN_RETENTION_AGE_SECONDS and
#: versioned_snapshot_reconciler.DEFAULT_MIN_ABSOLUTE_AGE_SECONDS (matched
#: by convention/review, not by import -- no runtime layering dependency).
DEFAULT_MIN_ABSOLUTE_AGE_SECONDS = 900.0

#: Matches a ``v_<unix_ts>`` snapshot leaf name and captures the timestamp.
_SNAPSHOT_NAME_RE = re.compile(r"^v_(\d+)$")


def resolve_retention_keep_last() -> int:
    """Return the configured keep-last-N, falling back to the safe default.

    A value < 1 would schedule EVERY snapshot for deletion (including the
    live one once it ages out), so non-positive / unreadable values fall
    back to DEFAULT_SNAPSHOT_RETENTION_KEEP_LAST. Any read/parse failure is
    logged (not silently swallowed) before falling back.
    """
    try:
        keep = int(get_config_service().get_config().snapshot_retention_keep_last)
    except Exception as exc:
        logger.warning(
            "[retention] failed to read snapshot_retention_keep_last "
            "(falling back to default=%d): %s: %s",
            DEFAULT_SNAPSHOT_RETENTION_KEEP_LAST,
            type(exc).__name__,
            exc,
        )
        return DEFAULT_SNAPSHOT_RETENTION_KEEP_LAST
    if keep < 1:
        logger.warning(
            "[retention] snapshot_retention_keep_last=%r is non-positive "
            "(falling back to default=%d)",
            keep,
            DEFAULT_SNAPSHOT_RETENTION_KEEP_LAST,
        )
        return DEFAULT_SNAPSHOT_RETENTION_KEEP_LAST
    return keep


# ---------------------------------------------------------------------------
# Canonical pointer-reading + supersession predicate -- the SINGLE shared
# implementation used by BOTH this module's enforce_snapshot_retention
# (live retention, every refresh) AND
# server/services/versioned_snapshot_reconciler.py (startup/periodic
# orphan sweep). See module docstring for the full rationale.
# ---------------------------------------------------------------------------


def read_pointer_target_and_previous(
    alias_file: Path,
) -> Optional[Tuple[str, Optional[str]]]:
    """One atomic open+json.load of an alias pointer file; target_path AND
    previous_path from the SAME parsed dict. None on any read/parse
    failure, a non-object payload, or a missing target_path. NEVER
    AliasManager.read_alias/get_previous_path (module docstring holes
    A/B)."""
    try:
        with open(alias_file, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    target_path = data.get("target_path")
    if not isinstance(target_path, str) or not target_path:
        return None
    previous_path = data.get("previous_path")
    if not isinstance(previous_path, str) or not previous_path:
        previous_path = None
    return target_path, previous_path


def collect_all_pointers(aliases_dir: Path) -> Dict[str, Tuple[str, Optional[str]]]:
    """Read EVERY ``*.json`` in *aliases_dir* -- cross-alias reference
    union. Unreadable/unparseable files are simply absent -- never raise.
    Raises OSError only if *aliases_dir* itself cannot be listed."""
    pointers: Dict[str, Tuple[str, Optional[str]]] = {}
    for alias_file in aliases_dir.glob("*.json"):
        parsed = read_pointer_target_and_previous(alias_file)
        if parsed is not None:
            pointers[alias_file.stem] = parsed
    return pointers


def globally_referenced_paths(
    pointers: Dict[str, Tuple[str, Optional[str]]],
) -> Set[str]:
    """Every target_path/previous_path across every pointer read."""
    referenced: Set[str] = set()
    for target_path, previous_path in pointers.values():
        referenced.add(target_path)
        if previous_path:
            referenced.add(previous_path)
    return referenced


def resolve_governing_pointer(
    bare_namespace: str, pointers: Dict[str, Tuple[str, Optional[str]]]
) -> Optional[Tuple[str, Optional[str]]]:
    """Prefers `-global`, falls back to bare (temporal aliases have no
    `-global` suffix). None if neither present -- fail-closed."""
    for alias_name in (f"{bare_namespace}-global", bare_namespace):
        pointer = pointers.get(alias_name)
        if pointer is not None:
            return pointer
    return None


def parse_live_timestamp(target_path: str) -> Optional[int]:
    """*target_path* must be structurally a genuine v_<ts> snapshot: its
    grandparent directory must be the literal ".versioned" segment, and
    its own parent (the namespace directory) must not itself be a
    reserved name (mirrors snapshot_paths.is_versioned_snapshot's
    canonical-clause guard). ROOT-AGNOSTIC by design -- see module
    docstring's path-rooting note.

    Deliberately takes NO namespace string to compare against: a clone
    backend may sanitize the namespace component on disk (e.g.
    CowDaemonBackend._sanitize_identifier maps disallowed characters to
    underscores), so an independently-computed `bare_namespace` string
    can legitimately differ from the real on-disk directory name for the
    SAME alias. Comparing against it would incorrectly fail-closed for
    every such repo. Callers that need "is this candidate co-located with
    the live target" instead compare directories directly (see
    `_filter_deletion_candidates`), never by re-deriving a namespace
    string. None (never guessed) if not shaped this way.

    NOTE: this is a signature change from the original ``(target_path,
    bare_namespace)`` -- the ONLY call sites are internal to this module
    and to `versioned_snapshot_reconciler.py` (both maintained together;
    no test imports `parse_live_timestamp` directly)."""
    target = Path(target_path)
    if target.parent.parent.name != ".versioned":
        return None
    if target.parent.name in (".versioned", "activated-repos"):
        return None
    match = _SNAPSHOT_NAME_RE.match(target.name)
    return int(match.group(1)) if match else None


def _protected_snapshot_paths(
    older: List[Tuple[str, int]],
    live_or_newer_paths: Set[str],
    referenced_paths: Set[str],
    all_paths_here: Set[str],
    keep_last: int,
) -> Set[str]:
    """Union of the three protection sources for one namespace."""
    protected: Set[str] = set(live_or_newer_paths)
    protected |= referenced_paths & all_paths_here
    keep_from_history = max(keep_last - 1, 0)
    if keep_from_history > 0:
        for path, _ts in older[-keep_from_history:]:
            protected.add(path)
    return protected


def _filter_deletion_candidates(
    older: List[Tuple[str, int]],
    protected: Set[str],
    target_namespace_dir: Path,
    min_absolute_age_seconds: float,
    now: float,
) -> List[str]:
    """Age-gate + structural re-confirmation pass over the older/unprotected
    set -- split out purely to keep `compute_snapshot_deletion_candidates`
    short. Re-confirmation compares each candidate's OWN parent directory
    against *target_namespace_dir* (derived from the trusted target_path,
    never a re-derived namespace string -- avoids the backend-sanitization
    mismatch `parse_live_timestamp`'s docstring describes)."""
    candidates: List[str] = []
    for path, ts in older:
        if path in protected:
            continue
        if (now - ts) < min_absolute_age_seconds:
            continue
        if Path(path).parent != target_namespace_dir:
            continue
        candidates.append(path)
    return candidates


def compute_snapshot_deletion_candidates(
    bare_namespace: str,
    *,
    golden_repos_dir: Path,
    snapshots: List[Tuple[str, int]],
    target_path: str,
    referenced_paths: Set[str],
    keep_last: int,
    min_absolute_age_seconds: float = DEFAULT_MIN_ABSOLUTE_AGE_SECONDS,
    now: Optional[float] = None,
) -> List[str]:
    """THE canonical supersession predicate (module docstring). keep_last
    < 1 -> 1 (live alone, never "keep nothing"). Negative
    min_absolute_age_seconds clamps to 0.0.

    ``golden_repos_dir`` and ``bare_namespace`` are accepted for
    signature/API stability (existing callers pass them) but are not used
    to pick the expected root/namespace -- `parse_live_timestamp`'s
    root-agnostic shape check, plus comparing candidates against
    *target_path*'s own parent directory, are the sole authority (module
    docstring path-rooting note and `parse_live_timestamp`'s
    sanitization-mismatch note).
    """
    del golden_repos_dir, bare_namespace  # signature-compat only; see docstring
    if now is None:
        now = time.time()
    keep_last = max(keep_last, 1)
    min_absolute_age_seconds = max(min_absolute_age_seconds, 0.0)

    ts_live = parse_live_timestamp(target_path)
    if ts_live is None:
        return []
    target_namespace_dir = Path(target_path).parent

    older = sorted(
        ((p, ts) for p, ts in snapshots if ts < ts_live), key=lambda item: item[1]
    )
    live_or_newer_paths = {p for p, ts in snapshots if ts >= ts_live}
    all_paths_here = {p for p, _ts in snapshots}

    protected = _protected_snapshot_paths(
        older, live_or_newer_paths, referenced_paths, all_paths_here, keep_last
    )
    return _filter_deletion_candidates(
        older, protected, target_namespace_dir, min_absolute_age_seconds, now
    )


# ---------------------------------------------------------------------------
# Live retention path -- runs on every refresh.
# ---------------------------------------------------------------------------


def _resolve_live_pointer(
    alias_name: str, current_target: str, alias_manager: "AliasManager"
) -> Tuple[str, Set[str]]:
    """One atomic pointer read (Bug #1567 Gap 1 holes A/B) -> (target_path,
    referenced_paths). Falls back to *current_target* only if the pointer
    file itself cannot be read at all."""
    alias_file = alias_manager.aliases_dir / f"{alias_name}.json"
    pointer = read_pointer_target_and_previous(alias_file)
    target_path = pointer[0] if pointer is not None else current_target
    previous_path = pointer[1] if pointer is not None else None
    referenced_paths: Set[str] = {target_path}
    if previous_path:
        referenced_paths.add(previous_path)
    return target_path, referenced_paths


def enforce_snapshot_retention(
    alias_name: str,
    current_target: str,
    *,
    snapshot_manager: Optional["VersionedSnapshotManager"],
    alias_manager: "AliasManager",
    cleanup_manager: "CleanupManager",
    retention_keep_last: Optional[int] = None,
) -> None:
    """Schedule deletion of superseded snapshots beyond keep-last-N, using
    the SAME ts_live-anchored predicate the reconciler sweep uses (module
    docstring). Non-fatal: any failure is logged and swallowed so a
    caller's refresh/publish never fails on retention.

    Args:
        retention_keep_last: Pre-resolved keep-last-N (callers with their
            own config-resolution path pass it explicitly); omitted, falls
            back to `resolve_retention_keep_last()`.
    """
    if snapshot_manager is None:
        return
    try:
        keep_last = (
            retention_keep_last
            if retention_keep_last is not None
            else resolve_retention_keep_last()
        )
        snapshots = snapshot_manager.list_snapshots(alias_name)
        if len(snapshots) <= keep_last:
            return

        bare_namespace = alias_name.removesuffix("-global")
        target_path, referenced_paths = _resolve_live_pointer(
            alias_name, current_target, alias_manager
        )
        candidates = compute_snapshot_deletion_candidates(
            bare_namespace,
            golden_repos_dir=Path(alias_manager.aliases_dir).parent,
            snapshots=snapshots,
            target_path=target_path,
            referenced_paths=referenced_paths,
            keep_last=keep_last,
        )

        for path in candidates:
            if not snapshot_manager.is_versioned_snapshot(path):
                continue
            logger.info(
                f"[retention] Scheduling cleanup of superseded snapshot "
                f"{path} (keep_last={keep_last}) for {alias_name}"
            )
            cleanup_manager.schedule_cleanup(path)
    except Exception as exc:
        logger.warning(
            f"[retention] keep-last-N enforcement failed for {alias_name} "
            f"(non-fatal): {type(exc).__name__}: {exc}"
        )


def discover_and_enforce_temporal_retention(
    repo_alias: str,
    *,
    snapshot_manager: Optional["VersionedSnapshotManager"],
    alias_manager: "AliasManager",
    cleanup_manager: "CleanupManager",
) -> None:
    """Discover and retention-sweep every temporal sister alias for ONE
    golden repo (`{repo_alias}-temporal-*` alias files).

    Args:
        repo_alias: The golden repo's BARE alias (no "-global" suffix).
        snapshot_manager: None is a no-op (mirrors enforce_snapshot_retention).
    """
    if snapshot_manager is None:
        return
    prefix = f"{repo_alias}-temporal-"
    try:
        alias_files = sorted(alias_manager.aliases_dir.glob(f"{prefix}*.json"))
    except OSError as exc:
        logger.warning(
            "[temporal-retention] alias directory scan failed for %s "
            "(non-fatal): %s: %s",
            repo_alias,
            type(exc).__name__,
            exc,
        )
        return

    for alias_file in alias_files:
        temporal_alias_name = alias_file.stem
        # Bug #1567 Gap 1: raw pointer read, never AliasManager.read_alias.
        pointer = read_pointer_target_and_previous(alias_file)
        if pointer is None:
            continue
        enforce_snapshot_retention(
            temporal_alias_name,
            pointer[0],
            snapshot_manager=snapshot_manager,
            alias_manager=alias_manager,
            cleanup_manager=cleanup_manager,
        )
