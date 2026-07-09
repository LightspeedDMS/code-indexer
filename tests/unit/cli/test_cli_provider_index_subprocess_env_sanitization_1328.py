"""Story #1328: cidx subprocesses spawned by the `provider-index add`/`recreate`
CLI commands with cwd=<repo_path> must never inherit a RELATIVE PYTHONPATH
unchanged from the parent CLI process.

Root cause: when the CLI itself is launched via the documented dev command
(`PYTHONPATH=./src python3 ...`), that relative PYTHONPATH entry is inherited
unchanged by the `cidx index` child subprocess spawned by these commands.
Because PYTHONPATH resolution is relative to the CURRENT process's cwd, and
these children run with cwd=<repo_path>, the relative entry re-anchors into
the target repository directory. If the repo has its own `src/`-layout
package colliding with a real cidx dependency (e.g. `click`), the repo's
package shadows the installed dependency and cidx's own imports break --
the same class of failure fixed server-side for Bug #1325.

Fix: both call sites in cli_provider_index.py pass
env=build_cidx_subprocess_env() to subprocess.run, absolutizing any relative
PYTHONPATH entry before the child changes cwd to the repository.
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from code_indexer.cli_provider_index import provider_index_group

_RELATIVE_PYTHONPATH = "./src"


@pytest.fixture
def runner():
    """Create a Click CLI test runner."""
    return CliRunner()


@pytest.fixture
def repo_path(tmp_path):
    """Create a minimal initialized repo with a .code-indexer/config.json."""
    repo = tmp_path / "test-repo"
    cidx_dir = repo / ".code-indexer"
    cidx_dir.mkdir(parents=True)
    (cidx_dir / "config.json").write_text(json.dumps({}))
    return repo


def _mock_provider_index_service():
    """Return a MagicMock ProviderIndexService whose validate_provider passes."""
    service = MagicMock()
    service.validate_provider.return_value = None
    return service


@pytest.mark.parametrize(
    "subcommand",
    ["add", "recreate"],
)
def test_provider_index_command_absolutizes_relative_pythonpath(
    subcommand, runner, repo_path, monkeypatch
):
    """The cidx index subprocess.run call must receive an absolutized PYTHONPATH."""
    monkeypatch.setenv("PYTHONPATH", _RELATIVE_PYTHONPATH)
    expected_abs = os.path.abspath(_RELATIVE_PYTHONPATH)

    with (
        patch("subprocess.run") as mock_run,
        patch("code_indexer.config.ConfigManager") as mock_config_manager_cls,
        patch(
            "code_indexer.server.services.provider_index_service.ProviderIndexService"
        ) as mock_service_cls,
    ):
        mock_config_manager_cls.return_value.load.return_value = MagicMock()
        mock_service_cls.return_value = _mock_provider_index_service()
        mock_run.return_value = MagicMock(returncode=0)

        result = runner.invoke(
            provider_index_group,
            [subcommand, "--provider", "voyage-ai", "--repo", str(repo_path)],
        )

    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()
    call_kwargs = mock_run.call_args.kwargs
    env = call_kwargs.get("env")
    assert env is not None, "subprocess.run must receive an explicit env kwarg"
    assert env["PYTHONPATH"] == expected_abs
