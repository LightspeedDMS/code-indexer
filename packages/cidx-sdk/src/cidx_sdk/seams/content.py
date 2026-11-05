"""The content seam: enumerate and read a repository's files.

Selected by the core's ``content_backend`` setting; entry-point group
``code_indexer.content_backend``. ``local`` reads this pod's disk, ``delegated``
asks the service that owns filesystem access (workspace-mcp).

What every implementation must keep (the conformance suite in
``cidx_testkit.conformance.content`` checks it):

* ``blob_sha`` is the git blob hash of the bytes
  (:func:`cidx_sdk.seams.content_identity.git_blob_hash_bytes`). It seeds the
  identity of every indexed chunk, so a backend that computes it differently
  does not fail -- it silently re-identifies content.
* A per-file failure is a :class:`FetchedFile` record carrying ``error``, never
  an exception: one unreadable file must not discard a batch.
* Symlinks are listed, never followed; paths are repo-relative POSIX strings.
* Manifest paging is cursor-based and stable.

The types here moved from ``code_indexer.kernel.seam_types`` (and before that
from ``server/storage/shared/content_backend.py`` and
``server/services/file_service.py``); the old modules re-export them.
"""

from __future__ import annotations

import errno
import ntpath
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import (
    FrozenSet,
    Iterator,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Union,
)

#: Per-file fetch errors that mean "the file is there but could not be read":
#: a directory, or a file the backend will not serve. Every OTHER per-file
#: error reads as "not found" (workspace-mcp answers EACCES, not ENOENT, for a
#: file under a missing directory).
UNREADABLE_FETCH_ERRORS = frozenset(
    {"EISDIR", os.strerror(errno.EISDIR), "EFBIG", "ESIZECHANGED"}
)

#: Per-file fetch errors that mean "there is no such file": ENOENT, ENOTDIR (a
#: path through a file), and EACCES, which workspace-mcp answers for a file
#: under a missing directory. For callers that must tell ABSENCE from FAILURE
#: (an absent build file is an answer; EIO or a timeout record is not): an
#: error in neither this set nor ``UNREADABLE_FETCH_ERRORS`` is a failure.
NOT_FOUND_FETCH_ERRORS = frozenset(
    {
        "ENOENT",
        os.strerror(errno.ENOENT),
        "ENOTDIR",
        os.strerror(errno.ENOTDIR),
        "EACCES",
        os.strerror(errno.EACCES),
    }
)

#: Entries per manifest page when the caller does not say. Matches the server
#: seam's default so local and delegated paging behave alike.
DEFAULT_PAGE_SIZE = 1000

# ── manifest options ─────────────────────────────────────────────────────────
#: Optional manifest request fields beyond the base protocol. A backend
#: advertises the ones it honours in a ``manifest_options`` attribute (or, for
#: plugins predating that, by taking them as ``manifest()`` keywords).
#: ``hash`` False skips the blob hash; ``prefix`` lists one directory's
#: subtree; ``max_depth`` limits the depth below ``prefix`` (1 = direct
#: children).
MANIFEST_OPTION_HASH = "hash"
MANIFEST_OPTION_PREFIX = "prefix"
MANIFEST_OPTION_MAX_DEPTH = "max_depth"
MANIFEST_OPTIONS: FrozenSet[str] = frozenset(
    {MANIFEST_OPTION_HASH, MANIFEST_OPTION_PREFIX, MANIFEST_OPTION_MAX_DEPTH}
)

# ── refusals ─────────────────────────────────────────────────────────────────
#: Why a path is refused rather than read. A refused path is answered as an
#: absent file by every reader: the protocol never serves it.
REFUSED_EMPTY_PATH = "empty path refused"
REFUSED_ABSOLUTE_PATH = "absolute path refused"
REFUSED_PARENT_COMPONENT = "parent-relative component refused"
REFUSED_CURRENT_COMPONENT = "current-directory component refused"
REFUSED_SYMLINK = "symlink not followed"
REFUSED_SYMLINKED_DIRECTORY = "symlinked directory not followed"
REFUSED_ESCAPE = "path escapes the repository"
REFUSED_FETCH_ERRORS = frozenset(
    {
        REFUSED_EMPTY_PATH,
        REFUSED_ABSOLUTE_PATH,
        REFUSED_PARENT_COMPONENT,
        REFUSED_CURRENT_COMPONENT,
        REFUSED_SYMLINK,
        REFUSED_SYMLINKED_DIRECTORY,
        REFUSED_ESCAPE,
    }
)


def unsafe_relative_reason(rel: str) -> Optional[str]:
    """Why ``rel`` may not be used as a repo-relative path, or None if it may.

    ONE rule for every place that takes a caller-supplied path -- the paths
    handed to ``fetch``, the ``cursor`` handed back to ``manifest`` and the
    ``prefix`` option -- in every backend. They were separate once and
    drifted: ``fetch`` refused ``..`` while the cursor did not, so ``manifest``
    would enumerate a sibling repository while ``fetch`` refused the very same
    path.
    """
    if not rel:
        return REFUSED_EMPTY_PATH
    if PurePosixPath(rel).is_absolute() or ntpath.isabs(rel):
        return REFUSED_ABSOLUTE_PATH
    # The RAW components, not PurePosixPath's: pathlib normalises "." away, so
    # asking it would never see a "." a walk would otherwise descend into --
    # and a "./a" cursor then yields paths carrying a "./" prefix that no
    # longer match the canonical form the rest of the protocol uses.
    raw_parts = rel.split("/")
    if ".." in raw_parts:
        return REFUSED_PARENT_COMPONENT
    if "." in raw_parts:
        return REFUSED_CURRENT_COMPONENT
    return None


