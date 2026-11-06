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

import errno
import logging
import os
from pathlib import Path
from typing import (
    FrozenSet,
    cast,
    Iterator,
    Optional,
    Sequence,
    Union,
)


# The seam's types and contract constants live in the SDK (modular plan
# phase 5); re-exported so existing imports keep resolving to the same objects.
from cidx_sdk.seams.content import (
    ABSENT_ROOT_MARKER as ABSENT_ROOT_MARKER,
    ABSENT_ROOT_STATUS as ABSENT_ROOT_STATUS,
    DEFAULT_PAGE_SIZE as DEFAULT_PAGE_SIZE,
    MANIFEST_OPTION_HASH as MANIFEST_OPTION_HASH,
    MANIFEST_OPTION_MAX_DEPTH as MANIFEST_OPTION_MAX_DEPTH,
    MANIFEST_OPTION_PREFIX as MANIFEST_OPTION_PREFIX,
    MANIFEST_OPTIONS as MANIFEST_OPTIONS,
    NOT_FOUND_FETCH_ERRORS as NOT_FOUND_FETCH_ERRORS,
    REFUSED_ABSOLUTE_PATH as REFUSED_ABSOLUTE_PATH,
    REFUSED_CURRENT_COMPONENT as REFUSED_CURRENT_COMPONENT,
    REFUSED_EMPTY_PATH as REFUSED_EMPTY_PATH,
    REFUSED_ESCAPE as REFUSED_ESCAPE,
    REFUSED_FETCH_ERRORS as REFUSED_FETCH_ERRORS,
    REFUSED_PARENT_COMPONENT as REFUSED_PARENT_COMPONENT,
    REFUSED_SYMLINK as REFUSED_SYMLINK,
    REFUSED_SYMLINKED_DIRECTORY as REFUSED_SYMLINKED_DIRECTORY,
    UNREADABLE_FETCH_ERRORS as UNREADABLE_FETCH_ERRORS,
    ContentBackend as ContentBackend,
    ContentEntry as ContentEntry,
    ContentPage as ContentPage,
    FetchedFile as FetchedFile,
    ManifestScopeIgnored as ManifestScopeIgnored,
    entry_in_scope as entry_in_scope,
    is_absent_root_error as is_absent_root_error,
    normalize_manifest_prefix as normalize_manifest_prefix,
)

# The local implementation lives in its own distribution
# (packages/backends/cidx-backend-workspace) and is selected by the seam
# resolver under the entry name ``local``; re-exported for existing imports.
from cidx_backend_workspace.content import (
    LocalContentBackend as LocalContentBackend,
)

logger = logging.getLogger(__name__)

# ``ABSENT_ROOT_STATUS`` / ``ABSENT_ROOT_MARKER``: how workspace-mcp answers a
# manifest whose ROOT does not exist (HTTP 400, ``source_path does not exist:
# <path>``). The delegated plugin raises that as an error carrying ``status``
# and the response ``detail``. ``LocalContentBackend`` lists such a root as
# empty; see :func:`is_absent_root_error`.


def fetched_mtime_ns(fetched: object) -> Optional[int]:
    """A fetch record's ``mtime_ns``, or None when the backend sent none.

    Read with ``getattr``: a delegated plugin's record is duck-typed and one
    predating the field has no such attribute. Anything but an int is unknown.
    """
    value = getattr(fetched, "mtime_ns", None)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def is_directory_fetch_error(error: Optional[str]) -> bool:
    """Whether a per-file fetch error means "that path is a directory"."""
    return error in _DIRECTORY_FETCH_ERRORS


def is_not_found_fetch_error(error: Optional[str]) -> bool:
    """Whether a per-file fetch error means "no such file is served": absent
    (:data:`NOT_FOUND_FETCH_ERRORS`) or a path the protocol refuses to read
    (:data:`REFUSED_FETCH_ERRORS`, e.g. a symlink, never followed)."""
    return error in NOT_FOUND_FETCH_ERRORS or error in REFUSED_FETCH_ERRORS


