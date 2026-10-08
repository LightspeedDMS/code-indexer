"""Content-read backend abstraction for cidx's repository reads.

The sibling of ``GitBackend``/``CloneBackend``/``RipgrepBackend``: cidx's job is
semantic search, and reading a repository's bytes off a shared mount is a thing it
should not have to own. Today every indexing read goes straight to the filesystem,
which is why cidx needs the golden-repos NFS mount at all. This module makes that
one hop pluggable so the reads can move to workspace-mcp, which owns filesystem
access by charter.

- ``LocalContentBackend`` -- walks and reads the mount exactly as today. The
  DEFAULT, so selecting nothing is a zero-behaviour-change.
- a delegated backend -- calls workspace-mcp's ``/content/manifest`` and
  ``/content/fetch``. Supplied by a plugin, not defined here, mirroring
  ``GitMcpBackend``.

THIS PHASE LANDS INERT. No call site is migrated; the indexing path still reads
the filesystem directly. That is deliberate: every seam in this codebase lands
this way, because shipping the refactor and the behaviour flip together makes a
regression unattributable. Migrating the read path is phase 4, and it is then a
selector change whose blast radius is known.

WHY THE SEAM IS SYNCHRONOUS. Its consumers -- ``services/file_identifier.py``,
``indexing/file_finder.py`` and the chunkers -- are synchronous, and indexing runs
off the event loop in background job threads. An async protocol would force those
call sites to grow an event loop apiece for no gain. A delegated backend performs
blocking HTTP and is called from the same worker threads.

TWO FACTS THE PROTOCOL EXISTS TO PRESERVE:

* **One definition of content identity.** ``blob_sha`` is the git blob hash, and
  it seeds the deterministic point UUID of every indexed chunk. A backend that
  computes it differently does not raise -- it re-identifies chunks, which
  surfaces as silent duplicate content. ``LocalContentBackend`` therefore calls
  ``utils.blob_hash.git_blob_hash`` rather than growing a second implementation.
* **``fetch`` streams.** It yields, and must never materialise a list: the whole
  point of the paged manifest and the framed fetch is that neither side holds a
  repository's worth of bytes at once.

Selection is NOT part of this protocol. There are no extension filters or size
arguments: the caller decides which paths it wants, because which files cidx
indexes is cidx's policy and must not drift into the service that stores them.
"""

from __future__ import annotations

import bisect
import logging
import ntpath
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator, List, Optional, Protocol, Sequence, Tuple, Union

from ....utils.blob_hash import git_blob_hash, git_blob_hash_bytes

logger = logging.getLogger(__name__)

#: Entries per manifest page when the caller does not say. Matches the server
#: seam's default so local and delegated paging behave alike.
DEFAULT_PAGE_SIZE = 1000


@dataclass(frozen=True)
class ContentEntry:
    """One file as the manifest describes it.

    ``blob_sha`` is ``None`` for a symlink (never followed, so there are no bytes
    to describe) and for a file that could not be read. Callers must treat a
    missing hash as "unknown", never as "empty".
    """

    path: str
    size: int
    blob_sha: Optional[str]
    is_symlink: bool
    #: Modification time in integer nanoseconds, exactly as the filesystem holds
    #: it (a symlink's own time, never its target's). None when unknown -- an
    #: unreadable entry, or a delegated server predating the field -- which a
    #: caller must treat as "unknown", never as the epoch.
    mtime_ns: Optional[int] = None


@dataclass(frozen=True)
class ContentPage:
    """A page of manifest entries. ``next_cursor`` is ``None`` on the last page."""

    entries: Tuple[ContentEntry, ...]
    next_cursor: Optional[str]


@dataclass(frozen=True)
class FetchedFile:
    """One file's bytes, or the reason they could not be had.

    A per-file failure is a RECORD, not an exception: one unreadable file in a
    batch of hundreds must not discard the rest, which is the same contract the
    wire format keeps.
    """

    path: str
    blob_sha: Optional[str]
    data: bytes
    error: Optional[str] = None


