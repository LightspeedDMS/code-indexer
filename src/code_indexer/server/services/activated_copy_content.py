"""An activated working copy's files and history for the REST surfaces, read
through the content and git seams when the copy is not on this pod.

With ``content_backend: delegated`` a user's activated copy lives on
workspace-mcp's storage at ``<activated_repos_dir>/<user>/<alias>`` -- the same
absolute path the registry names -- and this pod holds only the copy's index
state (``index_root(copy)``). The REST routes that used to walk, stat and read
the copy on this disk (``/api/repos/{alias}``, ``/api/repos/{alias}/files``,
``/api/repositories/{id}``, ``/api/repositories/{id}/files?content=true`` and
``/api/repositories/{id}/stats``) ask these helpers instead:

* files, sizes and modification times come from the content backend's
  manifest (which never lists ``.git``);
* a file's bytes come from one ``fetch``;
* commits come from the git seam (git-mcp), never local git;
* the copy's index state is this pod's own directory, read where it is.

Every helper raises on a backend failure (:class:`CopyContentUnavailableError`
or the git seam's own error): a route reports it, never an empty repository.
Callers check the registry for THIS user before calling any of them.

With a local content backend :func:`delegated_copy_source` is None and the
routes keep their disk walks unchanged.
"""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from code_indexer.server.services.file_service import (
    ContentSeamSource,
    content_seam_source,
    fetch_one,
    manifest_entries,
)
from code_indexer.utils.index_store import INDEX_DIRNAME, index_root

_CONSUMER = "activated copy REST"

#: Directory names the manifest is asked to skip: the copy's index state is
#: this pod's, never workspace-mcp's (``.git`` is never listed at all).
_PRUNE_DIRS: Tuple[str, ...] = (INDEX_DIRNAME,)


class CopyContentUnavailableError(RuntimeError):
    """The content backend could not describe or read an activated copy."""


class CopyPathError(LookupError):
    """A caller-supplied path inside a copy names nothing usable.

    ``kind`` is ``"missing"`` (nothing at that path) or ``"not_dir"`` /
    ``"not_file"`` (something of the other kind is there).
    """

    def __init__(self, kind: str, path: str) -> None:
        super().__init__(f"{kind}: {path}")
        self.kind = kind
        self.path = path


@dataclass(frozen=True)
class CopyFile:
    """One file of a copy, relative to the copy's root (POSIX form)."""

    path: str
    size: int
    mtime_ns: Optional[int]

    @property
    def name(self) -> str:
        return posixpath.basename(self.path)


def delegated_copy_source(
    repo_path: Union[str, Path],
) -> Optional[ContentSeamSource]:
    """The seam that holds *repo_path*'s files, or None to read this disk."""
    return content_seam_source(str(repo_path), consumer=_CONSUMER)


def working_tree_files(source: ContentSeamSource) -> List[CopyFile]:
    """Every file of the copy's working tree (no ``.git``, no index state)."""
    try:
        entries = manifest_entries(source, _PRUNE_DIRS)
    except Exception as e:
        raise CopyContentUnavailableError(
            f"Could not list the repository at {source.root}: {type(e).__name__}: {e}"
        ) from e
    return [CopyFile(e.path, e.size, e.mtime_ns) for e in entries]


def pod_index_files(repo_path: Union[str, Path]) -> Optional[List[CopyFile]]:
    """The copy's index state on THIS pod, relative to the copy's root, or
    None when this pod holds none. Pod state, not repository content."""
    root = index_root(repo_path)
    if not root.is_dir():
        return None
    base = Path(repo_path)
    files: List[CopyFile] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            path = Path(dirpath) / filename
            try:
                stat = path.stat()
            except OSError:
                continue
            rel = path.relative_to(base).as_posix()
            files.append(CopyFile(rel, stat.st_size, stat.st_mtime_ns))
    return files


def copy_files_with_index(
    source: ContentSeamSource, repo_path: Union[str, Path]
) -> List[CopyFile]:
    """What a local walk of the copy outside ``.git`` sees: the working tree
    and the copy's index state (on this pod)."""
    return working_tree_files(source) + (pod_index_files(repo_path) or [])


def visible_files(
    source: ContentSeamSource, repo_path: Union[str, Path]
) -> List[CopyFile]:
    """The files the local repository walks count: everything outside
    ``.git`` (index state included) whose name does not start with a dot."""
    return [
        f
        for f in copy_files_with_index(source, repo_path)
        if not f.name.startswith(".")
    ]


def storage_info_mb(
    source: ContentSeamSource, repo_path: Union[str, Path]
) -> Dict[str, float]:
    """``storage_info`` of ``/api/repos/{alias}`` as the local walk computes
    it: ``disk_usage_mb`` over :func:`visible_files`, and ``index_size_mb``
    over every index file when the copy has index state on this pod."""
    index = pod_index_files(repo_path)
    tree = [f for f in working_tree_files(source) if not f.name.startswith(".")]
    counted = tree + [f for f in index or [] if not f.name.startswith(".")]
    info = {"disk_usage_mb": round(sum(f.size for f in counted) / (1024 * 1024), 2)}
    if index is not None:
        info["index_size_mb"] = round(sum(f.size for f in index) / (1024 * 1024), 2)
    return info


