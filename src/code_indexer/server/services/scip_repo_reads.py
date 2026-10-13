"""SCIP's repository reads, through the seams on a pod without the repository.

Delegating the SCIP *build* (``scip_backend=delegated-plugin``) moved the
compile step off the pod, but three questions around it still went to the local
disk: which projects the repository holds (discovery), whether a root build file
exists (the freshness and presence shape check), and which commit HEAD is at
(freshness and the build-queue dedupe key). On a mount-less pod those read a
directory holding only cidx's own index state, and answered "no projects",
"no root build file" and "no commit" -- wrong answers, not errors.

Here each question goes where the repository is:

* files: the content backend (workspace-mcp) -- ``manifest`` for the listing,
  ``fetch`` for a ``pom.xml``'s text and for the root build-file probe;
* HEAD: the git backend (git-mcp) -- ``rev_parse(repo, "HEAD")``.

Selected by the content seam alone: when the server reads repository files
through a NON-local content backend, the repository is not on this pod, so SCIP
must not read it either. Otherwise every answer is the local one, from the same
code that answered it before (``ProjectDiscovery``'s ``LocalBuildFileSource``,
``index_freshness.current_commit``).

FAIL LOUD: a delegated content seam with no delegated git backend, a git backend
that cannot resolve HEAD, or a content backend transport error raises
(``ScipRepoReadError`` / the backend's own error). Callers already turn an
exception into a logged skip; what must never happen is the old silent answer.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple, Union

from code_indexer.indexing.repo_manifest import accepts_prune_dirs
from code_indexer.scip.discovery import (
    DISCOVERY_PRUNE_DIRS,
    BUILD_FILE_MAPPINGS,
    GRADLE_SETTINGS_FILES,
    BuildFileSource,
    has_root_build_file,
)
from code_indexer.server.storage.shared.content_backend import (
    UNREADABLE_FETCH_ERRORS,
    ContentBackend,
)

logger = logging.getLogger(__name__)

#: How this module names itself to ``golden_repo_content_root``.
CONSUMER = "scip"
#: Root-level build-file names that can be probed by name (one fetch for all).
ROOT_PROBE_NAMES: Tuple[str, ...] = GRADLE_SETTINGS_FILES + tuple(
    name for name in BUILD_FILE_MAPPINGS if "*" not in name
)
#: (content root, HEAD) -> has a root build file. Immutable per commit.
_ROOT_CACHE_MAX = 4096


class ScipRepoReadError(RuntimeError):
    """A SCIP repository read that the seams could not answer."""


# ── selection ────────────────────────────────────────────────────────────────


def delegated_content_backend() -> Optional[ContentBackend]:
    """The server's content backend when it is NOT local, else None."""
    from code_indexer.server.startup.content_backend_wiring import (
        server_content_backend,
    )

    backend = server_content_backend()
    if backend is None or backend.is_local:
        return None
    return backend


def _content_root(repo_root: Union[str, Path]) -> Path:
    from code_indexer.server.startup.content_backend_wiring import (
        golden_repo_content_root,
    )

    return golden_repo_content_root(repo_root, consumer=CONSUMER)


def build_file_source(repo_root: Union[str, Path]) -> Optional[BuildFileSource]:
    """The source SCIP discovery must use for *repo_root*; None means local disk."""
    backend = delegated_content_backend()
    if backend is None:
        return None
    return ContentBuildFileSource(backend, Path(repo_root), _content_root(repo_root))


# ── HEAD ─────────────────────────────────────────────────────────────────────


def head_commit(
    repo_root: Union[str, Path], *, local_reader: Callable[[Path], Optional[str]]
) -> Optional[str]:
    """HEAD's commit for SCIP freshness / dedupe.

    Local content seam: ``local_reader(repo_root)``, exactly as before (it may
    answer None). Delegated: the git backend's answer, or ``ScipRepoReadError``
    -- never None, because "no commit" there means "could not ask", not "no
    history", and freshness must not silently read it as stale.
    """
    if delegated_content_backend() is None:
        return local_reader(Path(repo_root))
    return _delegated_head_commit(repo_root)


def _delegated_head_commit(repo_root: Union[str, Path]) -> str:
    from code_indexer.server.startup.git_backend_wiring import (
        server_delegated_git_backend,
    )

    git = server_delegated_git_backend()
    if git is None:
        raise ScipRepoReadError(
            "content_backend is delegated (the repository is not on this pod) but "
            "git_backend is local: SCIP cannot read HEAD of "
            f"{repo_root}. Configure git_backend=delegated (git-mcp)."
        )
    target = _content_root(repo_root)
    try:
        commit = str(git.rev_parse(str(target), "HEAD")).strip()
    except Exception as exc:  # the backend's transport, whatever it is
        raise ScipRepoReadError(
            f"git backend could not resolve HEAD of {target}: {exc}"
        ) from exc
    if not commit:
        raise ScipRepoReadError(f"git backend returned no HEAD commit for {target}")
    return commit


