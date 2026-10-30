"""#2093: after a watch cycle (and after a watch stop) the changed files are
searchable with their new content only.

Real pieces: the `cidx` CLI (index, query) and the watch handler the server's
auto-watch builds (`DaemonWatchManager._create_watch_handler`), run in a
child with the test network guard; the embedding endpoint is the local fake
(tests/utils/fake_voyage_provider.py), never a vendor.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterator, List

import pytest

from tests.utils.cidx_fake_provider_repo import FakeProviderRepo
from tests.utils.fake_voyage_provider import running_fake_voyage_server

pytestmark = [pytest.mark.slow, pytest.mark.timeout(600)]

KEY = "sentinel-test-key-2093"
OLD = "def alpha_original_marker_qzx():\n    return 'alpha OLDCONTENTTOKEN version'\n"
NEW = "def alpha_rewritten_marker_wvy():\n    return 'alpha NEWCONTENTTOKEN version'\n"
GAMMA = "def gamma_created_marker_pkt():\n    return 'gamma CREATEDTOKEN file'\n"
BETA = "def beta_deleted_marker_jhn():\n    return 'beta DELETEDTOKEN file'\n"
KEEP = "def keep_marker():\n    return 'KEEPTOKEN'\n"

#: One watch step in a guarded child: rewrite alpha, create gamma, delete
#: beta, queue the three events exactly as the observer does, then run one
#: processing cycle ("cycle") or stop the watch ("stop").
WATCH_STEP = r"""
import json, sys
from pathlib import Path
from code_indexer.daemon.watch_manager import DaemonWatchManager

repo, mode, server_watch = Path(sys.argv[1]), sys.argv[2], sys.argv[3] == "1"
handler = DaemonWatchManager()._create_watch_handler(
    str(repo), None, server_watch=server_watch
)
(repo / "src/alpha.py").write_text(sys.argv[4])
(repo / "src/gamma.py").write_text(sys.argv[5])
(repo / "src/beta.py").unlink()
handler._add_pending_change(repo / "src/alpha.py", "modified")
handler._add_pending_change(repo / "src/gamma.py", "created")
handler._add_pending_change(repo / "src/beta.py", "deleted")
if mode == "cycle":
    handler._process_pending_changes()
else:
    handler.stop_watching()
owed = {}
for indexer in getattr(handler, "indexers", [handler.smart_indexer]):
    store = indexer.vector_store_client
    name = store.resolve_collection_name(indexer.config, indexer.embedding_provider)
    owed[name] = store.hnsw_publish_pending(name)
print("RESULT " + json.dumps({"owed": owed, "pending": len(handler.pending_changes)}))
"""


def _git(repo: FakeProviderRepo, *args: str) -> None:
    proc = subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=repo.repo,
        env=repo.env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.fixture
def indexed_repo(tmp_path: Path) -> Iterator[FakeProviderRepo]:
    with running_fake_voyage_server(KEY) as server:
        repo = FakeProviderRepo(tmp_path, server, KEY)
        (repo.repo / "src/alpha.py").write_text(OLD)
        (repo.repo / "src/beta.py").write_text(BETA)
        (repo.repo / "src/keep.py").write_text(KEEP)
        (repo.repo / ".gitignore").write_text(".code-indexer/\n")
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "init")
        proc = repo.cidx(*repo.index_args(), "--fts")
        assert proc.returncode == 0, proc.stderr[-3000:]
        yield repo


def run_watch_step(
    repo: FakeProviderRepo, mode: str, server_watch: bool
) -> Dict[str, object]:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            WATCH_STEP,
            str(repo.repo),
            mode,
            "1" if server_watch else "0",
            NEW,
            GAMMA,
        ],
        cwd=repo.repo,
        env=repo.env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    blocked = (
        repo.blocked_network_log.read_text()
        if repo.blocked_network_log.exists()
        else ""
    )
    assert blocked == "", f"watch step attempted network access: {blocked}"
    assert proc.returncode == 0, proc.stderr[-4000:]
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
    assert lines, proc.stdout[-2000:] + proc.stderr[-2000:]
    result: Dict[str, object] = json.loads(lines[-1][len("RESULT ") :])
    return result


def search(repo: FakeProviderRepo, *args: str) -> str:
    proc = repo.query(*args, "--limit", "50")
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc.stdout + proc.stderr


def assert_semantic_serves_new_content_only(repo: FakeProviderRepo) -> None:
    out = search(repo, "alpha NEWCONTENTTOKEN version")
    assert "NEWCONTENTTOKEN" in out
    assert "CREATEDTOKEN" in out
    assert "KEEPTOKEN" in out
    assert "OLDCONTENTTOKEN" not in out
    assert "DELETEDTOKEN" not in out
    assert "stale" not in out.lower()


@pytest.mark.parametrize("mode", ["cycle", "stop"])
def test_watch_leaves_semantic_search_on_new_content_only(
    indexed_repo: FakeProviderRepo, mode: str
) -> None:
    indexed_repo.server.ledger.begin_run("watch")

    result = run_watch_step(indexed_repo, mode, server_watch=False)
    watch_inputs = indexed_repo.server.ledger.run_stats("watch")["inputs"]

    assert result["pending"] == 0
    owed = result["owed"]
    assert isinstance(owed, dict) and owed and not any(owed.values()), owed
    assert_semantic_serves_new_content_only(indexed_repo)
    # Only the two changed one-chunk files were embedded; the publish embeds nothing.
    assert watch_inputs == 2


def fts_hits(repo: FakeProviderRepo, token: str) -> List[str]:
    out = search(repo, token, "--fts")
    return [name for name in ("alpha.py", "beta.py", "gamma.py") if name in out]


@pytest.mark.parametrize("mode", ["cycle", "stop"])
def test_server_watch_cycle_updates_fts_including_deletions(
    indexed_repo: FakeProviderRepo, mode: str
) -> None:
    assert fts_hits(indexed_repo, "OLDCONTENTTOKEN") == ["alpha.py"]
    assert fts_hits(indexed_repo, "DELETEDTOKEN") == ["beta.py"]

    run_watch_step(indexed_repo, mode, server_watch=True)

    assert fts_hits(indexed_repo, "NEWCONTENTTOKEN") == ["alpha.py"]
    assert fts_hits(indexed_repo, "CREATEDTOKEN") == ["gamma.py"]
    assert fts_hits(indexed_repo, "OLDCONTENTTOKEN") == []
    assert fts_hits(indexed_repo, "DELETEDTOKEN") == []
    assert_semantic_serves_new_content_only(indexed_repo)