def recent_commits(repo_path: str, count: int) -> List[Tuple[str, str]]:
    """``git log --oneline -<count>`` of the copy as (abbreviated hash,
    subject), through the git seam. No delegated git backend is an error --
    never local git against a path this pod does not hold."""
    from code_indexer.server.startup.git_backend_wiring import (
        server_delegated_git_backend,
    )

    backend = server_delegated_git_backend()
    if backend is None:
        raise CopyContentUnavailableError(
            "Repositories are not on this server (content_backend='delegated') "
            "but git runs locally; set git_backend='delegated'."
        )
    raw = backend.log_raw(repo_path, "%h %s", max_count=count)
    commits: List[Tuple[str, str]] = []
    for line in raw.decode("utf-8", errors="replace").strip().split("\n"):
        parts = line.strip().split(" ", 1)
        if len(parts) >= 2:
            commits.append((parts[0], parts[1]))
    return commits


def list_copy_directory(
    source: ContentSeamSource,
    rel: str,
    *,
    recursive: bool,
    max_depth: Optional[int],
    limit: int,
) -> Dict[str, Any]:
    """``/api/repos/{alias}/files`` over the manifest.

    *rel* is the confined, repo-relative directory ("" for the root). The
    shape matches the local route's: one level (``files``), or the subtree
    walked top-down -- subdirectories then files of each directory, sorted --
    with ``count`` and ``truncated``. Directories are those holding a file
    (a manifest lists files, so an empty directory is not shown).

    Raises:
        CopyPathError: *rel* is missing, or is a file.
        CopyContentUnavailableError: the backend failed.
    """
    files = working_tree_files(source)
    dirs, children = _tree(files)
    if rel and rel not in dirs:
        kind = "not_dir" if any(f.path == rel for f in files) else "missing"
        raise CopyPathError(kind, rel)
    if not recursive:
        subdirs, here = children.get(rel, (set(), []))
        entries = [_dir_entry(rel, name) for name in sorted(subdirs)]
        entries += [_file_entry(f) for f in sorted(here, key=lambda f: f.path)]
        return {"files": entries}
    entries = []
    truncated = False
    stack = [(rel, 0)]
    while stack and not truncated:
        current, depth = stack.pop()
        if max_depth is not None and depth >= max_depth:
            continue
        subdirs, here = children.get(current, (set(), []))
        ordered_dirs = sorted(subdirs)
        for name in ordered_dirs:
            if len(entries) >= limit:
                truncated = True
                break
            entries.append(_dir_entry(current, name))
        if truncated:
            break
        for f in sorted(here, key=lambda f: f.name):
            if len(entries) >= limit:
                truncated = True
                break
            entries.append(_file_entry(f))
        if truncated:
            break
        # Visit children in sorted order: push in reverse onto the stack.
        for name in reversed(ordered_dirs):
            stack.append((_join(current, name), depth + 1))
    return {"files": entries, "count": len(entries), "truncated": truncated}


def read_copy_file(source: ContentSeamSource, rel: str) -> bytes:
    """One file's bytes from the copy.

    Raises:
        CopyPathError: *rel* is missing, or is a directory.
        CopyContentUnavailableError: the backend failed.
    """
    files = working_tree_files(source)
    if not any(f.path == rel for f in files):
        prefix = rel + "/"
        kind = (
            "not_file" if any(f.path.startswith(prefix) for f in files) else "missing"
        )
        raise CopyPathError(kind, rel)
    try:
        fetched = fetch_one(source, rel)
    except Exception as e:
        raise CopyContentUnavailableError(
            f"Could not read {rel} from {source.root}: {type(e).__name__}: {e}"
        ) from e
    if fetched.error is not None:
        raise CopyContentUnavailableError(
            f"Could not read {rel} from {source.root}: {fetched.error}"
        )
    return fetched.data


def decode_text_like_read_text(data: bytes) -> str:
    """``Path.read_text(encoding="utf-8")`` of these bytes: strict UTF-8 and
    universal newlines (``\\r\\n`` and ``\\r`` read as ``\\n``)."""
    return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def activation_created_at(record: Dict[str, Any]) -> datetime:
    """When the copy was created, from the registry's activation record.

    The local route reads the copy directory's ctime; a delegated pod has no
    such directory, and the activation time is that moment. A record without
    a usable ``activated_at`` raises: never "now" in place of an answer.
    """
    raw = record.get("activated_at")
    if isinstance(raw, datetime):
        value = raw
    elif isinstance(raw, str) and raw:
        try:
            value = datetime.fromisoformat(raw)
        except ValueError as e:
            raise CopyContentUnavailableError(
                f"Activation record has an unreadable activated_at {raw!r}"
            ) from e
    else:
        raise CopyContentUnavailableError("Activation record has no activated_at")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _join(parent: str, name: str) -> str:
    return f"{parent}/{name}" if parent else name


def _dir_entry(parent: str, name: str) -> Dict[str, Any]:
    return {"name": name, "path": _join(parent, name), "type": "dir", "size": 0}


def _file_entry(f: CopyFile) -> Dict[str, Any]:
    return {"name": f.name, "path": f.path, "type": "file", "size": f.size}


def _tree(
    files: Sequence[CopyFile],
) -> Tuple[set, Dict[str, Tuple[set, List[CopyFile]]]]:
    """(every directory, directory -> (subdirectory names, files in it))."""
    dirs: set = set()
    children: Dict[str, Tuple[set, List[CopyFile]]] = {}
    for f in files:
        parent = posixpath.dirname(f.path)
        children.setdefault(parent, (set(), []))[1].append(f)
        while parent:
            dirs.add(parent)
            grand = posixpath.dirname(parent)
            children.setdefault(grand, (set(), []))[0].add(posixpath.basename(parent))
            parent = grand
    return dirs, children