def _unsafe_relative_reason(rel: str) -> Optional[str]:
    """Why ``rel`` may not be used as a repo-relative path, or None if it may.

    ONE rule, shared by the two places that take a caller-supplied path: the list
    handed to ``fetch``, and the ``cursor`` handed back to ``manifest``. They were
    separate once and immediately drifted -- ``fetch`` refused ``..`` while the
    cursor, which had just become a filesystem path, did not, so ``manifest``
    would enumerate and hash a sibling repository while ``fetch`` refused the very
    same path.
    """
    if not rel:
        return "empty path refused"
    if PurePosixPath(rel).is_absolute() or ntpath.isabs(rel):
        return "absolute path refused"
    # The RAW components, not PurePosixPath's: pathlib normalises "." away, so
    # asking it would never see the "." that _resume_stack would otherwise descend
    # into -- and a "./a" cursor then yields paths carrying a "./" prefix that no
    # longer match the canonical form the rest of the protocol uses.
    raw_parts = rel.split("/")
    if ".." in raw_parts:
        return "parent-relative component refused"
    if "." in raw_parts:
        return "current-directory component refused"
    return None


class ContentBackend(Protocol):
    """Structural interface for enumerating and reading a repository's files."""

    @property
    def is_local(self) -> bool:
        """True when the files are on THIS container's filesystem.

        Drives any probe that asks the local disk a question. A delegated
        backend's files are not here, so ``os.path.exists`` says nothing about
        whether they exist and must not be consulted -- it would "discover" them
        missing and degrade silently instead of failing.
        """
        ...

    def manifest(
        self,
        repo_path: Union[str, Path],
        *,
        cursor: Optional[str] = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> ContentPage:
        """One page of the repository's files, in a stable order.

        Paging is cursor-based rather than offset-based so a file appearing or
        vanishing mid-walk cannot make the traversal skip or repeat an unrelated
        entry.
        """
        ...

    def fetch(
        self, repo_path: Union[str, Path], paths: Sequence[str]
    ) -> Iterator[FetchedFile]:
        """Yield the named files' bytes, lazily, in the order requested."""
        ...


class LocalContentBackend:
    """Walks and reads the mount in this container, exactly as cidx does today.

    Reproduces current behaviour; this is the default backend.
    """

    @property
    def is_local(self) -> bool:
        return True

    def manifest(
        self,
        repo_path: Union[str, Path],
        *,
        cursor: Optional[str] = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> ContentPage:
        root = Path(repo_path)
        collected: list = []
        last: Optional[str] = None

        for rel in self._walk_ordered(root, cursor):
            collected.append(self._describe(root, rel))
            last = rel
            if len(collected) >= page_size:
                return ContentPage(entries=tuple(collected), next_cursor=last)

        return ContentPage(entries=tuple(collected), next_cursor=None)

    def fetch(
        self, repo_path: Union[str, Path], paths: Sequence[str]
    ) -> Iterator[FetchedFile]:
        """A GENERATOR, deliberately: the caller streams, and materialising a list
        here would hold a repository's worth of bytes in this process."""
        root = Path(repo_path)
        try:
            root_real = os.path.realpath(root)
        except OSError:
            root_real = str(root)

        for rel in paths:
            target, refusal = self._confine(root, root_real, rel)
            if refusal is not None:
                yield FetchedFile(path=rel, blob_sha=None, data=b"", error=refusal)
                continue
            assert target is not None  # _confine returns one or the other
            try:
                data = target.read_bytes()
            except OSError as exc:
                # errno only. The absolute path is an internal layout detail and
                # the message may carry it, so neither is reported.
                yield FetchedFile(
                    path=rel,
                    blob_sha=None,
                    data=b"",
                    error=(os.strerror(exc.errno) if exc.errno else "unreadable"),
                )
                continue
            # Hashed from the bytes in hand, not by opening the file a second
            # time. A second read could see different content, and blob_sha would
            # then describe bytes this record never carried.
            yield FetchedFile(path=rel, blob_sha=git_blob_hash_bytes(data), data=data)

    # ── internals ──────────────────────────────────────────────────────────

    def _confine(
        self, root: Path, root_real: str, rel: str
    ) -> Tuple[Optional[Path], Optional[str]]:
        """Resolve ``rel`` inside ``root``, or say why it is refused.

        ``manifest`` reports a symlink and never follows it, because the target
        may be outside the repository entirely. ``fetch`` has to agree, or the two
        halves of the protocol disagree about the identity of the same path -- and
        phase 4 feeds manifest paths straight into fetch.

        Three refusals, each a per-file record rather than an exception, so one
        bad path does not discard the batch:

        * an ABSOLUTE ``rel``, because ``Path("/repo") / "/etc/passwd"`` is
          ``/etc/passwd``: joining silently discards the root.
        * a ``..`` component, which walks out lexically.
        * a symlink anywhere in the path. The final component is checked with
          ``lstat``; intermediate components are caught by requiring the parent to
          resolve to exactly where the path says it is, which is the same equality
          rule the workspace-mcp side applies, so both backends refuse the same
          repositories.
        """
        unsafe = _unsafe_relative_reason(rel)
        if unsafe is not None:
            return None, unsafe

        target = root / rel
        if target.is_symlink():
            return None, "symlink not followed"

        parent = target.parent
        try:
            parent_real = os.path.realpath(parent)
        except OSError:
            return None, "unreadable parent"
        # Compare resolved against resolved. An earlier version compared
        # realpath(parent) -- absolute and fully resolved -- with
        # normpath(str(parent)), which is only lexically normalised and keeps
        # whatever form repo_path arrived in. That equality held ONLY for an
        # already-canonical absolute root, so a relative repo_path, or any
        # symlinked ancestor (a golden-repo alias, a .versioned snapshot pointer,
        # an NFS mount-point link) made every single file fail -- while manifest,
        # whose scandir follows a symlinked root quite happily, went on listing
        # them. Silent per-file errors for an entire repository.
        #
        # root_real is resolved once by the caller, and rel is already known to be
        # relative and free of "..", so joining them gives where this file's parent
        # MUST resolve to. Anything else means a symlink was traversed on the way.
        expected_parent = os.path.normpath(
            os.path.join(root_real, os.path.dirname(rel))
        )
        if parent_real != expected_parent:
            return None, "symlinked directory not followed"
        # Backstop. Given the guards above this should be unreachable: a lexical
        # parent can only sit outside the root via an absolute path or a ".."
        # component, and both are already refused. It stays because those guards
        # are LEXICAL while this one compares resolved paths, so if one is ever
        # weakened the refusal still happens here rather than becoming a read.
        if parent_real != root_real and not parent_real.startswith(root_real + os.sep):
            return None, "path escapes the repository"
        return target, None

    def _sorted_children(
        self, directory: Path, prefix: str
    ) -> List[Tuple[str, "os.DirEntry[str]", str]]:
        """One directory's entries as ``(sort key, entry, prefix)``, ascending.

        A directory sorts under ``name + "/"`` because that is the prefix every
        path inside it carries; comparing the bare name would order ``sub``
        against ``sub.txt`` wrongly and break the monotonicity the cursor relies
        on. The key is computed once here and kept, so resuming can binary-search
        it without re-deriving anything.

        An unreadable directory yields nothing rather than aborting the walk: one
        bad directory must not abandon the rest of the repository.
        """
        try:
            scanned = list(os.scandir(directory))
        except OSError:
            return []
        children = [
            (
                entry.name + "/" if entry.is_dir(follow_symlinks=False) else entry.name,
                entry,
                prefix,
            )
            for entry in scanned
        ]
        children.sort(key=lambda item: item[0])
        return children

    def _resume_stack(self, root: Path, cursor: Optional[str]) -> List[Iterator]:
        """Rebuild the walk's stack so iteration continues just after ``cursor``.

        The previous implementation restarted at the root every page and threw
        away everything up to the cursor, so exhausting a repository cost
        O(pages x tree): 13 pages over 13 directories issued 103 scandir calls,
        and a 60k-file repo at page_size 1000 would have issued hundreds of
        thousands. On NFS each of those is a round trip.

        A sorted depth-first walk needs no replay. After emitting ``a/b/c.py`` the
        stack is exactly: root positioned after ``a``, ``a`` positioned after
        ``b``, ``b`` positioned after ``c.py``. That is reconstructible from the
        cursor alone by reading one directory per component and binary-searching
        for the position, which costs O(depth) directories instead of all of them.

        Intermediate components are skipped past as ``name + "/"`` because the walk
        is INSIDE them; the final component is a file or symlink, so its own name
        is the key. A cursor naming an entry that has since been deleted still
        lands in the right place, because the search is by key, not by identity.
        """
        if cursor is None:
            return [iter(self._sorted_children(root, ""))]

        # The cursor is opaque to callers but it is INPUT here, and since the walk
        # resumes by descending it, an unvalidated one is a path traversal: a
        # cursor of "../other-repo/a" made manifest enumerate and hash a sibling
        # repository, which on the golden-repos mount is every other repository.
        # Raised rather than silently restarted: a caller round-tripping a cursor
        # it did not get from us is a bug, and restarting would hide it behind a
        # duplicate first page.
        unsafe = _unsafe_relative_reason(cursor)
        if unsafe is not None:
            raise ValueError(f"invalid manifest cursor: {unsafe}")

        stack: List[Iterator] = []
        parts = [part for part in cursor.split("/") if part]
        directory, prefix = root, ""
        for index, part in enumerate(parts):
            children = self._sorted_children(directory, prefix)
            is_last = index == len(parts) - 1
            target = part if is_last else part + "/"
            position = bisect.bisect_right([key for key, _, _ in children], target)
            stack.append(iter(children[position:]))
            if not is_last:
                nxt = directory / part
                # The walk never DESCENDS a symlink -- it emits one as a leaf and
                # moves on (is_dir(follow_symlinks=False)). Resuming must not be
                # able to reach what a fresh walk cannot, or the cursor becomes a
                # way to enumerate THROUGH a committed symlink: os.scandir follows
                # links, so a repo containing "dirlink -> ../other-repo" plus a
                # cursor of "dirlink/a" listed and hashed the sibling repository,
                # while fetch refused the very same path.
                #
                # Lexical validation above cannot see this: "dirlink/a" has no
                # "..", no ".", and is relative. It is the filesystem, not the
                # string, that escapes.
                if os.path.islink(nxt):
                    raise ValueError(
                        "invalid manifest cursor: symlinked directory component"
                    )
                directory = nxt
                prefix = f"{prefix}{part}/"
        return stack

    def _walk_ordered(self, root: Path, cursor: Optional[str] = None) -> Iterator[str]:
        """Repo-relative POSIX paths in ascending path order, ``.git`` excluded.

        The order must be MONOTONIC in the emitted path, because the cursor is a
        path and resuming means "everything after this one". Emitting a
        directory's files before descending breaks that: ``sub/b.py`` would come
        after ``unicode.ts`` despite sorting before it, and resuming from
        ``unicode.ts`` would skip it entirely. So files and directories are walked
        as ONE sorted sequence, descending when a directory's turn arrives.

        One directory is materialised at a time, never the whole tree.
        """
        stack = self._resume_stack(root, cursor)
        while stack:
            nxt = next(stack[-1], None)
            if nxt is None:
                stack.pop()
                continue
            _key, entry, prefix = nxt
            rel = f"{prefix}{entry.name}"
            if entry.is_symlink():
                yield rel
            elif entry.is_dir(follow_symlinks=False):
                if entry.name == ".git":
                    continue
                stack.append(iter(self._sorted_children(Path(entry.path), f"{rel}/")))
            elif entry.is_file(follow_symlinks=False):
                yield rel

    def _describe(self, root: Path, rel: str) -> ContentEntry:
        target = root / rel
        try:
            stat = target.lstat()
        except OSError:
            return ContentEntry(path=rel, size=0, blob_sha=None, is_symlink=False)

        if os.path.islink(target):
            # Reported, never followed: the bytes behind a link belong to whatever
            # the link points at, which may be outside the repository entirely.
            return ContentEntry(
                path=rel,
                size=stat.st_size,
                blob_sha=None,
                is_symlink=True,
                mtime_ns=stat.st_mtime_ns,
            )

        return ContentEntry(
            path=rel,
            size=stat.st_size,
            blob_sha=git_blob_hash(target),
            is_symlink=False,
            mtime_ns=stat.st_mtime_ns,
        )


class ContentBackendFactory:
    """Create a ContentBackend from a backend name.

    Mirrors ``GitBackendFactory`` and ``RipgrepBackendFactory``. ``local`` (the
    default) reads this container's filesystem. ``delegated`` must be injected by
    the plugin seam and raises otherwise, so a misconfiguration fails loudly
    instead of silently reading the mount -- the whole point of delegating is that
    the mount may not be there, and a silent fallback would surface as an empty
    repository rather than an error.
    """

    @staticmethod
    def create(
        backend: str = "local",
        *,
        delegated_backend: Optional[ContentBackend] = None,
    ) -> ContentBackend:
        if backend == "local":
            return LocalContentBackend()
        if backend == "delegated":
            if delegated_backend is None:
                raise RuntimeError(
                    "content_backend='delegated' selected but no delegated backend "
                    "was injected. Install the delegated backend plugin, or set "
                    "content_backend='local'. There is deliberately no fallback to "
                    "the local filesystem: a pod built for delegation may not mount "
                    "the repositories at all, and a silent fallback would surface as "
                    "an empty repository instead of an error."
                )
            return delegated_backend
        raise RuntimeError(
            f"Unknown content_backend {backend!r}; expected 'local' or 'delegated'"
        )
