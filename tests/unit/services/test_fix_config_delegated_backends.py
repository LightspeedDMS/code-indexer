"""``cidx fix-config`` under delegated backends keeps the recorded git state.

The server spawns ``cidx fix-config --force`` on every new snapshot. On a pod
without the repository, its git-state check ran the POD's git, found no
repository, and rewrote ``git_available``/``current_branch``/``current_commit``
(and the git-remote-derived ``project_id``) that the index run had recorded
through the backends. The server now stamps ``--content-backend``/
``--git-backend`` on that spawn; under either delegated, fix-config proposes
no metadata fix and never runs git, while the rest of fix-config (config path
repair) still happens. Without the flags, behaviour is unchanged.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest
from click.testing import CliRunner

from code_indexer.config import Config, ConfigManager
from code_indexer.services import config_fixer
from code_indexer.services.config_fixer import ConfigurationRepairer

RECORDED: Dict[str, Any] = {
    "project_id": "recorded-project",
    "git_available": True,
    "current_branch": "main",
    "current_commit": "a" * 40,
    "files_processed": 7,
    "status": "completed",
}


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fix-config's legacy CoW-symlink step writes under ~/..collections."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def _project(tmp_path: Path, *, git: bool) -> Path:
    """A project whose config points elsewhere and whose metadata is recorded.

    The config keeps the legacy default vector store (none set), so every
    fix-config step runs, including the legacy collection-name check.
    """
    project = tmp_path / "snapshot"
    project.mkdir()
    if git:
        _git(project, "init", "-q", "-b", "trunk")
        _git(project, "config", "user.email", "t@example.com")
        _git(project, "config", "user.name", "T")
        (project / "a.py").write_text("x = 1\n")
        _git(project, "add", "-A")
        _git(project, "commit", "-q", "-m", "init")
    config_dir = project / ".code-indexer"
    config_dir.mkdir()
    ConfigManager(config_dir / "config.json").save(
        Config(codebase_dir=tmp_path / "base-clone")
    )
    (config_dir / "metadata.json").write_text(json.dumps(RECORDED))
    return project


def _metadata(project: Path) -> Dict[str, Any]:
    data: Dict[str, Any] = json.loads(
        (project / ".code-indexer" / "metadata.json").read_text()
    )
    return data


def _forbid_git(monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    calls: List[Any] = []
    real_run = subprocess.run

    def guarded_run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git":
            calls.append(cmd)
            raise AssertionError(f"local git under a delegated backend: {cmd}")
        return real_run(cmd, *args, **kwargs)

    def no_git_command(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        raise AssertionError("local git under a delegated backend")

    monkeypatch.setattr(config_fixer.subprocess, "run", guarded_run)
    monkeypatch.setattr(
        "code_indexer.services.file_identifier.run_git_command", no_git_command
    )
    return calls


def test_delegated_fix_config_keeps_recorded_git_state_and_never_runs_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, git=False)
    git_calls = _forbid_git(monkeypatch)

    result = ConfigurationRepairer(
        project / ".code-indexer", repository_backend_delegated=True
    ).fix_configuration()

    assert result.success, result.errors
    assert git_calls == []
    assert _metadata(project) == RECORDED
    fixed_fields = {fix.field for fix in result.fixes_applied}
    # The rest of fix-config still ran (the legacy CoW collection repair) ...
    assert fixed_fields
    # ... but nothing derived from the repository was touched.
    assert not fixed_fields & {
        "project_id",
        "git_available",
        "current_branch",
        "current_commit",
    }


def test_local_fix_config_still_rewrites_git_state_from_the_repository(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, git=True)

    result = ConfigurationRepairer(project / ".code-indexer").fix_configuration()

    assert result.success, result.errors
    metadata = _metadata(project)
    assert metadata["current_branch"] == "trunk"
    assert metadata["current_commit"] == _git(project, "rev-parse", "HEAD")
    assert metadata["files_processed"] == 7
    assert {"current_branch", "current_commit"} <= {
        fix.field for fix in result.fixes_applied
    }


def test_local_fix_config_on_a_non_repository_records_git_unavailable(
    tmp_path: Path,
) -> None:
    """The pre-existing local behaviour the delegated flag exists to avoid."""
    project = _project(tmp_path, git=False)

    ConfigurationRepairer(project / ".code-indexer").fix_configuration()

    assert _metadata(project)["git_available"] is False


class _RecordingRepairer:
    instances: List[Dict[str, Any]] = []

    def __init__(self, config_dir: Path, **kwargs: Any) -> None:
        _RecordingRepairer.instances.append(kwargs)

    def fix_configuration(self) -> Any:
        return config_fixer.FixResult(
            success=True, fixes_applied=[], errors=[], warnings=[]
        )


@pytest.mark.parametrize(
    "flags, delegated",
    [
        ([], False),
        (["--content-backend", "local", "--git-backend", "local"], False),
        (["--content-backend=delegated"], True),
        (["--git-backend=delegated"], True),
        (["--content-backend=delegated", "--git-backend=delegated"], True),
    ],
)
def test_cli_passes_the_backend_selection_to_the_repairer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flags: List[str],
    delegated: bool,
) -> None:
    from code_indexer.cli import cli

    project = _project(tmp_path, git=False)
    monkeypatch.chdir(project)
    monkeypatch.setattr(config_fixer, "ConfigurationRepairer", _RecordingRepairer)
    _RecordingRepairer.instances = []

    result = CliRunner().invoke(cli, ["fix-config", "--force", *flags])

    assert result.exit_code == 0, result.output
    assert _RecordingRepairer.instances == [
        {"dry_run": False, "repository_backend_delegated": delegated}
    ]
