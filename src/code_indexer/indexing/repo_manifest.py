"""One manifest of a repository, built once per indexing pass.

A pass asks each file for its git blob hash and then reads it again to chunk it.
The hash is a function of the bytes, and a content backend already reports it for
every file in one paged call, so asking the manifest removes that read.

NOT WIRED INTO THE INDEXER YET, on purpose. ``build`` goes through
``ContentBackend.manifest``, and the LOCAL backend computes those hashes by
reading every file -- so wiring this against the local backend moves the hash
reads to the front of the pass instead of removing them. Measured over a 20-file
repository: 40 opens without the manifest (20 hash + 20 chunk), 40 with it (20 to
build + 20 to chunk). Exactly a wash, plus the whole manifest held in memory and
hashes computed for files the indexer would have filtered out.

The saving is real only when the manifest costs no local reads -- a delegated
backend serving hashes over its own paged call, where the same 20-file pass does
20 opens instead of 40. This module is the piece that makes that switch a
constructor argument rather than a rewrite of the hash phase; selecting a
delegated backend still needs the config key and plugin loader that the other
seams have.

Built from ``ContentBackend.manifest``, so the same object serves either backend
without the caller knowing which.

WHAT THIS IS NOT. It is not a cache of file contents, and it is not consulted to
decide whether a file still exists. It answers "what did the repository look like
when this pass started", which is exactly the question the hash phase needs and
deliberately NOT the question the read path needs -- a file that changes mid-pass
must be caught by the read, not papered over from here.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Iterator, Optional, Union

from ..server.storage.shared.content_backend import (
    ContentBackend,
    ContentBackendFactory,
    ContentEntry,
)

logger = logging.getLogger(__name__)


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
    ) -> "RepoManifest":
        """Page the whole repository once.

        Paging rather than one huge call: a repository is not bounded, and the
        cursor contract exists so neither side has to hold it whole.
        """
        chosen = backend if backend is not None else ContentBackendFactory.create()
        root = Path(repo_path)
        entries: Dict[str, ContentEntry] = {}
        cursor: Optional[str] = None
        while True:
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
        """The repo-relative POSIX key for a path, or None if it is not inside."""
        try:
            return Path(file_path).relative_to(self._root).as_posix()
        except ValueError:
            return None

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

    def unindexable_reason(self, file_path: Union[str, Path]) -> Optional[str]:
        """Why this pass cannot index *file_path*, or None if it can.

        Only ever non-None for a DELEGATED backend. With a local backend the
        file is on this disk and everything stays exactly as it is today.

        The one reason is a symlink. A delegated backend enumerates symlinks and
        refuses to read them -- deliberately, so that fetch can never return
        bytes the manifest would not list -- so there is no way to obtain the
        content, from the backend or from a disk this pod may not have.

        Skipping costs nothing. A chunk's point UUID is seeded from
        ``{project_id}:{blob_sha}:{index}`` with NO path, so a symlink's chunks
        are byte-identical to its target's and carry the same ids: following one
        overwrites the target's chunks with a copy of themselves. Measured on a
        fixture containing both, the ids matched exactly. What the symlink points
        at is indexed under its real path either way.

        Returns a short reason string rather than a bool so the caller can report
        WHY a file was left out. A run that silently indexes less is the failure
        this whole seam exists to avoid.
        """
        if self._backend_is_local:
            return None
        key = self._relative(file_path)
        if key is None:
            return None
        entry = self._entries.get(key)
        if entry is None:
            # A manifest MISS is not a refusal: the file may have been added
            # after the pass began. Let it take the normal path and fail there
            # if it truly cannot be read.
            return None
        return "symlink" if entry.is_symlink else None

    def size(self, file_path: Union[str, Path]) -> Optional[int]:
        key = self._relative(file_path)
        entry = self._entries.get(key) if key is not None else None
        return None if entry is None else entry.size

    def is_symlink(self, file_path: Union[str, Path]) -> Optional[bool]:
        key = self._relative(file_path)
        entry = self._entries.get(key) if key is not None else None
        return None if entry is None else entry.is_symlink

    def paths(self) -> Iterator[str]:
        """Repo-relative paths, in the manifest's own order."""
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)
