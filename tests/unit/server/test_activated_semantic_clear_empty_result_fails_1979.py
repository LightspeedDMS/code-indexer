"""Bug #1979 step 4: a clear=true semantic indexing run whose child process
reports success (exit 0) but leaves the index genuinely empty must not be
reported as a successful job -- it must fail loudly.

Every other layer already guards the ordinary ways this could happen (a
literally file-less repo makes SmartIndexer raise ValueError -> non-zero
exit, already handled by the existing `result.returncode != 0` branch; the
step-1/2/3 fixes in this same bug stop the incremental zero-changes path
and the damaged-layout path from producing a silent empty success). The
only way left to exercise the guard this step adds is the boundary
`_execute_semantic_indexing` itself owns: what it does with a subprocess
result. So only that boundary is stubbed here (via the same
`_run_subprocess_with_telemetry` seam the manager already exposes for
telemetry stubbing in the sibling tests) -- the real repo, real
`.code-indexer/index` directory, and the real `FilesystemVectorStore` are
untouched.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from code_indexer.server.services.activated_repo_index_manager import (
    ActivatedRepoIndexManager,
)


def test_clear_reports_failure_when_child_claims_success_but_index_is_empty(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "example-repo"
    repo.mkdir()
    (repo / "app.py").write_text("def greet():\n    return 'hi'\n")

    code_indexer_dir = repo / ".code-indexer"
    code_indexer_dir.mkdir()
    (code_indexer_dir / "config.json").write_text("{}")
    # Genuinely empty index directory: no collections at all, matching what
    # the bug report describes ("leaves an EMPTY semantic index").
    (code_indexer_dir / "index").mkdir()

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
