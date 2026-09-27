"""Bug #1979 (codex review finding, turn 15): a configured provider whose
collection is genuinely populated -- but with data from a PREVIOUS run,
never touched during THIS clear=true operation -- must not let the job
report success. A positive point count alone is not proof a collection
was rebuilt; only its own metadata's timestamp says whether this run
actually reindexed it.

Scenario: voyage-ai gets a real, fresh rebuild this run. cohere was
configured but its health check failed this run (or any other reason it
silently didn't run) -- cohere's collection still holds old points from a
prior successful index, and its metadata-cohere.json still carries that
OLD run's timestamp, untouched by this one. clear=true promised a
from-scratch rebuild; cohere silently kept stale data instead.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

from code_indexer.server.services.activated_repo_index_manager import (
    ActivatedRepoIndexManager,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore


def test_clear_reports_failure_when_a_provider_collection_is_stale(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "example-repo"
    repo.mkdir()
    (repo / "app.py").write_text("def greet():\n    return 'hi'\n")

    code_indexer_dir = repo / ".code-indexer"
    code_indexer_dir.mkdir()
    code_indexer_dir.joinpath("config.json").write_text(
        json.dumps({"embedding_providers": ["voyage-ai", "cohere"]})
    )
    index_dir = code_indexer_dir / "index"
    index_dir.mkdir()

    store = FilesystemVectorStore(index_dir, project_root=repo)
    store.create_collection("voyage-code-3", vector_size=1024)
    store.create_collection("embed-v4.0", vector_size=1536)

    old_point = store.create_point(
        vector=[1.0] + [0.0] * 1535,
        payload={"path": "app.py", "content": "stale content from a prior run"},
        point_id="stale-chunk-0",
    )
    store.upsert_points("embed-v4.0", [old_point])
    assert store.count_points("embed-v4.0") > 0

    # cohere's metadata reflects a run that finished well BEFORE this
    # clear=true operation -- it must be treated as "not rebuilt this run"
    # even though its collection is genuinely non-empty.
    stale_timestamp = time.time() - 3600
    code_indexer_dir.joinpath("metadata-cohere.json").write_text(
        json.dumps({"status": "completed", "last_index_timestamp": stale_timestamp})
    )

    manager = ActivatedRepoIndexManager.__new__(ActivatedRepoIndexManager)
    manager.logger = logging.getLogger(__name__)

    def fake_subprocess_touches_only_voyage(*args, **kwargs):
        # voyage-ai genuinely gets indexed this run: fresh points AND a
        # fresh metadata timestamp. cohere's collection/metadata are left
        # completely untouched by this fake, exactly like the real skip.
        point = store.create_point(
            vector=[1.0] + [0.0] * 1023,
            payload={"path": "app.py", "content": "def greet(): return 'hi'"},
            point_id="fresh-chunk-0",
        )
        store.upsert_points("voyage-code-3", [point])
        code_indexer_dir.joinpath("metadata-voyage-ai.json").write_text(
            json.dumps({"status": "completed", "last_index_timestamp": time.time()})
        )
        return subprocess.CompletedProcess(
            args=["cidx", "index", "--clear"],
            returncode=0,
            stdout="Files processed: 1\n",
            stderr="",
        )

    monkeypatch.setattr(
        manager, "_run_subprocess_with_telemetry", fake_subprocess_touches_only_voyage
    )

    result = manager._execute_semantic_indexing(str(repo), clear=True)

    assert result["success"] is False, result