#: Per-file fetch errors that mean the requested path is a directory.
_DIRECTORY_FETCH_ERRORS = frozenset({"EISDIR", os.strerror(errno.EISDIR)})


# ── manifest options ─────────────────────────────────────────────────────────
#
# Three optional manifest request fields, beyond the base protocol, that turn a
# whole-repository walk into the walk a caller actually needs:
#
# * ``hash``      False skips the per-file blob hash -- the cost of a manifest
#                 is reading every byte to hash it, and a listing needs none.
# * ``prefix``    a repo-relative directory: only entries beneath it. Paths in
#                 the answer stay repo-relative, and so does the cursor.
# * ``max_depth`` only entries at most this many levels below ``prefix``
#                 (1 = the files directly inside it).
#
# A backend ADVERTISES them in one of two ways: a ``manifest_options``
# attribute naming the ones it honours (``LocalContentBackend``), or -- the
# delegated plugin from cidx-content-backend 0.11.0 -- a
# ``manifest_options_supported`` attribute (None = not yet known, True, False)
# together with a ``manifest()`` that takes the option keywords. One that
# advertises nothing gets none of them and keeps working, exactly as before.
#
# Support is also VERIFIED on every answer. The plugin reports a page it had
# to produce by walking and filtering itself (``emulated``: workspace-mcp
# predates the options), and any server that ignores ``prefix``/``max_depth``
# returns entries outside the scope. Either way the backend is treated as not
# honouring them for the rest of the process, and :func:`query_manifest_pages`
# raises :class:`ManifestScopeIgnored` so a scoped caller can answer from the
# cached whole manifest instead of paying an uncached walk.
#
# Entries are FILES (and symlinks). A directory entry -- the plugin reports a
# directory at the ``max_depth`` limit as ``is_dir`` -- is dropped here, so
# every backend's view has the same shape.

#: Backends (by id) whose answers proved they ignore ``prefix``/``max_depth``.
_IGNORES_SCOPE_OPTIONS: "set[int]" = set()


def manifest_options_supported(backend: object) -> FrozenSet[str]:
    """The manifest options *backend* advertises and has not been caught ignoring."""
    declared = getattr(backend, "manifest_options", None)
    if isinstance(declared, (set, frozenset, tuple, list)):
        supported = frozenset(str(name) for name in declared) & MANIFEST_OPTIONS
    else:
        supported = _keyword_options(backend)
    if id(backend) in _IGNORES_SCOPE_OPTIONS:
        supported -= {MANIFEST_OPTION_PREFIX, MANIFEST_OPTION_MAX_DEPTH}
    return supported


def _keyword_options(backend: object) -> FrozenSet[str]:
    """The options a plugin backend takes as ``manifest()`` keywords, unless
    its ``manifest_options_supported`` says the server cannot honour them."""
    if not hasattr(backend, "manifest_options_supported"):
        return frozenset()
    if getattr(backend, "manifest_options_supported") is False:
        return frozenset({MANIFEST_OPTION_HASH}) & _manifest_keywords(backend)
    return _manifest_keywords(backend)


def _manifest_keywords(backend: object) -> FrozenSet[str]:
    import inspect

    try:
        parameters = inspect.signature(getattr(backend, "manifest")).parameters
    except (AttributeError, TypeError, ValueError):
        return frozenset()
    return frozenset(parameters) & MANIFEST_OPTIONS


def supports_scoped_manifest(backend: object) -> bool:
    """Whether *backend* can list one directory without walking the repository."""
    return {MANIFEST_OPTION_PREFIX, MANIFEST_OPTION_MAX_DEPTH} <= (
        manifest_options_supported(backend)
    )


