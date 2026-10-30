"""#2093 (review P1-2): a branch switch seen by the watch goes through the
same rules as a write cycle: every provider's collection, FTS, a publish,
and the repository indexing lock (deferred while another run holds it).
Queued changes the branch change does not cover are kept.

Two providers: no Cohere fake exists, so the second "provider" is a second
collection of the same fake Voyage endpoint (model voyage-large-2, its own
SmartIndexer, store and metadata file) -- the same shape the server watch
builds per configured provider.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any, Dict

import pytest

from tests.utils.cidx_fake_provider_repo import FakeProviderRepo
from tests.unit.services.test_watch_cycle_searchable_2093 import (  # noqa: F401
    GAMMA,
    NEW,
    indexed_repo,
)

pytestmark = [pytest.mark.slow, pytest.mark.timeout(600)]

STEP = r"""
import json, subprocess, sys, time
from pathlib import Path

repo, NEW, GAMMA = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
from code_indexer.backends.backend_factory import BackendFactory
from code_indexer.config import ConfigManager
from code_indexer.services.embedding_factory import EmbeddingProviderFactory
from code_indexer.services.git_aware_watch_handler import GitAwareWatchHandler
from code_indexer.services.git_topology_service import GitTopologyService
from code_indexer.services.provider_metadata_path import provider_metadata_path
from code_indexer.services.smart_indexer import SmartIndexer
from code_indexer.services.tantivy_index_manager import TantivyIndexManager
from code_indexer.services.watch_metadata import WatchMetadata


def git(*args):
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def make_indexer(cfg, metadata):
    store = BackendFactory.create(cfg, repo).get_vector_store_client()
    return SmartIndexer(cfg, EmbeddingProviderFactory.create(config=cfg), store, metadata)


cidx = repo / ".code-indexer"
config = ConfigManager.load_verified_config(repo)
primary = make_indexer(config, provider_metadata_path(cidx, "voyage-ai"))
second_config = config.model_copy(deep=True)
second_config.voyage_ai.model = "voyage-large-2"
second = make_indexer(second_config, cidx / "metadata-second-collection.json")
second.smart_index(force_full=True, quiet=True)

main_head = git("rev-parse", "HEAD")
git("checkout", "-q", "-b", "feature")
(repo / "src/alpha.py").write_text(NEW)
(repo / "src/gamma.py").write_text(GAMMA)
(repo / "src/beta.py").unlink()
git("add", "-A")
git("commit", "-qm", "feature")
feature_head = git("rev-parse", "HEAD")
keep = repo / "src/keep.py"
keep.write_text("def keep_marker():\n    return 'KEEPEDITEDTOKEN'\n")

handler = GitAwareWatchHandler(
    config=config,
    smart_indexer=primary,
    git_topology_service=GitTopologyService(repo, config),
    watch_metadata=WatchMetadata(),
    debounce_seconds=0.1,
    extra_indexers=[second],
    maintain_fts=True,
    hold_indexing_lock=True,
)
handler.queue_changes([keep])
event = {"old_branch": "main", "new_branch": "feature",
         "old_commit": main_head, "new_commit": feature_head}


def fts(token):
    index = TantivyIndexManager(cidx / "tantivy_index")
    index.open_for_search()
    return sorted(r["path"] for r in index.search(query_text=token, limit=50))


def snapshot():
    collections = {}
    for indexer in handler.indexers:
        store = indexer.vector_store_client
        name = store.resolve_collection_name(indexer.config, indexer.embedding_provider)
        hits = store.search("alpha NEWCONTENTTOKEN version", indexer.embedding_provider,
                            collection_name=name, limit=50)
        collections[name] = {
            "paths": sorted(h["payload"]["path"] for h in hits),
            "text": json.dumps(hits, default=str),
            "owed": store.hnsw_publish_pending(name),
        }
    return {"collections": collections,
            "fts": {t: fts(t) for t in ("NEWCONTENTTOKEN", "OLDCONTENTTOKEN",
                                        "CREATEDTOKEN", "DELETEDTOKEN", "KEEPEDITEDTOKEN")}}


sleeper = subprocess.Popen(["sleep", "120"])
heartbeat = cidx / "indexing_heartbeat.json"
now = time.time()
heartbeat.write_text(json.dumps({"pid": sleeper.pid, "project_path": str(repo),
                                 "started_at": now, "last_heartbeat": now}))
try:
    handler._handle_branch_change(event)
    while_locked = snapshot()
    while_locked["keep_queued"] = keep in handler.pending_changes
finally:
    sleeper.kill()
    sleeper.wait(timeout=10)
heartbeat.unlink()
handler._process_pending_changes()
print("RESULT " + json.dumps({"while_locked": while_locked, "after": snapshot()}))
"""


def _run(repo: FakeProviderRepo) -> Dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-c", STEP, str(repo.repo), NEW, GAMMA],
        cwd=repo.repo,
        env=repo.env,
        capture_output=True,
        text=True,
        timeout=400,
    )
    blocked = (
        repo.blocked_network_log.read_text()
        if repo.blocked_network_log.exists()
        else ""
    )
    assert blocked == "", f"branch step attempted network access: {blocked}"
    assert proc.returncode == 0, proc.stderr[-4000:]
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
    assert lines, proc.stdout[-3000:] + proc.stderr[-3000:]
    result: Dict[str, Any] = json.loads(lines[-1][len("RESULT ") :])
    return result


def test_branch_switch_updates_every_collection_and_fts_under_the_lock(
    indexed_repo: FakeProviderRepo,  # noqa: F811
) -> None:
    result = _run(indexed_repo)
    locked, after = result["while_locked"], result["after"]

    # Deferred while another run holds the repository lock: nothing written.
    assert locked["fts"]["OLDCONTENTTOKEN"] == ["src/alpha.py"]
    assert locked["fts"]["CREATEDTOKEN"] == []
    assert locked["keep_queued"] is True
    for state in locked["collections"].values():
        assert "src/beta.py" in state["paths"]

    assert set(after["collections"]) == {"voyage-code-3", "voyage-large-2"}
    for name, state in after["collections"].items():
        assert state["owed"] is False, name
        assert "src/gamma.py" in state["paths"], name
        assert "src/beta.py" not in state["paths"], name
        assert state["paths"].count("src/alpha.py") == 1, name
        assert "OLDCONTENTTOKEN" not in state["text"], name
    assert after["fts"]["NEWCONTENTTOKEN"] == ["src/alpha.py"]
    assert after["fts"]["CREATEDTOKEN"] == ["src/gamma.py"]
    assert after["fts"]["OLDCONTENTTOKEN"] == []
    assert after["fts"]["DELETEDTOKEN"] == []
    assert after["fts"]["KEEPEDITEDTOKEN"] == ["src/keep.py"]
