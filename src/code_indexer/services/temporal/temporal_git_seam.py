"""Temporal history read through the git seam (git-mcp).

Temporal indexing walks a repository's history: ``git log`` for the commits,
then one ``git diff`` per commit. Temporal queries resolve ``at_commit`` and,
for legacy pointer payloads, read a file at a commit. On a pod that does not
hold the repository none of that can run locally. With a git backend it is
asked of git-mcp instead -- and in pages and batches, never one round trip per
commit or per file:

* history: ``HISTORY_PAGE_COMMITS`` commits per ``log_raw`` call;
* diffs: ``DIFF_PAGE_COMMITS`` commits per ``diff_commits`` call;
* file reads: every result of a query in one ``show_objects`` call.

The backend returns git's raw stdout bytes, decoded here exactly as the local
``subprocess.run(text=True, errors="replace")`` decodes them, and parsed by the
same parsers -- so a delegated run produces the commit documents a local run
does. With no backend none of this is used: the local code paths are unchanged.
"""

from __future__ import annotations

import io
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from ...server.storage.shared.git_backend import head_branch
from .commit_aggregator import (
    COMMIT_LOG_FORMAT,
    FileChange,
    diff_base_ref,
    parse_commit_log,
    parse_diff_output,
)
from .models import CommitInfo

#: Commits per history page. Metadata only, so a page stays small.
HISTORY_PAGE_COMMITS = 2000

#: Commits per diff batch. Diffs can be large; this bounds one response.
DIFF_PAGE_COMMITS = 16

#: ``(rev, path)``: a file's content at a commit (``git show <rev>:<path>``).
ObjectSpec = Tuple[str, str]


def decode_like_local_git(raw: bytes) -> str:
    """Decode git output exactly as ``subprocess.run(text=True, errors="replace")``.

    That is what ``Popen`` does: the encoding of a ``TextIOWrapper`` opened
    with ``encoding="locale"``, invalid bytes replaced, then universal newlines
    (``\\r\\n`` and ``\\r`` become ``\\n``).
    """
    encoding = io.TextIOWrapper(io.BytesIO(), encoding="locale").encoding
    text = raw.decode(encoding, "replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def commit_history(
    backend: Any,
    repo_path: str,
    *,
    all_branches: bool,
    since_date: Optional[str],
    max_commits: Optional[int],
) -> List[CommitInfo]:
    """The commits ``git log --reverse`` lists, read a page at a time.

    ``--reverse`` applies after git's limits, so it cannot be paged; the pages
    walk newest-first with ``--skip`` and the result is reversed, which is the
    same list. Each call asks for one commit more than the page to learn
    whether another page follows, so N commits take ``ceil(N / page)`` calls.
    A single branch is pinned to HEAD's commit first, so a page boundary cannot
    shift if the branch moves mid-walk. ``--all`` cannot be pinned; a commit a
    moved ref pushes across a boundary is kept once.
    """
    revs = [] if all_branches else [str(backend.rev_parse(repo_path, "HEAD")).strip()]
    limit = max_commits or None  # local passes -n only for a truthy value
    newest_first: List[CommitInfo] = []
    seen = set()
    skip = 0
    more = True
    while more and (limit is None or len(newest_first) < limit):
        page_size = HISTORY_PAGE_COMMITS
        if limit is not None:
            page_size = min(page_size, limit - len(newest_first))
        raw = backend.log_raw(
            repo_path,
            COMMIT_LOG_FORMAT,
            revs,
            all_refs=all_branches,
            since=since_date,
            skip=skip,
            max_count=page_size + 1,
        )
        page = parse_commit_log(decode_like_local_git(raw))
        more = len(page) > page_size
        for commit in page[:page_size]:
            if commit.hash not in seen:
                seen.add(commit.hash)
                newest_first.append(commit)
        skip += page_size
    newest_first.reverse()
    return newest_first


def current_branch_name(backend: Any, repo_path: str) -> str:
    """``git branch --show-current`` or ``"HEAD"`` when detached."""
    return head_branch(backend, repo_path) or "HEAD"