def normalize_manifest_prefix(prefix: Optional[str]) -> str:
    """*prefix* as the canonical repo-relative directory ("" for the root).

    Raises ValueError for anything that is not a plain relative path: the
    prefix becomes a filesystem path on the server, so it is validated by the
    same rule as a fetch path or a cursor.
    """
    if not prefix:
        return ""
    cleaned = prefix.rstrip("/")
    if not cleaned:
        raise ValueError("invalid manifest prefix: absolute path refused")
    unsafe = unsafe_relative_reason(cleaned)
    if unsafe is not None:
        raise ValueError(f"invalid manifest prefix: {unsafe}")
    return cleaned


def entry_in_scope(path: str, prefix: str, max_depth: Optional[int]) -> bool:
    """Whether a repo-relative *path* lies in the view ``prefix``/``max_depth``."""
    if prefix:
        if not path.startswith(prefix + "/"):
            return False
        rest = path[len(prefix) + 1 :]
    else:
        rest = path
    if max_depth is not None and rest.count("/") + 1 > max_depth:
        return False
    return True


#: How workspace-mcp answers a manifest whose ROOT does not exist: HTTP 400
#: with this marker in the detail. A local backend lists such a root as empty.
ABSENT_ROOT_STATUS = 400
ABSENT_ROOT_MARKER = "source_path does not exist"


def is_absent_root_error(exc: BaseException) -> bool:
    """Whether *exc* (or anything it was raised from) is a delegated backend's
    "this manifest root does not exist" answer.

    Narrow on purpose: it must carry BOTH the 400 ``status`` and the marker in
    its ``detail`` (or message). Every other failure -- unreachable, 5xx,
    401/403, another 400 -- is not an absent root and must keep raising, so an
    outage never reads as "empty".
    """
    seen = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "status", None) == ABSENT_ROOT_STATUS:
            detail = getattr(current, "detail", None)
            text = (
                detail.decode("utf-8", "replace")
                if isinstance(detail, (bytes, bytearray))
                else str(current)
            )
            if ABSENT_ROOT_MARKER in text:
                return True
        current = current.__cause__ or current.__context__
    return False


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
    #: The file's modification time in integer nanoseconds, from the fetch
    #: frame header when the backend sends one. None when unknown (an error
    #: record, or a backend predating the field): callers read it with
    #: ``fetched_mtime_ns``, since a plugin's own record type may not carry the
    #: attribute at all.
    mtime_ns: Optional[int] = None


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
        prune_dirs: Optional[Sequence[str]] = None,
        hash: bool = True,
        prefix: Optional[str] = None,
        max_depth: Optional[int] = None,
    ) -> ContentPage:
        """One page of the repository's files, in a stable order.

        Paging is cursor-based rather than offset-based so a file appearing or
        vanishing mid-walk cannot make the traversal skip or repeat an unrelated
        entry.

        ``prune_dirs`` are directory NAMES the caller will discard anyway,
        skipped at any depth exactly as ``.git`` is -- never read, never hashed.
        The same list must be sent on every page of one manifest.

        ``hash``, ``prefix`` and ``max_depth`` are the optional manifest
        options (see :data:`MANIFEST_OPTIONS`). They are only ever passed to a
        backend that advertises them.
        """
        ...

    def fetch(
        self, repo_path: Union[str, Path], paths: Sequence[str]
    ) -> Iterator[FetchedFile]:
        """Yield the named files' bytes, lazily, in the order requested."""
        ...


@dataclass(frozen=True)
class ContentSeamSource:
    """Where a golden repo's files are read when they are not on this pod:
    the configured (non-local) backend, and the root that backend knows.

    ``version`` names the published golden repo version the files belong to:
    listings are cached under it until the next refresh/publish. None -- an
    activated copy, a write session, anything mutable -- caches for a short
    time only.
    """

    backend: ContentBackend
    root: str
    version: Optional[str] = None


class ManifestDeadlineExceeded(TimeoutError):
    """The caller's deadline passed before the manifest's last page arrived."""


class ManifestScopeIgnored(RuntimeError):
    """The backend was sent ``prefix``/``max_depth`` and did not honour them."""


#: Suffix of every :class:`DelegatedRepositoryUnsupportedError` message.
UNSUPPORTED_SUFFIX = (
    "is not supported with content_backend='delegated': this pod does not hold "
    "the repository"
)


class DelegatedRepositoryUnsupportedError(RuntimeError):
    """A feature that reads the repository from this pod's disk, on a pod
    whose content backend is delegated (the repository is elsewhere)."""

    def __init__(self, feature: str, reason: str) -> None:
        super().__init__(f"{feature} {UNSUPPORTED_SUFFIX} ({reason})")
        self.feature = feature
        self.reason = reason
