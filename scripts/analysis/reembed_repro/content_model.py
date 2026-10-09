"""Content model of the repository on disk, independent of the indexer's state.

R-c (A6-core) is content-addressed: for every non-empty disk file with file
hash ``H`` and ``n`` chunks (current chunker), the ids
``md5(f"{project_id}_{H}_{i}")`` for ``i < n`` must exist in the store and
not be hidden on the current branch key. Duplicate-content files share those
ids, so the check passes for them on every version (one stored path per
content is the design rule). The same model yields each file's chunk
keys (``sha256(chunk text)``), the keys the provider embeds.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Set, Tuple


@dataclass(frozen=True)
class FileContent:
    file_hash: str  # "sha256:<hex>" of the file bytes, as the indexer records it
    keys: Tuple[str, ...]  # sha256(chunk text) per chunk, in chunk order


def expected_point_ids(project_id: str, content: FileContent) -> List[str]:
    """The indexer's content-addressed point ids for every chunk of ``content``."""
    return [
        hashlib.md5(f"{project_id}_{content.file_hash}_{i}".encode()).hexdigest()
        for i in range(len(content.keys))
    ]


def missing_content(
    files: Dict[str, FileContent],
    project_id: str,
    stored_ids: Set[str],
    hidden_ids: Set[str],
) -> List[str]:
    """Sorted paths whose content is not completely present and visible."""
    missing = []
    for path, content in files.items():
        ids = expected_point_ids(project_id, content)
        if any(pid not in stored_ids or pid in hidden_ids for pid in ids):
            missing.append(path)
    return sorted(missing)


def all_keys(files: Iterable[FileContent]) -> Set[str]:
    """Every chunk key of the given contents."""
    return {key for content in files for key in content.keys}


def project_id_for(repo: Path) -> str:
    """The indexer's project id: git origin basename, else the directory name.

    Mirrors ``FileIdentifier.get_project_id`` (lowercase, ``_`` -> ``-``).
    """
    name = repo.name
    if (repo / ".git").exists():
        origin = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
        )
        url = origin.stdout.strip()
        if origin.returncode == 0 and url:
            name = url.split("/")[-1]
            name = name[:-4] if name.endswith(".git") else name
    return name.lower().replace("_", "-")


ChunkFn = Callable[[Path], Tuple[str, ...]]


def make_chunk_fn(repo: Path) -> ChunkFn:
    """Chunk keys of a file with the chunker of the importable code_indexer tree.

    The harness puts the tree under test first on ``sys.path``, so this is the
    current chunker, configured from the repository's own config.
    """
    from code_indexer.config import ConfigManager
    from code_indexer.indexing.fixed_size_chunker import FixedSizeChunker

    chunker = FixedSizeChunker(
        ConfigManager(repo / ".code-indexer" / "config.json").load()
    )
    root = repo.resolve()

    def chunk_keys(path: Path) -> Tuple[str, ...]:
        return tuple(
            hashlib.sha256(chunk["text"].encode("utf-8")).hexdigest()
            for chunk in chunker.chunk_file(path.resolve(), root)
        )

    return chunk_keys


class ContentModel:
    """Per-path FileContent of the files on disk, cached by stat."""

    def __init__(self, repo: Path, chunk_fn: ChunkFn) -> None:
        self.repo = repo
        self.chunk_fn = chunk_fn
        self._cache: Dict[str, Tuple[Tuple[int, int, int], FileContent]] = {}

    def files(self, rel_paths: Iterable[str]) -> Dict[str, FileContent]:
        """FileContent of every listed file that has at least one chunk."""
        result: Dict[str, FileContent] = {}
        for rel in rel_paths:
            path = self.repo / rel
            st = path.stat()
            stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
            cached = self._cache.get(rel)
            if cached is None or cached[0] != stamp:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                cached = (stamp, FileContent(f"sha256:{digest}", self.chunk_fn(path)))
                self._cache[rel] = cached
            if cached[1].keys:
                result[rel] = cached[1]
        return result
