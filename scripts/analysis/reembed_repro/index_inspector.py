"""Read-only inspection of a repository's index, metadata and progress output.

Only called between runs (no ``cidx`` child alive), and the chunk store is
opened read-only, so the inspection never changes what the indexer sees.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

import zstandard

METADATA_FILE = "metadata-voyage-ai.json"
_FILE_PROGRESS = re.compile(r"^(\d+)/(\d+) files\b")
_INSPECT_SCRATCH = Path.home() / ".tmp" / "reembed-repro" / "inspect"


@dataclass(frozen=True)
class IndexSnapshot:
    layout: str  # "chunks_db" | "none"
    path_counts: Dict[str, int]
    hot_journal: bool = False
    content_hashes: Set[str] = field(default_factory=set)
    point_ids: Set[str] = field(default_factory=set)
    #: point_id -> stored hidden_branches, only for rows hidden on some branch
    hidden: Dict[str, Tuple[str, ...]] = field(default_factory=dict)

    @property
    def points(self) -> int:
        return sum(self.path_counts.values())

    @property
    def paths(self) -> Set[str]:
        return set(self.path_counts)

    def points_for(self, paths: Set[str]) -> int:
        return sum(self.path_counts.get(p, 0) for p in paths)

    def hidden_ids_for(self, branch_key: Optional[str]) -> Set[str]:
        """Ids whose stored hidden_branches holds ``branch_key`` (none for non-git)."""
        if branch_key is None:
            return set()
        return {pid for pid, branches in self.hidden.items() if branch_key in branches}


@contextmanager
def _read_only(db: Path) -> Iterator[Tuple[sqlite3.Connection, bool]]:
    """Open ``db`` without changing any file; yields (connection, hot_journal).

    An interrupted writer may leave a hot rollback journal: even a read-only
    open must roll it back, which writes. Then a copy is read, so the files
    the next indexer run opens stay exactly as the interruption left them.
    """
    journal = db.with_name(db.name + "-journal")
    if journal.exists() and journal.stat().st_size > 0:
        _INSPECT_SCRATCH.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=_INSPECT_SCRATCH) as tmp:
            shutil.copy2(db, Path(tmp) / db.name)
            shutil.copy2(journal, Path(tmp) / journal.name)
            conn = sqlite3.connect(str(Path(tmp) / db.name), timeout=30)
            try:
                yield conn, True
            finally:
                conn.close()
        return
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        yield conn, False
    finally:
        conn.close()


def index_snapshot(repo: Path, with_hashes: bool = False) -> IndexSnapshot:
    """Content points per path; with_hashes also collects payload content_hash
    values (sha256 of each chunk's text, the text the provider embedded),
    point ids and stored hidden_branches."""
    index_dir = repo / ".code-indexer" / "index"
    stores = sorted(index_dir.glob("*/chunks.db"))
    if not stores:
        if any(index_dir.glob("*/**/vector_*.json")):
            raise RuntimeError(
                f"{index_dir} uses the sharded json layout; not supported here"
            )
        return IndexSnapshot("none", {})
    if len(stores) != 1:
        raise RuntimeError(f"expected one collection, found {stores}")
    with _read_only(stores[0]) as (conn, hot):
        rows = conn.execute(
            "SELECT path, COUNT(*) FROM chunks WHERE type = 'content' GROUP BY path"
        ).fetchall()
        snap = IndexSnapshot(
            "chunks_db", {path: count for path, count in rows}, hot_journal=hot
        )
        if with_hashes:
            decompressor = zstandard.ZstdDecompressor()
            for point_id, blob in conn.execute(
                "SELECT point_id, data FROM chunks WHERE type = 'content'"
            ):
                payload = json.loads(decompressor.decompress(blob)).get("payload") or {}
                snap.point_ids.add(point_id)
                if payload.get("content_hash"):
                    snap.content_hashes.add(payload["content_hash"])
                if payload.get("hidden_branches"):
                    snap.hidden[point_id] = tuple(payload["hidden_branches"])
    return snap


def pending_keys(repo: Path) -> Set[str]:
    """Content keys saved in every ``<collection>.pending.db`` (design 4.2).

    Empty when no pending store exists (12.83.0 has none). Read-only.
    """
    keys: Set[str] = set()
    for db in sorted((repo / ".code-indexer" / "index").glob("*.pending.db")):
        with _read_only(db) as (conn, _hot):
            keys.update(
                k for (k,) in conn.execute("SELECT content_key FROM pending_vectors")
            )
    return keys


def metadata_summary(repo: Path) -> Dict[str, Any]:
    path = repo / ".code-indexer" / METADATA_FILE
    if not path.exists():
        return {}
    meta = json.loads(path.read_text())
    return {
        "status": meta.get("status"),
        "total_files_to_index": meta.get("total_files_to_index"),
        "files_processed": meta.get("files_processed"),
        "completed_files": len(meta.get("completed_files") or []),
        "failed_files": meta.get("failed_files"),
        "chunks_indexed": meta.get("chunks_indexed"),
        "git_available": meta.get("git_available"),
        "run_sequence": meta.get("run_sequence"),
        "sealed": bool(meta.get("resume_seal")),
    }


@dataclass
class ProgressSummary:
    file_totals: List[int] = field(default_factory=list)
    last_file_current: Optional[int] = None
    completed: bool = False


def parse_progress_json(text: str) -> ProgressSummary:
    """Summarize ``--progress-json`` lines: distinct file-phase totals in order."""
    summary = ProgressSummary()
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        match = _FILE_PROGRESS.match(str(event.get("info", "")))
        if not match:
            continue
        total = int(match.group(2))
        if not summary.file_totals or summary.file_totals[-1] != total:
            summary.file_totals.append(total)
        summary.last_file_current = int(match.group(1))
        if "Completed" in event["info"]:
            summary.completed = True
    return summary


def inflight_bound(repo: Path) -> int:
    """Chunks an interruption may legitimately lose: parallel requests x batch size."""
    config = json.loads((repo / ".code-indexer" / "config.json").read_text())
    voyage = config.get("voyage_ai", {})
    return int(voyage.get("parallel_requests", 8)) * int(voyage.get("batch_size", 128))