# ── root build file ──────────────────────────────────────────────────────────

_root_cache: "OrderedDict[Tuple[str, str], bool]" = OrderedDict()
_root_cache_lock = threading.Lock()


def repo_has_root_build_file(repo_root: Union[str, Path]) -> bool:
    """``discovery.has_root_build_file`` asked of wherever the repository is.

    Delegated answers are cached per (repository, HEAD): the build files at one
    commit never change, and the backfill sweep asks every repo every cycle.
    """
    backend = delegated_content_backend()
    if backend is None:
        return has_root_build_file(Path(repo_root))
    commit = _delegated_head_commit(repo_root)
    key = (str(_content_root(repo_root)), commit)
    with _root_cache_lock:
        if key in _root_cache:
            _root_cache.move_to_end(key)
            return _root_cache[key]
    source = ContentBuildFileSource(backend, Path(repo_root), _content_root(repo_root))
    answer = has_root_build_file(Path(repo_root), source)
    with _root_cache_lock:
        _root_cache[key] = answer
        while len(_root_cache) > _ROOT_CACHE_MAX:
            _root_cache.popitem(last=False)
    return answer


def reset_cache() -> None:
    """Forget cached root build-file answers. For tests."""
    with _root_cache_lock:
        _root_cache.clear()


# ── the content-seam source ──────────────────────────────────────────────────


class ContentBuildFileSource:
    """``BuildFileSource`` over a content backend: nothing is read on this pod.

    * ``iter_files``: one paged ``manifest`` (``.git``/index dirs pruned, also
      filtered here for a backend that cannot prune), kept for this source's
      lifetime so ``exists``/``root_glob`` after a walk cost nothing;
    * ``exists`` of a root-level build-file name before any walk: ONE ``fetch``
      of every such name, so the root shape check usually needs no manifest;
    * ``read_text``: ``fetch`` of that one file.

    Paths in and out are under ``repo_root`` (what discovery and its callers
    know); requests go to ``content_root`` (where the backend sees it).
    """

    def __init__(
        self, backend: ContentBackend, repo_root: Path, content_root: Path
    ) -> None:
        self._backend = backend
        self.repo_root = Path(repo_root)
        self._content_root = Path(content_root)
        self._listing: Optional[List[str]] = None
        self._listing_set: Set[str] = set()
        self._root_probe: Optional[Dict[str, bool]] = None

    # -- BuildFileSource ----------------------------------------------------

    def iter_files(self) -> Iterator[Path]:
        for rel in self._list():
            yield self.repo_root / rel

    def exists(self, path: Path) -> bool:
        rel = self._rel(path)
        if rel is None:
            return False
        if self._listing is None and rel in ROOT_PROBE_NAMES:
            return self._probe_root()[rel]
        self._list()
        return rel in self._listing_set

    def root_glob(self, pattern: str) -> bool:
        return any(
            "/" not in rel and PurePosixPath(rel).match(pattern) for rel in self._list()
        )

    def read_text(self, path: Path) -> Optional[str]:
        rel = self._rel(path)
        if rel is None:
            return None
        for fetched in self._backend.fetch(self._content_root, [rel]):
            if fetched.error is not None:
                return None
            return fetched.data.decode("utf-8", errors="ignore")
        return None

    # -- internals ----------------------------------------------------------

    def _rel(self, path: Path) -> Optional[str]:
        try:
            return Path(path).relative_to(self.repo_root).as_posix()
        except ValueError:
            return None

    def _list(self) -> List[str]:
        if self._listing is None:
            prune = sorted(DISCOVERY_PRUNE_DIRS)
            kwargs: Dict[str, Any] = (
                {"prune_dirs": prune} if accepts_prune_dirs(self._backend) else {}
            )
            listing: List[str] = []
            cursor: Optional[str] = None
            while True:
                page = self._backend.manifest(
                    self._content_root, cursor=cursor, **kwargs
                )
                listing.extend(
                    entry.path
                    for entry in page.entries
                    if not DISCOVERY_PRUNE_DIRS.intersection(entry.path.split("/")[:-1])
                )
                if page.next_cursor is None:
                    break
                if page.next_cursor == cursor:
                    raise ScipRepoReadError(
                        f"content backend manifest of {self._content_root} did not "
                        f"advance past cursor {cursor!r}"
                    )
                cursor = page.next_cursor
            self._listing = listing
            self._listing_set = set(listing)
        return self._listing

    def _probe_root(self) -> Dict[str, bool]:
        if self._root_probe is None:
            present = {name: False for name in ROOT_PROBE_NAMES}
            for fetched in self._backend.fetch(
                self._content_root, list(ROOT_PROBE_NAMES)
            ):
                if fetched.path in present:
                    present[fetched.path] = (
                        fetched.error is None
                        or fetched.error in UNREADABLE_FETCH_ERRORS
                    )
            self._root_probe = present
        return self._root_probe
