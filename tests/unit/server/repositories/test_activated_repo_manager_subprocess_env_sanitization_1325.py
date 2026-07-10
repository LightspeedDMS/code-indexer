"""Bug #1325 (code-review follow-up): ActivatedRepoManager._stop_composite_services
spawns `cidx stop` with cwd=<repo_path> but (unlike the other cidx subprocess
call sites already fixed in golden_repo_manager.py / activated_repo_index_manager.py
/ refresh_scheduler.py) never passed an env= kwarg at all -- meaning the child
unconditionally inherited a RELATIVE PYTHONPATH from the server process
unchanged. Because PYTHONPATH resolution is relative to the CURRENT process's
cwd, and the child runs with cwd=repo_path, the relative entry re-anchors into
repo_path -- if the repo has its own src/-layout package colliding with a real
cidx dependency (e.g. click), the repo's package shadows the installed
dependency and `cidx stop` fails.
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, Mock, patch

import pytest

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)

_RELATIVE_PYTHONPATH = "./src"


@pytest.fixture
def manager():
    with tempfile.TemporaryDirectory() as tmp:
        m = ActivatedRepoManager(
            data_dir=tmp,
            golden_repo_manager=MagicMock(),
            background_job_manager=MagicMock(),
        )
        yield m


class TestStopCompositeServicesSanitizesPythonPath:
    def test_cidx_stop_receives_absolutized_pythonpath(
        self, monkeypatch, manager, tmp_path
    ):
        monkeypatch.setenv("PYTHONPATH", _RELATIVE_PYTHONPATH)
        expected_abs = os.path.abspath(_RELATIVE_PYTHONPATH)

        repo_path = tmp_path / "composite-repo"
        (repo_path / ".code-indexer").mkdir(parents=True)

        run_calls: list = []

        def _run(cmd, **kwargs):
            run_calls.append({"cmd": list(cmd), "kwargs": kwargs})
            return Mock(returncode=0, stdout="", stderr="")

        with patch(
            "code_indexer.server.repositories.activated_repo_manager.subprocess.run",
            side_effect=_run,
        ):
            manager._stop_composite_services(repo_path)

        stop_calls = [c for c in run_calls if c["cmd"] == ["cidx", "stop"]]
        assert stop_calls, f"expected a 'cidx stop' call, got: {run_calls}"
        stop_env = stop_calls[0]["kwargs"].get("env")
        assert stop_env is not None, "cidx stop must receive a sanitized env"
        assert stop_env["PYTHONPATH"] == expected_abs


class TestCloneWithCopyOnWriteFixConfigSanitizesPythonPath:
    def test_cidx_fix_config_receives_absolutized_pythonpath(
        self, monkeypatch, manager, tmp_path
    ):
        monkeypatch.setenv("PYTHONPATH", _RELATIVE_PYTHONPATH)
        expected_abs = os.path.abspath(_RELATIVE_PYTHONPATH)

        source_path = tmp_path / "source-repo"
        source_path.mkdir()
        dest_path = tmp_path / "dest-repo"

        def _fake_create_clone(src, dst, **kwargs):
            os.makedirs(dst, exist_ok=True)
            os.makedirs(os.path.join(dst, ".code-indexer"), exist_ok=True)
            return dst

        manager._clone_backend = Mock()
        manager._clone_backend.create_clone_at_path.side_effect = _fake_create_clone

        run_calls: list = []

        def _run(cmd, **kwargs):
            run_calls.append({"cmd": list(cmd), "kwargs": kwargs})
            return Mock(returncode=0, stdout="", stderr="")

        with patch(
            "code_indexer.server.repositories.activated_repo_manager.subprocess.run",
            side_effect=_run,
        ):
            result = manager._clone_with_copy_on_write(str(source_path), str(dest_path))

        assert result is True
        fix_config_calls = [
            c for c in run_calls if c["cmd"] == ["cidx", "fix-config", "--force"]
        ]
        assert fix_config_calls, (
            f"expected a 'cidx fix-config --force' call, got: {run_calls}"
        )
        fix_config_env = fix_config_calls[0]["kwargs"].get("env")
        assert fix_config_env is not None, (
            "cidx fix-config must receive a sanitized env"
        )
        assert fix_config_env["PYTHONPATH"] == expected_abs
