"""Bug #1979 (codex review finding, turn 11): a clear=true job whose child
exits 0 but leaves ONE configured collection populated while ANOTHER
configured collection is genuinely empty must not be reported as success.

The guard added in test_activated_semantic_clear_empty_result_fails_1979.py
only proves the ALL-empty case is caught. This proves the PARTIAL case: one
real, populated collection (voyage-code-3-style) sitting next to a second,
genuinely empty collection (a second provider, e.g. cohere, that silently
failed) must also fail the job -- matching the acceptance criterion's
"every configured collection ... holds points", not just "at least one".
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from code_indexer.server.services.activated_repo_index_manager import (
    ActivatedRepoIndexManager,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore


def test_clear_reports_failure_when_one_of_two_collections_is_empty(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "example-repo"
    repo.mkdir()
    (repo / "app.py").write_text("def greet():\n    return 'hi'\n")

    code_indexer_dir = repo / ".code-indexer"
    code_indexer_dir.mkdir()
    (code_indexer_dir / "config.json").write_text("{}")
    index_dir = code_indexer_dir / "index"
    index_dir.mkdir()

    # Real store, real collections -- no mocking of the indexer/store.
    store = FilesystemVectorStore(index_dir, project_root=repo)
    store.create_collection("voyage-code-3", vector_size=1024)
    store.create_collection("embed-v4.0", vector_size=1536)

    # voyage-code-3 genuinely has a point; embed-v4.0 (standing in for a
    # second provider that silently failed) stays empty.
    point = store.create_point(
        vector=[1.0] + [0.0] * 1023,
        payload={"path": "app.py", "content": "def greet(): return 'hi'"},
        point_id="app-py-chunk-0",
    )
    store.upsert_points("voyage-code-3", [point])
    assert store.count_points("voyage-code-3") > 0
    assert store.count_points("embed-v4.0") == 0

    manager = ActivatedRepoIndexManager.__new__(ActivatedRepoIndexManager)
    manager.logger = logging.getLogger(__name__)

    def fake_subprocess_success(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=["cidx", "index", "--clear"],
            returncode=0,
            stdout="Files processed: 1\n",
            stderr="",
        )

    monkeypatch.setattr(
        manager, "_run_subprocess_with_telemetry", fake_subprocess_success
    )

    result = manager._execute_semantic_indexing(str(repo), clear=True)

    assert result["success"] is False, result
