"""Indexer resume-state trust and containment.

Server-spawned indexing must ignore resume state here too:
``ClaudeCliManager._commit_and_reindex`` spawns `cidx index` to re-index the
cidx-meta directory after committing catch-up description-generation
changes. Fixed via the single shared seam (``append_server_layout_args`` in
``server/utils/index_command_layout.py``), which this call site already
routes through (proven by the pre-existing Story #1488 AST guard). This
test proves the fix reaches this REAL call site end-to-end, mirroring the
mocking pattern of
tests/unit/server/services/test_claude_cli_manager_subprocess_env_sanitization_1325.py.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

from code_indexer.server.services.claude_cli_manager import ClaudeCliManager


def test_commit_and_reindex_command_ignores_resume_state(tmp_path):
    meta_dir = tmp_path / "cidx-meta"
    meta_dir.mkdir()

    manager = ClaudeCliManager(api_key=None, max_workers=0)
    manager.set_meta_dir(meta_dir)

    run_calls: list = []

    def _run(cmd, **kwargs):
        run_calls.append({"cmd": list(cmd), "kwargs": kwargs})
        return Mock(returncode=0, stdout="", stderr="")

    with patch(
        "code_indexer.server.services.claude_cli_manager.subprocess.run",
        side_effect=_run,
    ):
        manager._commit_and_reindex(["some-alias"])

    index_calls = [c for c in run_calls if c["cmd"][:2] == ["cidx", "index"]]
    assert index_calls, f"expected a 'cidx index' call, got: {run_calls}"
    assert "--ignore-resume-state" in index_calls[0]["cmd"], (
        "SECURITY: the cidx-meta catch-up reindex must never trust "
        f"resume state stored inside the meta repo. Got: {index_calls[0]['cmd']}"
    )