class CommitDiffPages:
    """Each commit's file changes, fetched a page of commits per backend call.

    Indexing workers ask for one commit at a time, in queue order. The first
    request for any commit of a page fetches the whole page; later requests are
    served from it, and each diff is dropped once handed out, so at most a page
    or two is held.
    """

    def __init__(
        self,
        backend: Any,
        repo_path: str,
        commits: Sequence[CommitInfo],
        context_lines: int,
        page_size: Optional[int] = None,
    ) -> None:
        page_size = page_size or DIFF_PAGE_COMMITS
        self._backend = backend
        self._repo_path = repo_path
        self._context_lines = context_lines
        self._pages = [
            list(commits[i : i + page_size]) for i in range(0, len(commits), page_size)
        ]
        self._page_of = {
            commit.hash: index
            for index, page in enumerate(self._pages)
            for commit in page
        }
        self._locks = [threading.Lock() for _ in self._pages]
        self._fetched = [False] * len(self._pages)
        self._diffs: Dict[str, str] = {}

    def file_changes(self, commit: CommitInfo) -> List[FileChange]:
        index = self._page_of[commit.hash]
        with self._locks[index]:
            if not self._fetched[index]:
                self._fetch(self._pages[index])
                self._fetched[index] = True
            text = self._diffs.pop(commit.hash)
        return parse_diff_output(text)

    def _fetch(self, page: List[CommitInfo]) -> None:
        pairs = [(diff_base_ref(commit), commit.hash) for commit in page]
        outputs = self._backend.diff_commits(
            self._repo_path, pairs, self._context_lines
        )
        if len(outputs) != len(page):
            raise RuntimeError(
                f"git seam returned {len(outputs)} diffs for {len(page)} commits"
            )
        for commit, raw in zip(page, outputs):
            self._diffs[commit.hash] = decode_like_local_git(raw)


def commit_timestamp(backend: Any, repo_path: str, ref: str) -> int:
    """``resolve_commit_timestamp`` through the seam: the same answers and errors.

    ``rev-parse --verify --quiet`` says "no such commit" with exit 1 and no
    stderr (git-mcp: an empty answer). Any other failure -- git-mcp
    unreachable, a timeout -- says nothing about the ref and is reported as a
    failure to resolve, never as "not a commit".
    """
    try:
        resolved = str(
            backend.rev_parse(repo_path, "--verify", "--quiet", f"{ref}^{{commit}}")
        ).strip()
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 1 and not str(exc.stderr or "").strip():
            resolved = ""
        else:
            raise ValueError(f"at_commit '{ref}' could not be resolved: {exc}") from exc
    if not resolved:
        raise ValueError(
            f"at_commit '{ref}' does not resolve to a commit in this repository"
        )
    try:
        raw = backend.log_raw(repo_path, "%ct", [resolved], max_count=1)
    except subprocess.CalledProcessError as exc:
        raise ValueError(
            f"at_commit '{ref}' resolved to {resolved} but its commit "
            f"timestamp could not be read: {exc}"
        ) from exc
    ts_text = decode_like_local_git(raw).strip()
    try:
        return int(ts_text)
    except ValueError as exc:
        raise ValueError(
            f"at_commit '{ref}' resolved to {resolved} but returned a "
            f"non-numeric commit timestamp {ts_text!r}"
        ) from exc


def reconstruction_text(returncode: int, stdout: str, stderr: str) -> str:
    """What a pointer payload shows: the file, or why it is unavailable."""
    if returncode == 0:
        return stdout
    # Graceful error handling - truncate stderr to avoid log spam
    error_msg = stderr[:100] if stderr else "unknown error"
    return f"[Content unavailable - git error: {error_msg}]"


def read_files_at_commits(
    backend: Any, repo_path: str, specs: Sequence[ObjectSpec]
) -> Dict[ObjectSpec, str]:
    """Every ``(rev, path)``'s reconstruction text, in ONE backend call."""
    unique = list(dict.fromkeys(specs))
    if not unique:
        return {}
    results = backend.show_objects(repo_path, unique)
    return {
        spec: reconstruction_text(
            result.returncode,
            decode_like_local_git(bytes(result.stdout or b"")),
            str(result.stderr or ""),
        )
        for spec, result in zip(unique, results)
    }


def server_history_backend(
    project_root: Union[str, Path],
) -> Optional[Tuple[Any, str]]:
    """``(backend, repo_path)`` when a running server delegates git, else None.

    None outside a server, and on a server whose git runs locally: those keep
    local git exactly as before. A versioned snapshot path (index state only, on
    this pod) maps to the golden repo's base clone, which git-mcp can see.
    """
    from ...server.startup.content_backend_wiring import golden_repo_content_root
    from ...server.startup.git_backend_wiring import server_delegated_git_backend

    backend = server_delegated_git_backend()
    if backend is None:
        return None
    repo_path = golden_repo_content_root(project_root, consumer="temporal history")
    return backend, str(repo_path)
