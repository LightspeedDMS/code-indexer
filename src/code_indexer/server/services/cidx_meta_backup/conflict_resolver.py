"""Claude-assisted git conflict resolution for Story #926."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from code_indexer.global_repos.repo_analyzer import invoke_claude_cli

_PROMPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "mcp"
    / "prompts"
    / "cidx_meta_conflict_resolution.md"
)


@dataclass
class ResolverResult:
    success: bool
    error: Optional[str]


def _load_prompt() -> str:
    if not _PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"cidx_meta_conflict_resolution.md prompt not found at {_PROMPT_PATH}"
        )
    return _PROMPT_PATH.read_text(encoding="utf-8")


class ClaudeConflictResolver:
    """Resolve git rebase conflicts inside cidx-meta using Claude CLI."""

    def __init__(self) -> None:
        self._prompt_template = _load_prompt()

    def resolve(
        self, cidx_meta_path: str, conflict_files: List[str], branch: str
    ) -> ResolverResult:
        prompt = self._prompt_template.format(
            conflict_files="\n".join(conflict_files),
            branch=branch,
            repo_path=cidx_meta_path,
        )
        success, output = invoke_claude_cli(
            repo_path=cidx_meta_path,
            prompt=prompt,
            shell_timeout_seconds=540,
            outer_timeout_seconds=600,
        )
        if not success:
            return ResolverResult(success=False, error=output)

        unmerged = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=cidx_meta_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if unmerged.stdout.strip():
            return ResolverResult(
                success=False, error="Claude did not resolve all conflicts"
            )
        return ResolverResult(success=True, error=None)
