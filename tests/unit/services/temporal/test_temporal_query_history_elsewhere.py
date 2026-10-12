"""Temporal queries refuse, rather than run local git, where history is not here.

Two temporal query paths read git history with LOCAL git: ``at_commit``
(``resolve_commit_timestamp``) and the content of added/deleted files stored as
pointers (``_reconstruct_temporal_content``). On a server whose content backend
is delegated the pod holds no repository, so local git answered "not a git
repository" -- reported to the caller as "at_commit does not resolve to a
commit in this repository" (false: the commit exists) and shown as a git error
in place of content. Both now say why, without spawning git. A temporal index
can outlive the mount: refresh stops refreshing it there, it does not delete it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from code_indexer.server.startup import content_backend_wiring
from code_indexer.services.temporal import temporal_search_service as tss


class _Delegated:
    is_local = False


@pytest.fixture
def no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """A delegated server; any git spawn from the temporal query path fails."""
    monkeypatch.setattr(
        content_backend_wiring, "server_content_backend", lambda: _Delegated()
    )

    def spawn(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"local git spawned: {args[0] if args else kwargs}")

    monkeypatch.setattr(tss.subprocess, "run", spawn)


def test_at_commit_says_the_history_is_elsewhere(no_git: None, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not on this server pod"):
        tss.resolve_commit_timestamp(tmp_path, "c0ffee1")


def test_pointer_content_says_the_history_is_elsewhere(
    no_git: None, tmp_path: Path
) -> None:
    service = tss.TemporalSearchService.__new__(tss.TemporalSearchService)
    service.project_root = tmp_path
    content = service._reconstruct_temporal_content(
        {
            "reconstruct_from_git": True,
            "diff_type": "added",
            "path": "src/a.py",
            "commit_hash": "c0ffee1",
        }
    )
    assert content == f"[Content unavailable - {tss.HISTORY_ELSEWHERE}]"


def test_outside_a_delegated_server_local_git_still_answers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(content_backend_wiring, "server_content_backend", lambda: None)
    run = MagicMock(return_value=MagicMock(returncode=1, stdout="", stderr=""))
    monkeypatch.setattr(tss.subprocess, "run", run)
    with pytest.raises(ValueError, match="does not resolve to a commit"):
        tss.resolve_commit_timestamp(tmp_path, "c0ffee1")
    assert run.call_args[0][0][:2] == ["git", "rev-parse"]
