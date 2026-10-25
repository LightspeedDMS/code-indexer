"""A non-git (``local://``-shaped) repository driven through the REAL ``cidx``
CLI (subprocess, the tree under test) against the local fake VoyageAI server
(tests/utils/fake_voyage_provider.py: plain HTTP on 127.0.0.1, sentinel key).
No real provider call is possible through it.

Shared by the Story #2119 and #2120 tests.
"""

from __future__ import annotations

import json
import os
import signal
import site
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Set, Tuple

import code_indexer

SRC_DIR = Path(code_indexer.__file__).resolve().parents[1]
CLI_TIMEOUT_SECONDS = 120
COLLECTION = "voyage-code-3"


def unique_python_text(name: str, lines: int) -> str:
    """Unique content per file and per line, so every chunk is distinct."""
    return "".join(
        f"def {name}_function_{i}(value):\n"
        f"    return value * {i} + len('{name}-{i}-unique-marker-text')\n"
        for i in range(lines)
    )


#: Read-only PEP 578 audit hook installed in every cidx child: any
#: non-loopback connect or DNS lookup is refused and logged, so no test can
#: reach a real provider endpoint (e.g. a reranker not routed to the fake).
#: Audit argument layouts: socket.connect -> (socket, address);
#: socket.getaddrinfo -> (host, port, family, type, proto).
_NETWORK_GUARD = """\
import sys

_LOOPBACK = (None, "127.0.0.1", "::1", "localhost", b"localhost")


def _cidx_test_network_guard(event, args):
    target = None
    if event == "socket.connect":
        address = args[1]
        if isinstance(address, tuple) and address and address[0] not in _LOOPBACK:
            target = address
    elif event == "socket.getaddrinfo" and args[0] not in _LOOPBACK:
        target = args[0]
    if target is not None:
        with open(_BLOCKED_LOG, "a") as log:
            log.write(f"{event} {target!r}\\n")
        raise PermissionError(f"test network guard refused {event} {target!r}")


sys.addaudithook(_cidx_test_network_guard)
"""


def install_network_guard(
    root: Path, extra_sitecustomize: str = ""
) -> Tuple[Path, Path]:
    """Write the test ``sitecustomize`` (any non-loopback connect or DNS
    lookup is refused and logged) under ``root``. Returns the site dir to
    put first on a child's PYTHONPATH and the log of refused attempts."""
    blocked_log = root / "blocked-network.log"
    site_dir = root / "test-site"
    site_dir.mkdir()
    (site_dir / "sitecustomize.py").write_text(
        f"_BLOCKED_LOG = {str(blocked_log)!r}\n" + _NETWORK_GUARD + extra_sitecustomize
    )
    return site_dir, blocked_log


