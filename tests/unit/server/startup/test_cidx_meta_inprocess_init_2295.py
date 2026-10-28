from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Tuple
from unittest.mock import MagicMock

import pytest

from tests.utils.real_process_spawn_recorder import record_cidx_spawns


def _assert_initialised(project: Path, *, with_override: bool) -> None:
    index_dir = project / ".code-indexer"
    config = json.loads((index_dir / "config.json").read_text())
    assert config["codebase_dir"] == str(project.resolve())
    assert config["embedding_provider"] == "voyage-ai"
    assert config["voyage_ai"]["model"] == "voyage-code-3"
    assert (index_dir / "README.md").is_file()
    assert (index_dir / "language-mappings.yaml").is_file()
    assert (index_dir / "index").is_dir()
    assert (index_dir / "filesystem").is_dir()
    assert (project / ".code-indexer-override.yaml").exists() is with_override


Entry = Tuple[str, int, str]

# ^ invariant entries = pre-change `cidx init` output under umask 022, project path replaced by <ROOT>; regenerate only for an intended change
_PRE_CHANGE_CIDX_INIT: Dict[str, Entry] = {
    ".code-indexer": ("d", 0o755, ""),
    ".code-indexer/README.md": (
        "f",
        0o644,
        "46ed5a179ea16a27a3b1e9efdc57be2005bd6455f8594f62a3f1eea702b86b28",
    ),
    ".code-indexer/config.json": (
        "f",
        0o644,
        "d5d539dd168b66996669468fab08b8c7e3d1bba86671b3e4c2c13a9c3d6369e6",
    ),
    ".code-indexer/filesystem": ("d", 0o755, ""),
    ".code-indexer/index": ("d", 0o755, ""),
    ".code-indexer/language-mappings.yaml": (
        "f",
        0o644,
        "2c53b349df38bd30e982d83a3db6090b2b4c1b1bdabf851ab13cc51eb9eeb21e",
    ),
}
_PRE_CHANGE_OVERRIDE_FILE: Dict[str, Entry] = {
    ".code-indexer-override.yaml": (
        "f",
        0o644,
        "1c37b6653b215bb923c9a6909d57735ce30b4c31ff3dfb75d3efae8763938adf",
    ),
}
_VARIANTS = [
    pytest.param(False, id="override"),
    pytest.param(True, id="no-override"),
]


def _expected(no_override_file: bool) -> Dict[str, Entry]:
    if no_override_file:
        return dict(_PRE_CHANGE_CIDX_INIT)
    return {**_PRE_CHANGE_CIDX_INIT, **_PRE_CHANGE_OVERRIDE_FILE}


def _snapshot(root: Path) -> Dict[str, Entry]:
    real_root = root.resolve()
    entries: Dict[str, Entry] = {}
    for path in sorted(real_root.rglob("*")):
        mode = stat.S_IMODE(path.lstat().st_mode)
        rel = str(path.relative_to(real_root))
        if path.is_dir():
            entries[rel] = ("d", mode, "")
        else:
            body = path.read_bytes().replace(str(real_root).encode(), b"<ROOT>")
            entries[rel] = ("f", mode, hashlib.sha256(body).hexdigest())
    return entries


@contextmanager
def _umask_022() -> Iterator[None]:
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.mark.parametrize("no_override_file", _VARIANTS)
def test_in_process_init_matches_pre_change_cidx_init(tmp_path, no_override_file):
    from code_indexer.services.local_project_init import initialize_local_project

    with _umask_022():
        initialize_local_project(tmp_path.resolve(), no_override_file=no_override_file)

    assert _snapshot(tmp_path) == _expected(no_override_file)


@pytest.mark.slow
@pytest.mark.parametrize("no_override_file", _VARIANTS)
def test_cidx_init_process_matches_pre_change_cidx_init(tmp_path, no_override_file):
    flags = ["--no-override-file"] if no_override_file else []
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parents[4] / "src"), env.get("PYTHONPATH", "")]
    )
    env["COLUMNS"] = "500"
    with _umask_022():
        result = subprocess.run(
            [sys.executable, "-m", "code_indexer.cli", "init", *flags],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
        )

    assert result.returncode == 0, result.stderr
    assert "Initialized filesystem vector storage backend" in result.stdout
    assert "Created language-mappings.yaml" in result.stdout
    assert ("Created .code-indexer-override.yaml" in result.stdout) is (
        not no_override_file
    )
    assert _snapshot(tmp_path) == _expected(no_override_file)


