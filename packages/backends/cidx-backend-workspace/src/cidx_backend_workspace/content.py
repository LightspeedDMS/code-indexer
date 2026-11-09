"""The local content backend: walk and read a repository on this pod's disk.

Registered on ``code_indexer.content_backend`` under the entry name ``local``
(see this distribution's ``pyproject.toml``), so the core reaches it only
through the seam resolver when ``content_backend`` is ``local``. It honours
every manifest option, so it is also the reference for their semantics, and it
must pass ``cidx_testkit.conformance.content`` like any delegated backend.

Moved from ``code_indexer.server.storage.shared.content_backend``, which
re-exports :class:`LocalContentBackend` for existing imports.
"""

from __future__ import annotations

import bisect
import os
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple, Union

from cidx_sdk.seams.content import (
    DEFAULT_PAGE_SIZE,
    MANIFEST_OPTIONS,
    REFUSED_ESCAPE,
    REFUSED_SYMLINK,
    REFUSED_SYMLINKED_DIRECTORY,
    ContentBackend,
    ContentEntry,
    ContentPage,
    FetchedFile,
    normalize_manifest_prefix,
    unsafe_relative_reason,
)
from cidx_sdk.seams.content_identity import git_blob_hash, git_blob_hash_bytes


class LocalContentBackend:
    """Walks and reads the mount in this container, exactly as cidx does today.

    Reproduces current behaviour; this is the default backend. It honours
    every manifest option, so it is also the reference for their semantics.
    """

    #: Every manifest option is honoured here (see :data:`MANIFEST_OPTIONS`).
    manifest_options = MANIFEST_OPTIONS

    @property
    def is_local(self) -> bool:
        return True

    def manifest(
        self,
        repo_path: Union[str, Path],
        *,
        cursor: Optional[str] = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        prune_dirs: Optional[Sequence[str]] = None,
        hash: bool = True,
        prefix: Optional[str] = None,
        max_depth: Optional[int] = None,
    ) -> ContentPage:
        root = Path(repo_path)
        scope = normalize_manifest_prefix(prefix)
        if max_depth is not None and max_depth < 1:
            raise ValueError("max_depth must be at least 1")
        if cursor is not None and scope and not cursor.startswith(scope + "/"):
            raise ValueError("invalid manifest cursor: outside the requested prefix")
        collected: list = []
        last: Optional[str] = None

        for rel in self._walk_scoped(
            root, scope, max_depth, cursor, frozenset(prune_dirs or ())
        ):
            collected.append(self._describe(root, rel, with_hash=hash))
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
          rule the the content service side applies, so both backends refuse the same
          repositories.
        """
        unsafe = unsafe_relative_reason(rel)
        if unsafe is not None:
            return None, unsafe

        target = root / rel
        if target.is_symlink():
            return None, REFUSED_SYMLINK

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
            return None, REFUSED_SYMLINKED_DIRECTORY
        # Backstop. Given the guards above this should be unreachable: a lexical
        # parent can only sit outside the root via an absolute path or a ".."
        # component, and both are already refused. It stays because those guards
        # are LEXICAL while this one compares resolved paths, so if one is ever
        # weakened the refusal still happens here rather than becoming a read.
        if parent_real != root_real and not parent_real.startswith(root_real + os.sep):
            return None, REFUSED_ESCAPE
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
        unsafe = unsafe_relative_reason(cursor)
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

    def _walk_ordered(
        self,
        root: Path,
        cursor: Optional[str] = None,
        prune: frozenset = frozenset(),
        max_depth: Optional[int] = None,
    ) -> Iterator[str]:
        """Repo-relative POSIX paths in ascending path order, ``.git`` and any
        directory named in ``prune`` excluded.

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
                if entry.name == ".git" or entry.name in prune:
                    continue
                if max_depth is not None and len(stack) >= max_depth:
                    continue  # its files are deeper than the caller asked for
                stack.append(iter(self._sorted_children(Path(entry.path), f"{rel}/")))
            elif entry.is_file(follow_symlinks=False):
                yield rel

    def _walk_scoped(
        self,
        root: Path,
        prefix: str,
        max_depth: Optional[int],
        cursor: Optional[str],
        prune: frozenset,
    ) -> Iterator[str]:
        """:meth:`_walk_ordered` of ``root/prefix``, as root-relative paths.

        A prefix that is missing, is not a directory, crosses a symlink or
        passes through a pruned or ``.git`` directory lists nothing -- the
        whole-repository walk would list nothing beneath it either.
        """
        if not prefix:
            yield from self._walk_ordered(root, cursor, prune, max_depth)
            return
        base = root
        for part in prefix.split("/"):
            base = base / part
            if part == ".git" or part in prune or base.is_symlink():
                return
        if not base.is_dir():
            return
        inner_cursor = cursor[len(prefix) + 1 :] if cursor else None
        for rel in self._walk_ordered(base, inner_cursor, prune, max_depth):
            yield f"{prefix}/{rel}"

    def _describe(
        self, root: Path, rel: str, *, with_hash: bool = True
    ) -> ContentEntry:
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
            blob_sha=git_blob_hash(target) if with_hash else None,
            is_symlink=False,
            mtime_ns=stat.st_mtime_ns,
        )


def make_content_backend() -> ContentBackend:
    """The ``local`` entry point's factory: a backend over this pod's disk."""
    return LocalContentBackend()
