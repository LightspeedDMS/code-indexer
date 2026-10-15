"""A golden repo's wiki files read through the content backend.

On a pod configured with a delegated content backend the repositories are not
on this disk: a golden repo's alias target is a versioned snapshot holding
index state only. The wiki then lists the base clone through the backend's
manifest and reads its markdown and assets through ``fetch`` -- the same seam,
root mapping and TTL-cached manifest the golden file tools use
(``services/file_service.py``).

The answers mirror what the local wiki derives from ``rglob``/``exists``/
``is_file``/``read_text``/``stat`` so the rendered pages are the same; what
differs is the failure mode. A backend that cannot answer raises
:class:`WikiContentUnavailable` -- the wiki must never look empty, or answer
"not found" for a file that exists, because the backend was down.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Dict, FrozenSet, Iterator, List, Optional, Sequence, Tuple

from code_indexer.indexing.fetch_planner import plan_fetch_batches
from code_indexer.server.services.file_service import (
    ContentSeamSource,
    content_seam_source,
    manifest_entries,
)
from code_indexer.server.storage.shared.content_backend import ContentEntry
from code_indexer.utils.path_confinement import confine_relative_path

from .wiki_cache import FileStamp
from .wiki_service import MarkdownText

logger = logging.getLogger(__name__)

#: Named in the warning logged when a snapshot has no registered base clone.
WIKI_SEAM_CONSUMER = "Wiki"

#: The pattern the local wiki globs for (``Path.rglob("*.md")``).
_MARKDOWN_GLOB = "*.md"

#: No directory is pruned from the wiki's manifest: the local walk descends
#: everywhere and filters hidden paths afterwards, and so does this.
_NO_PRUNE: Tuple[str, ...] = ()


class WikiContentUnavailable(RuntimeError):
    """The content backend could not list or read the wiki's repository."""


def _mtime_from_ns(mtime_ns: Optional[int]) -> Optional[float]:
    """``st_mtime`` exactly as CPython computes it from the same nanoseconds
    (seconds plus nanoseconds times 1e-9, in floating point) -- so a cache row
    stored from a local ``stat()`` validates against the manifest."""
    if mtime_ns is None:
        return None
    seconds, nanos = divmod(mtime_ns, 1_000_000_000)
    return seconds + nanos * 1e-9


def _decode_text(data: bytes) -> str:
    """*data* exactly as ``Path.read_text(encoding="utf-8")`` returns it:
    strict UTF-8, universal newlines. UnicodeDecodeError propagates."""
    with io.TextIOWrapper(io.BytesIO(data), encoding="utf-8") as text:
        return text.read()


def _parent_dirs(paths: Sequence[str]) -> FrozenSet[str]:
    """Every directory that holds one of *paths*, the root (``""``) included."""
    dirs = {""}
    for path in paths:
        parts = path.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:depth]))
    return frozenset(dirs)


def _walk_order_key(path: str) -> Tuple[str, ...]:
    """``sorted(Path.rglob(...))`` order: paths compare by their components."""
    return tuple(path.split("/"))