@pytest.mark.parametrize("no_override_file", _VARIANTS)
def test_cli_init_reports_what_it_wrote(tmp_path, monkeypatch, no_override_file):
    from click.testing import CliRunner

    from code_indexer.cli import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLUMNS", "500")
    flags = ["--no-override-file"] if no_override_file else []
    runner = CliRunner()

    first = runner.invoke(cli, ["init", *flags])
    second = runner.invoke(cli, ["init", *flags])

    assert first.exit_code == 0, first.output
    assert "Initialized filesystem vector storage backend" in first.output
    assert ("Created .code-indexer-override.yaml" in first.output) is (
        not no_override_file
    )
    assert "Created language-mappings.yaml" in first.output
    assert "Initialized configuration at" in first.output
    assert second.exit_code == 1, second.output
    assert "Configuration already exists" in second.output


def _manager(already_registered: bool) -> MagicMock:
    manager = MagicMock()
    manager.golden_repo_exists.return_value = already_registered
    return manager


def test_bootstrap_initializes_cidx_meta_without_a_cidx_process(tmp_path, monkeypatch):
    from code_indexer.server.startup.bootstrap import bootstrap_cidx_meta

    spawns = record_cidx_spawns(tmp_path, monkeypatch)
    bootstrap_cidx_meta(_manager(already_registered=False), str(tmp_path))

    assert spawns() == []
    _assert_initialised(tmp_path / "cidx-meta", with_override=True)


def test_register_local_repo_initializes_without_a_cidx_process(tmp_path, monkeypatch):
    from code_indexer.server.repositories.golden_repo_manager import (
        GoldenRepoManager,
    )

    manager = GoldenRepoManager(data_dir=str(tmp_path / "data"))
    folder = tmp_path / "data" / "golden-repos" / "local-repo"
    folder.mkdir(parents=True)
    spawns = record_cidx_spawns(tmp_path, monkeypatch)

    manager.register_local_repo(
        alias="local-repo", folder_path=folder, fire_lifecycle_hooks=False
    )

    assert spawns() == []
    _assert_initialised(folder, with_override=False)


_INIT_MODULE = "code_indexer.services.local_project_init"


def _error_messages(caplog) -> List[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


def test_bootstrap_logs_and_continues_when_init_cannot_be_imported(
    tmp_path, monkeypatch, caplog
):
    from code_indexer.server.startup.bootstrap import bootstrap_cidx_meta

    monkeypatch.setitem(sys.modules, _INIT_MODULE, None)
    manager = _manager(already_registered=False)

    with caplog.at_level(logging.ERROR):
        bootstrap_cidx_meta(manager, str(tmp_path))

    manager.register_local_repo.assert_called_once()
    assert any("Failed to initialize cidx-meta" in m for m in _error_messages(caplog))
    assert not (tmp_path / "cidx-meta" / ".code-indexer").exists()


def test_register_local_repo_logs_and_continues_when_init_cannot_be_imported(
    tmp_path, monkeypatch, caplog
):
    from code_indexer.server.repositories.golden_repo_manager import (
        GoldenRepoManager,
    )

    manager = GoldenRepoManager(data_dir=str(tmp_path / "data"))
    folder = tmp_path / "data" / "golden-repos" / "local-repo"
    folder.mkdir(parents=True)
    monkeypatch.setitem(sys.modules, _INIT_MODULE, None)

    with caplog.at_level(logging.ERROR):
        registered = manager.register_local_repo(
            alias="local-repo", folder_path=folder, fire_lifecycle_hooks=False
        )

    assert registered is True
    assert any(
        "Failed to initialize CIDX for 'local-repo'" in m
        for m in _error_messages(caplog)
    )
    assert not (folder / ".code-indexer").exists()
