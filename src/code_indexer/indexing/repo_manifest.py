"""One manifest of a repository, built once per indexing pass.

A pass asks each file for its git blob hash and then reads it again to chunk it.
The hash is a function of the bytes, and a content backend already reports it for
every file in one paged call, so asking the manifest removes that read.

Built ONLY for a delegated backend. ``build`` goes through
``ContentBackend.manifest``, and the LOCAL backend computes those hashes by
reading every file -- so against local it would move the hash reads to the front
of the pass instead of removing them. Measured over a 20-file repository: 40
opens without the manifest (20 hash + 20 chunk), 40 with it (20 to build + 20 to
chunk). Exactly a wash, plus the whole manifest held in memory and hashes
computed for files the indexer would have filtered out.

The saving is real only when the manifest costs no local reads -- a delegated
backend serving hashes over its own paged call, where the same 20-file pass does
20 opens instead of 40.

SYMLINKS ARE NOT SPECIAL-CASED HERE. A delegated manifest reports them with no
hash, and an earlier version of this module dropped them from the pass on the
grounds that a symlink's chunks are byte-identical to its target's and carry the
same point ids, so indexing one adds nothing. That is true only when the target
is itself in the pass. A symlink pointing outside the repository root, at an
excluded or generated path, or at nothing at all, carries content that is
reachable NO other way -- and ``ContentEntry`` has no target field, so there is
no way from here to tell those apart. Dropping them would silently index less in
exactly the cases that matter. They take the ordinary path instead and end as
counted failures.

Built from ``ContentBackend.manifest``, so the same object serves either backend
without the caller knowing which.

WHAT THIS IS NOT. It is not a cache of file contents, and it is not consulted to
decide whether a file still exists. It answers "what did the repository look like
when this pass started", which is exactly the question the hash phase needs and
deliberately NOT the question the read path needs -- a file that changes mid-pass
must be caught by the read, not papered over from here.
"""

from __future__ import annotations

import inspect
import logging
import os
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterator, Optional, Sequence, Union

from ..server.storage.shared.content_backend import (
    ContentBackend,
    ContentBackendFactory,
    ContentEntry,
)

logger = logging.getLogger(__name__)


def _accepts_prune_dirs(backend: Any) -> bool:
    """Whether ``backend.manifest`` takes ``prune_dirs`` (or arbitrary kwargs)."""
    try:
        params = inspect.signature(backend.manifest).parameters
    except (TypeError, ValueError):
        return False
    return "prune_dirs" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


