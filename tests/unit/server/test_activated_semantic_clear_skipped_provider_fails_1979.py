"""Bug #1979 (codex review findings, turns 13 and 15): a clear=true job
whose child skips a configured provider -- for ANY reason (missing API
key, a failed health check, or anything else) -- must not be reported as
success.

Earlier iterations of this guard matched a SPECIFIC stdout string emitted
by one specific skip reason ("no API key found"). Codex's turn-15 finding
proved that approach is fundamentally fragile: a DIFFERENT skip reason
("<provider> health check failed ... skipping", cli.py ~4648) uses
different wording and slipped straight past the regex. Chasing individual
message strings is whack-a-mole.

The robust fix reads the repo's real config.json via the real
`Config.get_embedding_providers()` (zero duplication of the CLI's
credential-resolution logic) and checks, for EVERY configured provider,
that its own `metadata-<provider>.json` (the CLI's own per-provider
progress file, `cli.py`'s `_get_provider_metadata_path`) was genuinely
updated AFTER this clear operation started. This is agnostic to *why* a
provider didn't run -- missing key, bad key, health-check failure, a
future reason not yet invented -- because all of them leave that
provider's own metadata file untouched or stale, which is the one signal
that's actually authoritative regardless of stdout wording.

This file covers the "provider's metadata file is missing entirely"
sub-case (never even attempted). The "metadata exists but predates this
run, while its collection still shows old points" sub-case (a supposedly
full rebuild silently leaving stale data behind) is covered separately in
test_activated_semantic_clear_stale_provider_fails_1979.py.
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


def test_clear_reports_failure_when_a_configured_provider_is_skipped(
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

    # voyage-ai indexed fine and is genuinely populated; cohere was
    # configured but never even attempted -- no collection directory AND
    # no metadata-cohere.json at all, regardless of which reason caused
    # the skip.
    store = FilesystemVectorStore(index_dir, project_root=repo)
    store.create_collection("voyage-code-3", vector_size=1024)
    point = store.create_point(
        vector=[1.0] + [0.0] * 1023,
        payload={"path": "app.py", "content": "def greet(): return 'hi'"},
        point_id="app-py-chunk-0",
    )
    store.upsert_points("voyage-code-3", [point])
    assert store.count_points("voyage-code-3") > 0

    manager = ActivatedRepoIndexManager.__new__(ActivatedRepoIndexManager)
    manager.logger = logging.getLogger(__name__)

    def fake_subprocess_touches_only_voyage(*args, **kwargs):
        # Simulate the real cidx child genuinely completing voyage-ai's
        # pass -- its own metadata file gets a fresh, post-call timestamp,
        # exactly like the real subprocess would produce.
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
