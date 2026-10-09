"""cidx-meta repository descriptions carry the repository URL with its
userinfo redacted (the description file is indexed and globally queryable).

The real ``_generate_repo_description`` and ``RepoAnalyzer.extract_info``
run; only the external Claude CLI extraction
(``RepoAnalyzer._extract_info_with_claude``) returns a fixed RepoInfo.

Hosts and secrets are neutral placeholders.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from code_indexer.global_repos.meta_description_hook import _generate_repo_description
from code_indexer.global_repos.repo_analyzer import RepoAnalyzer, RepoInfo
from code_indexer.server.services.claude_cli_manager import ClaudeCliManager

SECRET = "example-token-123"
URL_USER = "example-user"
USERINFO_URL = f"https://{URL_USER}:{SECRET}@git.example.com/example/repo.git"
REDACTED_URL = "https://***@git.example.com/example/repo.git"


def _fixed_info(_self: RepoAnalyzer) -> RepoInfo:
    return RepoInfo(
        summary="An example library.",
        technologies=["Python"],
        features=["example feature"],
        use_cases=["example use case"],
        purpose="library",
    )


def test_description_carries_redacted_repo_url(tmp_path: Path) -> None:
    repo_dir = tmp_path / "example-repo"
    repo_dir.mkdir()
    (repo_dir / "README.md").write_text("# Example\n")
    # The Claude CLI manager is only handed through to the (replaced)
    # external extraction; spec= satisfies the generator's type guard.
    cli_manager = MagicMock(spec=ClaudeCliManager)

    with patch.object(RepoAnalyzer, "_extract_info_with_claude", _fixed_info):
        content = _generate_repo_description(
            "example-repo", USERINFO_URL, str(repo_dir), cli_manager
        )

    assert SECRET not in content, content
    assert f"{URL_USER}:" not in content, content
    assert f"{URL_USER}@" not in content, content
    _, frontmatter_text, body = content.split("---\n", 2)
    assert yaml.safe_load(frontmatter_text)["url"] == REDACTED_URL
    assert f"**Repository URL**: {REDACTED_URL}" in body
