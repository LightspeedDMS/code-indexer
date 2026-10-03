"""Real-fixture builders for Bug #2022 Gap 4 (fatal chunk-store failure
during a golden-repo refresh).

Everything here is REAL: a real ``cidx init`` child, a real CHUNKS_DB
collection written through ``FilesystemVectorStore`` (the production
writer), real on-disk b-tree page corruption that ``PRAGMA quick_check``
reports, and a real server-style ``cidx index`` child process.

No embedding provider is contacted: a corrupt (or unreadable) ``chunks.db``
is detected by the child's Bug #1746 write preflight, which runs before any
file is hashed, chunked or embedded. The dummy API key below only satisfies
the provider's "a key is configured" construction check.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from code_indexer.server.utils.index_command_layout import append_server_layout_args

COLLECTION_NAME = "voyage-code-3"
VECTOR_DIM = 1024
DUMMY_VOYAGE_KEY = "dummy-key-not-used-for-network"
CHILD_TIMEOUT_SECONDS = 180
_METADATA_FILENAME = "metadata-voyage-ai.json"
_SQLITE_FILE_HEADER_BYTES = 100


def cidx_child_command(args: List[str]) -> List[str]:
    """The real CLI, invoked through the current interpreter."""
    return [sys.executable, "-m", "code_indexer.cli", *args]


def child_env() -> Dict[str, str]:
    env = dict(os.environ)
    env["VOYAGE_API_KEY"] = DUMMY_VOYAGE_KEY
    return env


def make_chunks_db_repo(repo_dir: Path, metadata_marker: str) -> Path:
    """Create a git working tree initialised for cidx, holding a real
    CHUNKS_DB collection and a provider metadata file tagged with
    ``metadata_marker``. Returns the collection's ``chunks.db`` path."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    for i in range(1, 4):
        (repo_dir / f"module_{i}.py").write_text(
            f"def function_{i}():\n    return {i}\n"
        )
    git = ["git", "-c", "user.email=test@example.com", "-c", "user.name=test"]
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run([*git, "commit", "-qm", "init"], cwd=repo_dir, check=True)
    subprocess.run(
        cidx_child_command(["init"]),
        cwd=repo_dir,
        env=child_env(),
        check=True,
        capture_output=True,
        timeout=CHILD_TIMEOUT_SECONDS,
    )

    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    index_dir = repo_dir / ".code-indexer" / "index"
    store = FilesystemVectorStore(
        base_path=index_dir,
        project_root=repo_dir,
        use_chunks_db_for_new_collections=True,
    )
    assert store.create_collection(COLLECTION_NAME, VECTOR_DIM)
    store.begin_indexing(COLLECTION_NAME)
    store.upsert_points(
        COLLECTION_NAME,
        [
            {
                "id": f"point-{i}",
                "vector": [0.01 * (i + 1)] * VECTOR_DIM,
                "payload": {"path": f"module_{i % 3 + 1}.py", "content": "x" * 3000},
            }
            for i in range(40)
        ],
    )
    store.end_indexing(COLLECTION_NAME)
    write_metadata_marker(repo_dir, metadata_marker)
    chunks_db = index_dir / COLLECTION_NAME / "chunks.db"
    assert quick_check_ok(chunks_db)
    return chunks_db


def write_metadata_marker(repo_dir: Path, marker: str) -> None:
    """Write a realistic completed provider metadata file. An empty-looking
    one ("0 files indexed") makes the child wipe and recreate the whole
    collection instead of resuming, which is not the production path."""
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo_dir / ".code-indexer" / _METADATA_FILENAME).write_text(
        json.dumps(
            {
                "status": "completed",
                "last_index_timestamp": time.time(),
                "git_available": True,
                "project_id": repo_dir.name,
                "current_branch": "master",
                "current_commit": head,
                "embedding_provider": "voyage-ai",
                "embedding_model": COLLECTION_NAME,
                "files_processed": 3,
                "chunks_indexed": 3,
                "failed_files": 0,
                "branch_commit_watermarks": {"master": head},
                "fixture_marker": marker,
            }
        )
    )


def read_metadata_marker(repo_dir: Path) -> Optional[str]:
    path = repo_dir / ".code-indexer" / _METADATA_FILENAME
    if not path.exists():
        return None
    value = json.loads(path.read_text()).get("fixture_marker")
    return str(value) if value is not None else None


def quick_check_ok(chunks_db: Path) -> bool:
    """True only when a fresh read-only connection's PRAGMA quick_check is ok."""
    try:
        conn = sqlite3.connect(f"file:{chunks_db}?mode=ro", uri=True)
        try:
            rows = conn.execute("PRAGMA quick_check").fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False
    return rows == [("ok",)]


def corrupt_btree_pages(chunks_db: Path) -> None:
    """Garble the b-tree page header of every page -- including the
    sqlite_master b-tree on page 1 (after the 100-byte file header), so the
    child's write preflight trips over it at open, before any embedding --
    then prove quick_check now fails."""
    conn = sqlite3.connect(str(chunks_db))
    try:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    finally:
        conn.close()
    assert page_count > 2, "fixture too small to corrupt real pages"
    with open(chunks_db, "r+b") as handle:
        handle.seek(_SQLITE_FILE_HEADER_BYTES)
        handle.write(bytes([0xFF, 0xFF, 0xFF]))
        for page in range(2, page_count + 1):
            handle.seek((page - 1) * page_size)
            header = handle.read(12)
            handle.seek((page - 1) * page_size)
            handle.write(bytes([0x0D, 0xFF, 0xFF]) + header[3:])
    assert not quick_check_ok(chunks_db), "corruption did not take"


def run_server_index_child(repo_dir: Path) -> subprocess.CompletedProcess:
    """Run the exact server-context ``cidx index`` command line
    (``refresh_scheduler._index_source`` builds the same arguments)."""
    args = append_server_layout_args(["cidx", "index", "--fts", "--progress-json"])
    return subprocess.run(
        cidx_child_command(args[1:]),
        cwd=repo_dir,
        env=child_env(),
        capture_output=True,
        text=True,
        timeout=CHILD_TIMEOUT_SECONDS,
    )


def install_cidx_shim(bin_dir: Path) -> Path:
    """Put a ``cidx`` executable on a private bin dir that runs THIS
    interpreter's real CLI, so code that spawns bare ``cidx`` (the refresh
    scheduler) runs the code under test."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "cidx"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m code_indexer.cli "$@"\n')
    shim.chmod(0o755)
    return bin_dir
