"""Identity-based change detection for a non-git (``local://``) repository.

Such a repository is refreshed only on a trigger. Each refresh that indexes
records, BEFORE its index child lists the source, a manifest of every
non-hidden source file (relative path -> ``(size, mtime_ns)``) inside the
source's ``.code-indexer/``; the snapshot that run publishes inherits it, so
the manifest of the PUBLISHED snapshot describes exactly what that index
was built from. A later check compares the live source with that manifest by
equality: any added, removed or re-stamped file is a change. Equality (not
"newer than") is immune to coarse or skewed mtimes and sees deletions; a
missing, unreadable or invalid manifest is an unknown baseline, which is a
change.

Hidden files and directories (leading ``.``) are not repository content, and
symlinked directories are not followed (the indexer descends into neither).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MANIFEST_RELATIVE_PATH = Path(".code-indexer") / "refresh_source_manifest.json"
_MANIFEST_FORMAT = 1

#: (size in bytes, mtime in nanoseconds)
FileIdentity = Tuple[int, int]


@dataclass(frozen=True)
class SourceListing:
    files: Dict[str, FileIdentity]
    #: Relative paths (``.`` for the root) that could not be listed or stat'ed.
    unreadable: Tuple[str, ...]


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


def read_manifest(snapshot_root: str) -> Tuple[Optional[Dict[str, FileIdentity]], str]:
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
    if not isinstance(entries, dict):
        return None, f"source manifest {path} has no file table"
    manifest: Dict[str, FileIdentity] = {}
    for rel, identity in entries.items():
        if not (
            isinstance(identity, list)
            and len(identity) == 2
            and all(type(v) is int for v in identity)
        ):
            return None, f"source manifest {path} has an invalid entry"
        manifest[rel] = (identity[0], identity[1])
    return manifest, ""


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
    listing = list_source(source_root)
    if listing.unreadable:
        return f"{len(listing.unreadable)} source entries could not be read"
    removed = sum(1 for rel in manifest if rel not in listing.files)
    if removed:
        return f"{removed} file(s) removed since the published snapshot"
    for rel, identity in listing.files.items():
        recorded = manifest.get(rel)
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
    must never inherit an older run's manifest."""
    listing = list_source(source_root)
    path = Path(source_root) / MANIFEST_RELATIVE_PATH
    temp = path.with_name(path.name + ".tmp")
    payload = {
        "format": _MANIFEST_FORMAT,
        "files": {rel: list(identity) for rel, identity in listing.files.items()},
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
    return any(rel not in listing.files for rel in published)
