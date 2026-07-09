"""Bug #1325 (code-review follow-up): bootstrap_cidx_meta() spawns `cidx init`
with cwd=<cidx_meta_path> but never passed an env= kwarg -- the child
unconditionally inherited a RELATIVE PYTHONPATH from the server process
unchanged. Because PYTHONPATH resolution is relative to the CURRENT process's
cwd, and the child runs with cwd=cidx_meta_path, the relative entry re-anchors
into cidx_meta_path -- if that directory ever contains a src/-layout package
colliding with a real cidx dependency (e.g. click), the local package shadows
the installed dependency and `cidx init` fails.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

_RELATIVE_PYTHONPATH = "./src"


def _write_code_indexer_config(cidx_meta_path: Path) -> None:
    code_indexer_dir = cidx_meta_path / ".code-indexer"
    code_indexer_dir.mkdir(parents=True, exist_ok=True)
    (code_indexer_dir / "config.json").write_text(
        '{"codebase_dir": "' + str(cidx_meta_path) + '", "file_extensions": []}'
    )


def _make_manager() -> MagicMock:
    mgr = MagicMock()
    mgr.golden_repo_exists.return_value = False
    mgr.register_local_repo.return_value = True
    return mgr


class TestBootstrapCidxMetaSanitizesPythonPath:
    def test_cidx_init_receives_absolutized_pythonpath(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PYTHONPATH", _RELATIVE_PYTHONPATH)
        expected_abs = os.path.abspath(_RELATIVE_PYTHONPATH)

        from code_indexer.server.startup.bootstrap import bootstrap_cidx_meta

        cidx_meta_path = tmp_path / "cidx-meta"
        run_calls: list = []

        def _fake_run(cmd, **kwargs):
            run_calls.append({"cmd": list(cmd), "kwargs": kwargs})
            _write_code_indexer_config(cidx_meta_path)
            result = MagicMock()
            result.returncode = 0
            return result

        with patch("subprocess.run", side_effect=_fake_run):
            bootstrap_cidx_meta(_make_manager(), str(tmp_path))

        init_calls = [c for c in run_calls if c["cmd"] == ["cidx", "init"]]
        assert init_calls, f"expected a 'cidx init' call, got: {run_calls}"
        init_env = init_calls[0]["kwargs"].get("env")
        assert init_env is not None, "cidx init must receive a sanitized env"
        assert init_env["PYTHONPATH"] == expected_abs