def query_manifest_pages(
    backend: ContentBackend,
    root: Union[str, Path],
    *,
    prune_dirs: Sequence[str] = (),
    prefix: str = "",
    max_depth: Optional[int] = None,
    want_hash: bool = True,
    page_size: int = DEFAULT_PAGE_SIZE,
    deadline: Optional[float] = None,
) -> Iterator[ContentPage]:
    """Every page of the manifest view ``prefix``/``max_depth``, filtered.

    The options *backend* honours are sent; the rest are applied here, on
    every page, so the entries yielded are exactly the view asked for whatever
    the server did. A server caught ignoring ``prefix``/``max_depth`` it was
    sent is remembered (see :func:`manifest_options_supported`), warned about
    once, and raises :class:`ManifestScopeIgnored`; asked again, it is no
    longer sent them and the view is filtered here.

    ``deadline`` is a ``time.monotonic()`` instant checked before each page is
    requested (``TimeoutError``). A cursor that does not advance raises rather
    than looping on a misbehaving backend.
    """
    import time

    prefix = normalize_manifest_prefix(prefix)
    if max_depth is not None and max_depth < 1:
        raise ValueError("max_depth must be at least 1")
    supported = manifest_options_supported(backend)
    options: dict = {}
    if not want_hash and MANIFEST_OPTION_HASH in supported:
        options[MANIFEST_OPTION_HASH] = False
    scoped = bool(prefix) or max_depth is not None
    if prefix and MANIFEST_OPTION_PREFIX in supported:
        options[MANIFEST_OPTION_PREFIX] = prefix
    if max_depth is not None and MANIFEST_OPTION_MAX_DEPTH in supported:
        options[MANIFEST_OPTION_MAX_DEPTH] = max_depth
    sent_scope = MANIFEST_OPTION_PREFIX in options or (
        MANIFEST_OPTION_MAX_DEPTH in options
    )
    prune = tuple(sorted(set(prune_dirs)))
    cursor: Optional[str] = None
    while True:
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError("content manifest walk passed its deadline")
        page = backend.manifest(
            root,
            cursor=cursor,
            page_size=page_size,
            prune_dirs=prune,
            **options,
        )
        entries = tuple(e for e in page.entries if not getattr(e, "is_dir", False))
        if scoped:
            kept = tuple(
                e for e in entries if entry_in_scope(e.path, prefix, max_depth)
            )
            if sent_scope and (
                getattr(page, "emulated", False) is True or len(kept) != len(entries)
            ):
                _note_scope_options_ignored(backend)
                raise ManifestScopeIgnored(
                    f"{type(backend).__name__} did not honour the manifest "
                    "prefix/max_depth"
                )
            entries = kept
        yield ContentPage(entries=entries, next_cursor=page.next_cursor)
        if page.next_cursor is None:
            return
        if page.next_cursor == cursor:
            raise RuntimeError(
                "content manifest cursor did not advance; refusing to loop"
            )
        cursor = page.next_cursor


def _note_scope_options_ignored(backend: object) -> None:
    if id(backend) in _IGNORES_SCOPE_OPTIONS:
        return
    _IGNORES_SCOPE_OPTIONS.add(id(backend))
    logger.warning(
        "Content backend %s advertises manifest prefix/max_depth but did not "
        "honour them (the server predates the options); listings fall back to "
        "the cached whole manifest",
        type(backend).__name__,
    )


# The REFUSED_* reasons (why a path is refused rather than read) are part of
# the seam contract and live in ``cidx_sdk.seams.content``. A refused path is
# answered as an absent file by every reader: the protocol never serves it.


class ContentBackendFactory:
    """Create a ContentBackend from a backend name.

    Mirrors ``GitBackendFactory`` and ``RipgrepBackendFactory``. ``local`` is
    the module installed for the content seam's ``local`` mode, reached through
    the seam resolver (``cidx-backend-workspace``); with none installed it
    raises, naming the seam. ``delegated`` must be injected by the plugin seam
    and raises otherwise, so a misconfiguration fails loudly instead of
    silently reading the mount -- the whole point of delegating is that the
    mount may not be there, and a silent fallback would surface as an empty
    repository rather than an error.
    """

    @staticmethod
    def create(
        backend: str = "local",
        *,
        delegated_backend: Optional[ContentBackend] = None,
    ) -> ContentBackend:
        if backend == "local":
            from code_indexer.server.services.content_backend_plugin import (
                resolve_content_backend,
            )

            return cast(ContentBackend, resolve_content_backend("local"))
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
