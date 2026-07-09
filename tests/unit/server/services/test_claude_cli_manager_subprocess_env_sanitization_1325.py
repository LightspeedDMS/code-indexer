"""Bug #1325 (code-review follow-up): ClaudeCliManager._commit_and_reindex
spawns `cidx index` with cwd=<meta_dir> but never passed an env= kwarg -- the
child unconditionally inherited a RELATIVE PYTHONPATH from the server process
unchanged. Because PYTHONPATH resolution is relative to the CURRENT process's
cwd, and the child runs with cwd=meta_dir, the relative entry re-anchors into
meta_dir -- if that directory ever contains a src/-layout package colliding
with a real cidx dependency (e.g. click), the local package shadows the
installed dependency and `cidx index` fails.
"""

from __future__ import annotations

import os
from unittest.mock import Mock, patch

from code_indexer.server.services.claude_cli_manager import ClaudeCliManager

_RELATIVE_PYTHONPATH = "./src"


class TestCommitAndReindexSanitizesPythonPath:
    def test_cidx_index_receives_absolutized_pythonpath(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PYTHONPATH", _RELATIVE_PYTHONPATH)
        expected_abs = os.path.abspath(_RELATIVE_PYTHONPATH)

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

        index_calls = [c for c in run_calls if c["cmd"] == ["cidx", "index"]]
        assert index_calls, f"expected a 'cidx index' call, got: {run_calls}"
        index_env = index_calls[0]["kwargs"].get("env")
        assert index_env is not None, "cidx index must receive a sanitized env"
        assert index_env["PYTHONPATH"] == expected_abs
