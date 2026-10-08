"""One manifest of a repository, built once per indexing pass.

Today an indexing pass opens each file up to three times -- the text probe, the
blob hash, and chunking -- on top of an ``os.walk`` and a ``stat``. The blob hash
read is pure waste: the hash is a function of the bytes, and a content backend
already reports it for every file in one paged call. Asking the manifest instead
removes that read entirely, which on the golden-repos NFS mount removes a round
trip per file.

Built from ``ContentBackend.manifest``, so the same object serves the local
backend today and a delegated one later without the caller knowing which.

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

    def __init__(self, root: Path, entries: Dict[str, ContentEntry]) -> None:
        self._root = root
        self._entries = entries

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
        return cls(root, entries)

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