class RepoManifest:
    """Path -> entry for one repository, as of when it was built."""

    def __init__(
        self,
        root: Path,
        entries: Dict[str, ContentEntry],
        *,
        backend_is_local: bool = True,
    ) -> None:
        self._root = root
        self._entries = entries
        # Whether the backend that produced these entries reads THIS container's
        # disk. It decides what an entry with no hash means: with a local
        # backend the file is still right there to be read, and with a delegated
        # one it may not exist here at all.
        self._backend_is_local = backend_is_local

    @classmethod
    def build(
        cls,
        repo_path: Union[str, Path],
        backend: Optional[ContentBackend] = None,
        page_size: int = 1000,
        prune_dirs: Sequence[str] = (),
    ) -> "RepoManifest":
        """Page the whole repository once.

        Paging rather than one huge call: a repository is not bounded, and the
        cursor contract exists so neither side has to hold it whole.

        ``prune_dirs``: directory names discovery always discards
        (``FileFinder.manifest_prune_dirs``), sent on EVERY page so the backend
        never reads or hashes them. Sent only to a backend whose ``manifest``
        accepts it: one predating the argument (a plugin older than 0.3.0)
        still works, unpruned -- slower, and exactly as correct, because
        discovery filters what comes back either way.
        """
        chosen = backend if backend is not None else ContentBackendFactory.create()
        root = Path(repo_path)
        entries: Dict[str, ContentEntry] = {}
        cursor: Optional[str] = None
        prune = list(prune_dirs) if _accepts_prune_dirs(chosen) else []
        if prune_dirs and not prune:
            logger.info(
                "content backend %s cannot prune directories; manifest walks them",
                type(chosen).__name__,
            )
        while True:
            if prune:
                page = chosen.manifest(
                    root, cursor=cursor, page_size=page_size, prune_dirs=prune
                )
            else:
                page = chosen.manifest(root, cursor=cursor, page_size=page_size)
            for entry in page.entries:
                entries[entry.path] = entry
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        return cls(
            root,
            entries,
            backend_is_local=bool(getattr(chosen, "is_local", True)),
        )

    def _relative(self, file_path: Union[str, Path]) -> Optional[str]:
        """The repo-relative POSIX key for a path, or None if it is not inside.

        Accepts a path under the root OR one that is already repo-relative.
        Callers legitimately hold both: the indexer walks absolute paths, while
        the fetch planner and the wire format speak repo-relative ones, and a
        manifest that understood only the first returned None for every lookup
        the planner made. That failed SILENTLY -- the planner reads None as
        "size unknown", sends each file in a request of its own, and quietly
        becomes the per-file fetching it exists to prevent. Measured on EKS
        against a real golden repo: 43 batches where one was correct.
        """
        candidate = Path(file_path)
        if candidate.is_absolute():
            try:
                return candidate.relative_to(self._root).as_posix()
            except ValueError:
                return None
        # Already relative. Normalise so "./a.py" and "a.py" are one key, and
        # refuse anything climbing out of the repository.
        key = PurePosixPath(os.path.normpath(candidate.as_posix())).as_posix()
        if key == ".." or key.startswith("../"):
            return None
        return key

    def blob_sha(self, file_path: Union[str, Path]) -> Optional[str]:
        """The git blob hash, or None when this manifest cannot vouch for it.

        None for a path outside the repository, a path the manifest never listed
        (added after the pass began), and a symlink -- the manifest reports those
        but never follows them, so it has no hash to give. Every caller must treat
        None as "ask the filesystem", never as "no hash": returning a wrong hash
        here would not raise, it would RE-IDENTIFY chunks, because this value
        seeds each chunk's deterministic point UUID.
        """
        key = self._relative(file_path)
        if key is None:
            return None
        entry = self._entries.get(key)
        if entry is None or entry.is_symlink:
            return None
        return entry.blob_sha

    def size(self, file_path: Union[str, Path]) -> Optional[int]:
        key = self._relative(file_path)
        entry = self._entries.get(key) if key is not None else None
        return None if entry is None else entry.size

    def mtime(self, file_path: Union[str, Path]) -> Optional[float]:
        """The file's ``st_mtime`` exactly as ``os.stat`` would report it, or None.

        Rebuilt from the integer nanoseconds the way CPython builds ``st_mtime``
        (``sec + nsec * 1e-9``), so it is the SAME float a local stat of the
        unchanged file returns, bit for bit. That matters: mtimes are stored in
        point payloads and compared on later passes, and a value off in the last
        bit reads as "modified" forever. ``ns / 1e9`` is not that float.
        """
        key = self._relative(file_path)
        entry = self._entries.get(key) if key is not None else None
        # getattr, not attribute access: a delegated plugin's entries are its own
        # type, and one built before the field existed has no such attribute.
        mtime_ns = getattr(entry, "mtime_ns", None) if entry is not None else None
        if not isinstance(mtime_ns, int):
            return None
        sec, nsec = divmod(mtime_ns, 1_000_000_000)
        return sec + nsec * 1e-9

    def is_symlink(self, file_path: Union[str, Path]) -> Optional[bool]:
        key = self._relative(file_path)
        entry = self._entries.get(key) if key is not None else None
        return None if entry is None else entry.is_symlink

    def paths(self) -> Iterator[str]:
        """Repo-relative paths, in the manifest's own order."""
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)
