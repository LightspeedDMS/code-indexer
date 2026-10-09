"""A description refresh rewrites a cidx-meta description written before
repository URLs were redacted into the redacted form, and never hands the
URL's userinfo to the model.

Real LifecycleBatchRunner._process_one_repo, real files and the real
writer; only the Claude CLI invoker (the external model boundary) is a
recording stand-in that refines by carrying the existing text forward.

Hosts and secrets are neutral placeholders.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from code_indexer.global_repos.lifecycle_batch_runner import LifecycleBatchRunner
from code_indexer.global_repos.repo_analyzer import split_frontmatter_and_body
from code_indexer.global_repos.unified_response_parser import UnifiedResult

SECRET = "example-token-123"
USERINFO_URL = f"https://example-user:{SECRET}@git.example.com/example/repo.git"
REDACTED_URL = "https://***@git.example.com/example/repo.git"
ALIAS = "example-repo"


class _Scheduler:
    def acquire_write_lock(self, key: str, owner_name: str) -> bool:
        return True

    def release_write_lock(self, key: str, owner_name: str) -> None:
        pass


class _JobTracker:
    def update_status(self, job_id: str, **kwargs: Any) -> None:
        pass

    def complete_job(self, job_id: str, result: Optional[Dict] = None) -> None:
        pass

    def fail_job(self, job_id: str, error: str) -> None:
        pass


class _Debouncer:
    def signal_dirty(self) -> None:
        pass


class _CarryForwardInvoker:
    """A model that keeps the existing description text in its refinement."""

    def __init__(self) -> None:
        self.existing: List[Optional[str]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> UnifiedResult:
        existing = kwargs.get("existing_description")
        self.existing.append(existing)
        return UnifiedResult(
            description=(existing or "").strip() + "\n\nRefined.",
            lifecycle={"ci_system": "none", "confidence": "low"},
        )


def test_refresh_rewrites_an_old_description_with_redacted_url(
    tmp_path: Path,
) -> None:
    (tmp_path / "cidx-meta").mkdir()
    (tmp_path / ALIAS).mkdir()
    meta_md = tmp_path / "cidx-meta" / f"{ALIAS}.md"
    old_frontmatter = {"name": ALIAS, "url": USERINFO_URL, "purpose": "library"}
    meta_md.write_text(
        "---\n"
        + yaml.dump(old_frontmatter, default_flow_style=False)
        + "---\n\n"
        + f"# {ALIAS}\n\nAn example.\n\n**Repository URL**: {USERINFO_URL}\n",
        encoding="utf-8",
    )
    invoker = _CarryForwardInvoker()
    runner = LifecycleBatchRunner(
        golden_repos_dir=tmp_path,
        job_tracker=_JobTracker(),
        refresh_scheduler=_Scheduler(),
        debouncer=_Debouncer(),
        claude_cli_invoker=invoker,
        concurrency=1,
        sub_batch_size_override=10,
    )

    runner._process_one_repo(ALIAS, "job-example")

    assert invoker.existing and SECRET not in (invoker.existing[0] or "")
    content = meta_md.read_text(encoding="utf-8")
    assert SECRET not in content, content
    frontmatter, body = split_frontmatter_and_body(content)
    assert frontmatter["url"] == REDACTED_URL
    assert f"**Repository URL**: {REDACTED_URL}" in body