class FakeProviderRepo:
    """A non-git repository initialised for cidx, pointed at the fake.

    Every child runs with a test ``sitecustomize`` that refuses (and logs)
    any non-loopback network access; ``extra_sitecustomize`` is appended to
    it (one ``sitecustomize`` per interpreter)."""

    def __init__(
        self,
        root: Path,
        server,
        api_key: str,
        extra_sitecustomize: str = "",
    ) -> None:
        self.server = server
        self.repo = root / "example-repo"
        (self.repo / "src").mkdir(parents=True)
        home = root / "home"
        home.mkdir()
        site_dir, self.blocked_network_log = install_network_guard(
            root, extra_sitecustomize
        )
        self.env: Dict[str, str] = {
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "PYTHONPATH": f"{site_dir}{os.pathsep}{SRC_DIR}",
            "PYTHONUSERBASE": site.getuserbase(),
            "VOYAGE_API_KEY": api_key,
            # The tokenizer comes from the existing local Hugging Face cache
            # (the temp HOME has none), never from the network.
            "HF_HOME": os.environ.get(
                "HF_HOME", str(Path.home() / ".cache" / "huggingface")
            ),
            "HF_HUB_OFFLINE": "1",
        }
        proc = self.cidx("init")
        assert proc.returncode == 0, proc.stderr[-3000:]
        config_path = self.repo / ".code-indexer" / "config.json"
        config = json.loads(config_path.read_text())
        config.setdefault("voyage_ai", {})["api_endpoint"] = (
            f"{server.base_url}/v1/embeddings"
        )
        config_path.write_text(json.dumps(config))

    @property
    def collection_dir(self) -> Path:
        return self.repo / ".code-indexer" / "index" / COLLECTION

    @property
    def chunks_db(self) -> Path:
        return self.collection_dir / "chunks.db"

    @property
    def metadata_path(self) -> Path:
        return self.repo / ".code-indexer" / "metadata-voyage-ai.json"

    def metadata(self) -> Dict[str, object]:
        loaded: Dict[str, object] = json.loads(self.metadata_path.read_text())
        return loaded

    def write_files(self, prefix: str, count: int, lines: int = 40) -> None:
        for i in range(count):
            name = f"{prefix}_{i:03d}"
            (self.repo / "src" / f"{name}.py").write_text(
                unique_python_text(name, lines)
            )

    def index_args(self) -> List[str]:
        # The server's new-collection layout, so the collection is CHUNKS_DB.
        return ["index", "--new-collection-layout=chunks_db"]

    def cidx(
        self, *args: str, timeout: float = CLI_TIMEOUT_SECONDS
    ) -> "subprocess.CompletedProcess[str]":
        proc = subprocess.run(
            [sys.executable, "-m", "code_indexer.cli", *args],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            # ^ why surrogateescape, not strict; a raw non-UTF-8 byte stays distinct from an escaped one; #2196
            errors="surrogateescape",
            timeout=timeout,
        )
        blocked = (
            self.blocked_network_log.read_text()
            if self.blocked_network_log.exists()
            else ""
        )
        assert blocked == "", f"cidx {args[:1]} attempted network access: {blocked}"
        return proc

    def query(self, *args: str) -> "subprocess.CompletedProcess[str]":
        """``cidx query`` with the reranker explicitly disabled
        (``--rerank-query ''``): its endpoint is not routed to the fake, so
        an enabled reranker would reach the real vendor endpoint. Every
        query a test makes must go through here."""
        return self.cidx("query", *args, "--rerank-query", "", "--quiet")

    def spawn_index(self, *extra: str) -> "subprocess.Popen[bytes]":
        return subprocess.Popen(
            [sys.executable, "-m", "code_indexer.cli", *self.index_args(), *extra],
            cwd=self.repo,
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def stored_content_hashes(self) -> Set[str]:
        """content_hash of every committed row. A plain connection: after a
        SIGKILL it rolls back a hot journal exactly as the next writer would,
        leaving only committed rows."""
        from code_indexer.storage.sqlite_chunk_store import ChunkStore

        with ChunkStore(self.chunks_db) as store:
            return {
                record["payload"]["content_hash"]
                for record in store.stream_all()
                if record.get("payload", {}).get("content_hash")
            }

    def stored_rows(self) -> Dict[str, str]:
        """point_id -> stored path of every committed row (plain read)."""
        conn = sqlite3.connect(str(self.chunks_db))
        try:
            return {
                str(point_id): str(path)
                for point_id, path in conn.execute("SELECT point_id, path FROM chunks")
            }
        finally:
            conn.close()

    def committed_row_count(self) -> int:
        """Row count through a read-only connection; 0 while the writer holds
        the lock or before the store exists."""
        if not self.chunks_db.exists():
            return 0
        uri = f"{self.chunks_db.resolve().as_uri()}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=0.05)
            try:
                return int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
            finally:
                conn.close()
        except sqlite3.Error:
            return 0


def kill_after_rows(
    repo: FakeProviderRepo, minimum_rows: int, deadline_s: float
) -> None:
    """Spawn an index run and SIGKILL its process group once at least
    ``minimum_rows`` rows are committed."""
    proc = repo.spawn_index()
    try:
        deadline = time.monotonic() + deadline_s
        while repo.committed_row_count() < minimum_rows:
            assert proc.poll() is None, "the child finished before it could be killed"
            assert time.monotonic() < deadline, "the child committed too few rows"
            time.sleep(0.05)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=30)


def kill_at_first_held_request(
    repo: FakeProviderRepo, *extra_args: str, deadline_s: float = 90
) -> None:
    """Spawn an index run whose ledger run holds responses (the caller began
    it with ``hold_seconds``) and SIGKILL its process group as soon as the
    first embedding request is held: after the run planned, before any
    embedding of this run could be committed."""
    proc = repo.spawn_index(*extra_args)
    try:
        held = repo.server.ledger.wait_for_held(1, timeout=deadline_s)
        assert held, "the child never sent an embedding request"
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=30)
