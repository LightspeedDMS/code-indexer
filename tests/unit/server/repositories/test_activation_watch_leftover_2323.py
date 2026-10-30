"""#2323: an activation whose destination holds only what a server watch
leaves behind (no registry row, no clone) removes that leftover and proceeds;
anything else at the destination keeps the refusal."""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoError,
    ActivatedRepoManager,
)
from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend

pytestmark = [pytest.mark.slow, pytest.mark.timeout(300)]

USER = "example-user"
ALIAS = "example-repo"


class _UnusedJobs:
    pass


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def manager(tmp_path: Path) -> Iterator[ActivatedRepoManager]:
    golden_path = tmp_path / "golden-repos" / "example-golden"
    golden_path.mkdir(parents=True)
    (golden_path / "module.py").write_text("def f():\n    return 1\n")
    _git(golden_path, "init", "-q", "-b", "main")
    _git(golden_path, "add", "-A")
    _git(golden_path, "commit", "-qm", "init")
    golden = GoldenRepoManager(data_dir=str(tmp_path))
    golden._sqlite_backend.add_repo(
        alias="example-golden",
        repo_url=str(golden_path),
        default_branch="main",
        clone_path=str(golden_path),
        created_at=datetime.now(timezone.utc).isoformat(),
        enable_temporal=False,
        temporal_options=None,
    )
    yield ActivatedRepoManager(
        data_dir=str(tmp_path),
        golden_repo_manager=golden,
        # ^ why an unused stand-in; the activation worker is called directly, never submitted
        background_job_manager=_UnusedJobs(),  # type: ignore[arg-type]
        clone_backend=LocalCloneBackend(),
    )


def _activate(manager: ActivatedRepoManager) -> dict:
    return manager._do_activate_repository(
        username=USER,
        golden_repo_alias="example-golden",
        branch_name="main",
        user_alias=ALIAS,
    )


def _watch_leftover(manager: ActivatedRepoManager) -> Path:
    workspace = Path(manager.get_activated_repo_path(USER, ALIAS))
    (workspace / ".code-indexer").mkdir(parents=True)
    (workspace / ".code-indexer" / "watch_metadata.json").write_text("{}")
    (workspace / ".code-indexer" / "indexing_heartbeat.json").write_text("{}")
    return workspace


def test_activation_removes_a_watch_leftover_and_succeeds(
    manager: ActivatedRepoManager, caplog: pytest.LogCaptureFixture
) -> None:
    workspace = _watch_leftover(manager)
    caplog.set_level(logging.INFO)

    assert _activate(manager)["success"] is True

    assert (workspace / ".git").is_dir()
    assert (workspace / "module.py").is_file()
    leftover_lines = [r for r in caplog.records if "leftover" in r.getMessage()]
    assert len(leftover_lines) == 1
    assert leftover_lines[0].levelno == logging.INFO
    assert ALIAS in leftover_lines[0].getMessage()
    assert USER not in leftover_lines[0].getMessage()


def test_activation_refuses_a_destination_holding_a_real_clone(
    manager: ActivatedRepoManager,
) -> None:
    workspace = _watch_leftover(manager)
    (workspace / ".git").mkdir()
    (workspace / "work.py").write_text("unsaved = True\n")

    with pytest.raises(ActivatedRepoError, match="already exists"):
        _activate(manager)

    assert (workspace / "work.py").read_text() == "unsaved = True\n"
    assert (workspace / ".code-indexer" / "watch_metadata.json").exists()


def test_activation_keeps_a_leftover_shaped_dir_of_a_registered_repo(
    manager: ActivatedRepoManager,
) -> None:
    workspace = _watch_leftover(manager)
    manager._save_metadata(
        USER, ALIAS, {"username": USER, "user_alias": ALIAS, "activation_id": "x"}
    )

    with pytest.raises(ActivatedRepoError, match="already exists"):
        _activate(manager)

    assert (workspace / ".code-indexer" / "watch_metadata.json").exists()
