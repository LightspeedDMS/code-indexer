"""Every server site that matches a caller-supplied pattern in Python shares a
bounded evaluation budget."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.timeout(60)
SLOW_PATTERN = r"(a|a)*b"
SLOW_NAME = "a" * 24 + "!b"


def test_python_multiline_regex_search_stops_within_its_timeout(
    tmp_path: Path,
) -> None:
    from code_indexer.global_repos.regex_search import RegexSearchService

    (tmp_path / "slow.txt").write_text((SLOW_NAME + "\n") * 3)
    service = RegexSearchService(tmp_path)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        service._search_python_multiline(
            SLOW_PATTERN, tmp_path, None, None, True, 100, timeout_seconds=1
        )
    assert time.monotonic() - started < 2.0


def _forge(jobs: Any, log_text: str) -> Any:
    """The forge HTTP service (external) answering one jobs listing and one
    job log."""
    from unittest.mock import AsyncMock, Mock

    service = AsyncMock()
    service.get.side_effect = [
        Mock(status_code=200, json=Mock(return_value=jobs)),
        Mock(status_code=200, text=log_text),
    ]
    service.__aenter__.return_value = service
    service.__aexit__.return_value = None
    return service


@pytest.mark.parametrize("forge", ["github", "gitlab"])
def test_ci_log_search_stops_within_its_budget(forge: str) -> None:
    from unittest.mock import patch

    from code_indexer.server.clients.github_actions_client import GitHubActionsClient
    from code_indexer.server.clients.gitlab_ci_client import GitLabCIClient

    log_text = (SLOW_NAME + "\n") * 4
    job = {"id": 1, "name": "build", "stage": "test"}
    if forge == "github":
        service = _forge({"jobs": [job]}, log_text)
        search = GitHubActionsClient(token="example-token").search_logs(
            repository="example/repo", run_id=1, pattern=SLOW_PATTERN
        )
    else:
        service = _forge([job], log_text)
        search = GitLabCIClient("example-token").search_logs(
            project_id="example/repo", pipeline_id=1, pattern=SLOW_PATTERN
        )
    started = time.monotonic()
    with patch("httpx.AsyncClient", return_value=service):
        with pytest.raises(TimeoutError):
            asyncio.run(search)
    assert time.monotonic() - started < 2.5


def test_omni_repository_regex_stops_within_its_budget() -> None:
    from code_indexer.server.omni.repo_pattern_matcher import RepoPatternMatcher

    repos = ["fast-ab"] + [f"{SLOW_NAME}{n}" for n in range(3)]
    started = time.monotonic()
    assert RepoPatternMatcher([SLOW_PATTERN]).filter_repos(repos) == ["fast-ab"]
    assert time.monotonic() - started < 2.0


def test_xray_filename_driver_stops_within_its_walk_budget(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")
    from code_indexer.xray.search_engine import XRayPhase1TimeoutError, XRaySearchEngine

    for n in range(3):
        (tmp_path / f"{SLOW_NAME}{n}.py").write_text("x = 1\n")
    started = time.monotonic()
    with pytest.raises(XRayPhase1TimeoutError):
        XRaySearchEngine()._run_phase1_filename(
            repo_path=tmp_path,
            driver_regex=SLOW_PATTERN,
            include_patterns=[],
            exclude_patterns=[],
            timeout_seconds=1,
        )
    assert time.monotonic() - started < 2.0