@dataclass(frozen=True)
class SeamWikiTree:
    """One golden repo's files as the content backend's manifest lists them."""

    source: ContentSeamSource
    entries: Dict[str, ContentEntry]
    dirs: FrozenSet[str]

    @classmethod
    def load(cls, source: ContentSeamSource) -> "SeamWikiTree":
        """List the repository (the TTL-cached manifest; one walk per window)."""
        try:
            listed = manifest_entries(source, _NO_PRUNE)
        except Exception as exc:  # the backend's failure, whatever it is
            raise WikiContentUnavailable(
                f"Wiki content unavailable: the content backend could not list "
                f"the repository ({exc})"
            ) from exc
        entries = {entry.path: entry for entry in listed}
        return cls(source=source, entries=entries, dirs=_parent_dirs(list(entries)))

    @property
    def root(self) -> str:
        return self.source.root

    # ── what the local wiki asks the filesystem ──────────────────────────

    @staticmethod
    def confine(path: str) -> Optional[str]:
        """*path* as a repo-relative path, or None when it may not be served.

        Lexical (absolute paths and ``..`` are refused): there is no local
        tree to ``resolve()`` against.
        """
        try:
            return confine_relative_path(path)
        except PermissionError:
            return None

    def has_file(self, rel: str) -> bool:
        return rel in self.entries

    def has_path(self, rel: str) -> bool:
        return rel in self.entries or rel in self.dirs

    def stamp(self, rel: str) -> FileStamp:
        """The article's cache-validation stamp, from its manifest entry."""
        entry = self.entries[rel]
        return FileStamp(mtime=_mtime_from_ns(entry.mtime_ns), size=entry.size)

    def md_paths(self) -> List[str]:
        """Every ``*.md`` path, in the order ``sorted(rglob("*.md"))`` yields."""
        return sorted(
            (
                path
                for path in self.entries
                if fnmatchcase(PurePosixPath(path).name, _MARKDOWN_GLOB)
            ),
            key=_walk_order_key,
        )

    def visible_md_paths(self) -> List[str]:
        """:meth:`md_paths` without any path having a hidden component."""
        return [
            path
            for path in self.md_paths()
            if not any(part.startswith(".") for part in path.split("/"))
        ]

    def max_md_mtime(self) -> float:
        """``_max_mtime_of_md_files`` over the manifest: hidden DIRECTORIES are
        skipped, an unknown mtime is skipped as an unstat-able file is."""
        max_mtime = 0.0
        for path in self.md_paths():
            if any(part.startswith(".") for part in path.split("/")[:-1]):
                continue
            mtime = _mtime_from_ns(self.entries[path].mtime_ns)
            if mtime is not None and mtime > max_mtime:
                max_mtime = mtime
        return max_mtime

    # ── reads ────────────────────────────────────────────────────────────

    def fetch_bytes(self, rel: str) -> bytes:
        """The bytes of a file the manifest lists.

        A per-file failure is NOT "not found": the manifest says the file is
        there, so it raises :class:`WikiContentUnavailable`.
        """
        fetched = next(iter(self._fetch([rel])), None)
        if fetched is None or fetched[1] is None:
            reason = "not returned" if fetched is None else fetched[2]
            raise WikiContentUnavailable(
                f"Wiki content unavailable: the content backend could not read "
                f"{rel} ({reason})"
            )
        return fetched[1]

    def fetch_text(self, rel: str) -> str:
        """:meth:`fetch_bytes` decoded as ``Path.read_text`` decodes."""
        return _decode_text(self.fetch_bytes(rel))

    def read_visible_md_texts(self) -> Iterator[MarkdownText]:
        """Every visible ``*.md`` file's text, in walk order, fetched in
        byte-budgeted batches. A file the backend could not read, or that is
        not UTF-8, carries the reason instead of text."""
        for rel, data, failure in self._fetch(self.visible_md_paths()):
            path = PurePosixPath(rel)
            if data is None:
                yield path, None, failure
                continue
            try:
                yield path, _decode_text(data), None
            except UnicodeDecodeError as exc:
                yield path, None, str(exc)

    def _fetch(
        self, paths: Sequence[str]
    ) -> Iterator[Tuple[str, Optional[bytes], Optional[str]]]:
        """(path, bytes or None, failure reason) per requested path, in order.

        A failed CALL raises :class:`WikiContentUnavailable`; a failed FILE is
        a record, as the protocol keeps it.
        """
        sizes = {path: entry.size for path, entry in self.entries.items()}
        for batch in plan_fetch_batches(paths, sizes.get):
            try:
                fetched = list(self.source.backend.fetch(self.source.root, batch))
            except Exception as exc:  # the backend's failure, whatever it is
                raise WikiContentUnavailable(
                    f"Wiki content unavailable: the content backend could not "
                    f"read the repository ({exc})"
                ) from exc
            by_path = {record.path: record for record in fetched}
            for path in batch:
                record = by_path.get(path)
                if record is None:
                    yield path, None, "not returned"
                elif record.error is not None:
                    yield path, None, record.error
                else:
                    yield path, record.data, None


def content_backend_wiki_tree(repo_path: str) -> Optional[SeamWikiTree]:
    """The golden repo behind *repo_path* through the content backend, or None
    to read this pod's disk (no backend, or a local one: today's behaviour).

    Raises :class:`WikiContentUnavailable` when the backend cannot list it.
    """
    source = content_seam_source(repo_path, consumer=WIKI_SEAM_CONSUMER)
    if source is None:
        return None
    return SeamWikiTree.load(source)
