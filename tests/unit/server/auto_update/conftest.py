"""Bug #2028: DeploymentExecutor.execute() runs the real `git config --global`
safe.directory self-heal, so every test here gets a throwaway global git
config (see tests/fixtures/scratch_global_git_config.py)."""

from __future__ import annotations

from typing import Iterator, List, Tuple

import pytest

from tests.fixtures.scratch_global_git_config import (
    git_identity,
    point_global_git_config_at_scratch,
)


@pytest.fixture(scope="session")
def _developer_git_identity() -> List[Tuple[str, str]]:
    return git_identity()


@pytest.fixture(autouse=True)
def _scratch_global_git_config(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    _developer_git_identity: List[Tuple[str, str]],
) -> Iterator[None]:
    point_global_git_config_at_scratch(
        tmp_path_factory.mktemp("global-git"), monkeypatch, _developer_git_identity
    )
    yield


@pytest.fixture(autouse=True)
def _scratch_run_once_redeploy_marker(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug #2064: run_once's retry branch unlinks PENDING_REDEPLOY_MARKER, a
    constant bound at import time to the developer's real ~/.cidx-server.
    Point it at scratch so no test can consume a real marker."""
    from code_indexer.server.auto_update import run_once

    scratch = tmp_path_factory.mktemp("redeploy-marker") / "pending-redeploy"
    monkeypatch.setattr(run_once, "PENDING_REDEPLOY_MARKER", scratch)
