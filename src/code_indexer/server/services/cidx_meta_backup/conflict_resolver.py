"""LLM-assisted git conflict resolution for Story #926 (Bug #936 dispatcher migration)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from code_indexer.global_repos.repo_analyzer import invoke_claude_cli  # noqa: F401 — kept for backward-compat test patching
from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.services.dep_map_dispatcher_factory import (
    build_dep_map_dispatcher,
)

_PROMPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "mcp"
    / "prompts"
    / "cidx_meta_conflict_resolution.md"
)

# Default outer timeout for conflict resolution dispatches.
# Used when CidxMetaBackupConfig does not expose conflict_resolution_timeout_seconds.
_DEFAULT_CONFLICT_TIMEOUT = 600


@dataclass
class ResolverResult:
    success: bool
    error: Optional[str]


def _strip_yaml_frontmatter(text: str) -> str:
    """Strip leading ``---\\n...\\n---\\n`` YAML frontmatter from a prompt.

    CLI 2.1.119 echoes the prompt back without invoking tools when the
    prompt starts with YAML frontmatter (it appears to interpret the `---`
    markers as session metadata). The frontmatter exists for prompt-loader
    documentation only; it is not meant to be sent to the model.
    """
    if not text.startswith("---\n"):
        return text
    closing = text.find("\n---\n", 4)
    if closing == -1:
        return text
    return text[closing + len("\n---\n") :].lstrip("\n")


def _load_prompt() -> str:
    if not _PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"cidx_meta_conflict_resolution.md prompt not found at {_PROMPT_PATH}"
        )
    return _strip_yaml_frontmatter(_PROMPT_PATH.read_text(encoding="utf-8"))


class ClaudeConflictResolver:
    """Resolve git rebase conflicts inside cidx-meta using an LLM via CliDispatcher."""

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
        # Bug #936: route through dispatcher (Claude or Codex) instead of
        # calling invoke_claude_cli directly.
        config = get_config_service().get_config()
        timeout: int = getattr(
            getattr(config, "cidx_meta_backup_config", None),
            "conflict_resolution_timeout_seconds",
            _DEFAULT_CONFLICT_TIMEOUT,
        )
        dispatcher = build_dep_map_dispatcher(config)
        result = dispatcher.dispatch(
            flow="cidx_meta_conflict",
            cwd=cidx_meta_path,
            prompt=prompt,
            timeout=timeout,
        )
        if not result.success:
            return ResolverResult(success=False, error=result.error or result.output)

        unmerged = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=cidx_meta_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if unmerged.stdout.strip():
            return ResolverResult(
                success=False,
                error="Conflict resolver did not resolve all conflicts",
            )
        return ResolverResult(success=True, error=None)
