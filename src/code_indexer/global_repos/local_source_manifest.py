"""Identity-based change detection for a non-git (``local://``) repository.

Such a repository is refreshed only on a trigger. Each refresh that indexes
records, BEFORE its index child lists the source, a manifest of every
non-hidden source file (relative path -> ``(size, mtime_ns)``) inside the
source's ``.code-indexer/``; the snapshot that run publishes inherits it, so
the manifest of the PUBLISHED snapshot describes exactly what that index
was built from. A later check compares the live source with that manifest by
equality: any added, removed or re-stamped file is a change. Equality (not
"newer than") is immune to skewed mtimes and sees deletions; a missing,
unreadable or invalid manifest is an unknown baseline, which is a change.

Racily clean entries: a file whose recorded mtime lies within one timestamp
tick of the moment the listing started can be rewritten after it was stat'ed
without its (size, mtime) changing (coarse filesystem clocks, 1 s export
granularity). Such entries are recorded as racy and count as changed at the
next check; that refresh records them again, later, and converges. The tick
is judged on the filesystem's own clock: a file created just before listing.

Hidden files and directories (leading ``.``) are not repository content, and
symlinked directories are not followed (the indexer descends into neither).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

logger = logging.getLogger(__name__)

MANIFEST_RELATIVE_PATH = Path(".code-indexer") / "refresh_source_manifest.json"
_MANIFEST_FORMAT = 2
#: Covers 1 s timestamp granularity plus the coarse kernel clock tick.
_RACY_MARGIN_NS = 2_000_000_000

_UNREADABLE_SHOWN = 5

#: (size in bytes, mtime in nanoseconds)
FileIdentity = Tuple[int, int]


class IncompleteSourceListingError(OSError):
    """The source could not be listed completely; nothing was recorded."""


@dataclass(frozen=True)
class SourceListing:
    files: Dict[str, FileIdentity]
    #: Relative paths (``.`` for the root) that could not be listed or stat'ed.
    unreadable: Tuple[str, ...]


@dataclass(frozen=True)
class RecordedManifest:
    files: Dict[str, FileIdentity]
    #: Entries whose metadata could not prove them unchanged when recorded.
    racy: FrozenSet[str]


def list_source(root: str) -> SourceListing:
    """Every non-hidden file under *root* with its identity."""
    files: Dict[str, FileIdentity] = {}
    unreadable: List[str] = []
    # ^ invariant terminates: symlinked dirs are never queued, so each real directory is listed once
    pending: List[str] = [""]
    while pending:
        rel_dir = pending.pop()
        try:
            with os.scandir(os.path.join(root, rel_dir)) as entries:
                listed = list(entries)
        except OSError as exc:
            unreadable.append(rel_dir or ".")
            logger.warning(f"Cannot list {os.path.join(root, rel_dir)}: {exc}")
            continue
        for entry in listed:
            if entry.name.startswith("."):
                continue
            rel = os.path.join(rel_dir, entry.name) if rel_dir else entry.name
            try:
                if entry.is_dir():
                    if not entry.is_symlink():
                        pending.append(rel)
                    continue
                stat = entry.stat()
            except OSError as exc:
                unreadable.append(rel)
                logger.warning(f"Cannot stat {entry.path}: {exc}")
                continue
            files[rel] = (stat.st_size, stat.st_mtime_ns)
    return SourceListing(files, tuple(unreadable))


def _valid_identity(identity: object) -> bool:
    return (
        isinstance(identity, list)
        and len(identity) == 2
        and all(type(v) is int for v in identity)
    )


def read_manifest(snapshot_root: str) -> Tuple[Optional[RecordedManifest], str]:
    """The manifest a published snapshot carries, or ``(None, why)``."""
    path = Path(snapshot_root) / MANIFEST_RELATIVE_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"published snapshot {snapshot_root} has no source manifest"
    except (OSError, ValueError) as exc:
        return None, f"source manifest {path} is unreadable ({type(exc).__name__})"
    if not isinstance(raw, dict) or raw.get("format") != _MANIFEST_FORMAT:
        return None, f"source manifest {path} has an unknown format"
    entries = raw.get("files")
    racy = raw.get("racy")
    if not isinstance(entries, dict) or not isinstance(racy, list):
        return None, f"source manifest {path} has no file table"
    if not all(_valid_identity(identity) for identity in entries.values()) or not all(
        isinstance(rel, str) for rel in racy
    ):
        return None, f"source manifest {path} has an invalid entry"
    files = {rel: (identity[0], identity[1]) for rel, identity in entries.items()}
    return RecordedManifest(files, frozenset(racy)), ""


def source_change_reason(
    published_snapshot: Optional[str], source_root: str
) -> Optional[str]:
    """Why *source_root* differs from what *published_snapshot* indexed, or
    None when it holds exactly the files that snapshot's run listed.
    *published_snapshot* is None when the alias points at no versioned
    snapshot (nothing published yet)."""
    if published_snapshot is None:
        return "no versioned snapshot is published"
    manifest, why = read_manifest(published_snapshot)
    if manifest is None:
        return why
    if manifest.racy:
        return f"{len(manifest.racy)} file(s) were racily clean when recorded"
    listing = list_source(source_root)
    if listing.unreadable:
        return f"{len(listing.unreadable)} source entries could not be read"
    removed = sum(1 for rel in manifest.files if rel not in listing.files)
    if removed:
        return f"{removed} file(s) removed since the published snapshot"
    for rel, identity in listing.files.items():
        recorded = manifest.files.get(rel)
        if recorded is None:
            return f"{rel} added since the published snapshot"
        if recorded != identity:
            return f"{rel} changed since the published snapshot"
    return None


def record_source_manifest(source_root: str, published_snapshot: Optional[str]) -> bool:
    """Record the source as this run lists it (call BEFORE indexing) and
    return whether any file the published snapshot listed is gone, i.e. the
    run must remove gone paths. An unknown published baseline counts as
    gone. Raises OSError when the manifest cannot be written: a snapshot
    must never inherit an older run's manifest; raises
    IncompleteSourceListingError (writing nothing) when an entry could not
    be listed or stat'ed -- an unreadable file is never a deletion."""
    path = Path(source_root) / MANIFEST_RELATIVE_PATH
    temp = path.with_name(path.name + ".tmp")
    # ^ why a freshly created file: its mtime is the filesystem clock when listing starts
    temp.unlink(missing_ok=True)
    with open(temp, "x", encoding="utf-8") as probe:
        listing_started_ns = os.fstat(probe.fileno()).st_mtime_ns
    listing = list_source(source_root)
    if listing.unreadable:
        temp.unlink(missing_ok=True)
        shown = ", ".join(listing.unreadable[:_UNREADABLE_SHOWN])
        raise IncompleteSourceListingError(
            f"{len(listing.unreadable)} source entries of {source_root} could "
            f"not be read ({shown}); refresh stopped before indexing"
        )
    racy_from = listing_started_ns - _RACY_MARGIN_NS
    payload = {
        "format": _MANIFEST_FORMAT,
        "files": {rel: list(identity) for rel, identity in listing.files.items()},
        "racy": sorted(
            rel for rel, (_, mtime_ns) in listing.files.items() if mtime_ns >= racy_from
        ),
    }
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    if published_snapshot is None:
        return True
    published, why = read_manifest(published_snapshot)
    if published is None:
        logger.info(f"{why}; this run removes gone paths")
        return True
    return any(rel not in listing.files for rel in published.files)
