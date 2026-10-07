"""Per-run facts the runner derives after each child exits (pure or read-only)."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, FrozenSet, Mapping, Set

from index_inspector import parse_progress_json

_PROGRESS_TAIL_BYTES = 8192


def files_all_processed(out_log: Path) -> bool:
    """Finalization probe: the run's --progress-json reports every planned file done."""
    try:
        with open(out_log, "rb") as handle:
            handle.seek(max(0, out_log.stat().st_size - _PROGRESS_TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return False
    progress = parse_progress_json(tail)
    return (
        bool(progress.file_totals)
        and progress.file_totals[-1] > 0
        and progress.last_file_current == progress.file_totals[-1]
    )


def durability_facts(
    sent_counts: Mapping[str, int],
    durable_before: Set[str],
    durable_after: Set[str],
    interrupted: bool,
    held_keys: Set[str],
    pending_after: Set[str],
) -> Dict[str, FrozenSet[str]]:
    """Key facts for R-d, R-12, A1 and A2 (durable = in the store or in pending).

    In flight at a kill = sent during the run and not durable after the child
    exited, whether or not its response arrived (the pay-once definition: a
    received-but-unsaved response is in flight, never "unanswered").
    """
    return {
        "sent_durable_before": frozenset(k for k in sent_counts if k in durable_before),
        "inflight_keys": frozenset(k for k in sent_counts if k not in durable_after)
        if interrupted
        else frozenset(),
        "held_keys": frozenset(held_keys),
        "held_in_pending_after": frozenset(held_keys & pending_after),
    }


def content_bound(
    sent_counts: Mapping[str, int], disk_keys: Set[str], durable_before: Set[str]
) -> "tuple[int, FrozenSet[str]]":
    """R-a content facts: |K_r - D_r| and the sent keys outside K_r - D_r.

    K_r = chunk keys of the files on disk at the run's start; D_r = keys
    durable (store or pending) then.
    """
    owed = disk_keys - durable_before
    return len(owed), frozenset(k for k in sent_counts if k not in owed)


def unindexed_at_start(disk: Set[str], stored_paths: Set[str], stale: Set[str]) -> int:
    """The path R-a bound: files a correct recovery may plan.

    New paths (not stored; duplicate copies of stored content stay allowed,
    since an unregistered copy is legitimately planned once) plus stored
    paths whose content is stale (a git edit, or a branch switch that
    reverted the file).
    """
    return len((disk - stored_paths) | stale)


def imports_from(module_file: str, src: Path) -> bool:
    """The child's imported module lies inside the tree under test (a path
    check, never a string prefix: ``/x/src2`` is not inside ``/x/src``)."""
    return Path(module_file).resolve().is_relative_to(src.resolve())
