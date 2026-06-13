"""
Unit tests for #1094 — LifecycleBatchRunner._process_one_repo refresh-awareness.

The runner must, BEFORE invoking the Claude CLI:
  - Read cidx-meta/<alias>.md, extract its body + last_analyzed, and forward them
    to the invoker as the keyword arguments existing_description / last_analyzed
    so the LLM REFINES the existing description instead of replacing it.
  - When the .md is missing (or its body is empty/whitespace), call the invoker
    in CREATE mode (existing_description=None).
  - Raise on corrupt frontmatter BEFORE burning a Claude invocation (the invoker
    must NOT be called in that case).

After a successful write the runner must stamp a FRESH last_analyzed (UTC ISO
8601) into the frontmatter so the next refresh has an accurate change-scoping
anchor.

Real file writes + real split_frontmatter_and_body (Messi Rule #1).  Only the
invoker callable is a recording stub.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml

from code_indexer.global_repos.lifecycle_batch_runner import LifecycleBatchRunner
from code_indexer.global_repos.repo_analyzer import split_frontmatter_and_body
from code_indexer.global_repos.unified_response_parser import UnifiedResult


_LIFECYCLE_NEW: Dict[str, Any] = {
    "ci_system": "github-actions",
    "deployment_target": "kubernetes",
    "language_ecosystem": "python/poetry",
    "build_system": "poetry",
    "testing_framework": "pytest",
    "confidence": "high",
}

_NEW_DESCRIPTION = "Refined description produced by the refresh-aware invoker."


class _StubScheduler:
    def acquire_write_lock(self, key: str, owner_name: str) -> bool:
        return True

    def release_write_lock(self, key: str, owner_name: str) -> None:
        pass


class _StubJobTracker:
    def update_status(self, job_id: str, **kwargs: Any) -> None:
        pass

    def complete_job(self, job_id: str, result: Optional[Dict] = None) -> None:
        pass

    def fail_job(self, job_id: str, error: str) -> None:
        pass


class _StubDebouncer:
    def signal_dirty(self) -> None:
        pass


class _RecordingInvoker:
    """Records the (args, kwargs) of each call; returns a fixed UnifiedResult."""

    def __init__(self) -> None:
        self.calls: List[Tuple[tuple, dict]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> UnifiedResult:
        self.calls.append((args, dict(kwargs)))
        return UnifiedResult(
            description=_NEW_DESCRIPTION,
            lifecycle=dict(_LIFECYCLE_NEW),
        )


def _make_runner(
    golden_repos_dir: Path, invoker: _RecordingInvoker
) -> LifecycleBatchRunner:
    return LifecycleBatchRunner(
        golden_repos_dir=golden_repos_dir,
        job_tracker=_StubJobTracker(),
        refresh_scheduler=_StubScheduler(),
        debouncer=_StubDebouncer(),
        claude_cli_invoker=invoker,
        concurrency=1,
        sub_batch_size_override=10,
    )


def _write_md(path: Path, frontmatter: Dict[str, Any], body: str) -> None:
    fm_yaml = yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True)
    path.write_text(f"---\n{fm_yaml}---\n\n{body}\n", encoding="utf-8")


@pytest.fixture()
def golden_repos_dir(tmp_path: Path) -> Path:
    (tmp_path / "cidx-meta").mkdir(parents=True)
    return tmp_path


# ---------------------------------------------------------------------------
# 1. Existing .md body forwarded to the invoker as existing_description
# ---------------------------------------------------------------------------


def test_existing_body_forwarded_to_invoker(golden_repos_dir: Path) -> None:
    alias = "acme"
    (golden_repos_dir / alias).mkdir()
    meta_path = golden_repos_dir / "cidx-meta" / f"{alias}.md"

    existing_body = (
        "Existing description mentioning FrobnicationProtocol and planner.core."
    )
    _write_md(
        meta_path,
        {"last_analyzed": "2026-01-15T00:00:00+00:00", "name": "acme"},
        existing_body,
    )

    invoker = _RecordingInvoker()
    runner = _make_runner(golden_repos_dir, invoker)
    runner._process_one_repo(alias, "job-1094")

    assert len(invoker.calls) == 1
    args, kwargs = invoker.calls[0]
    # The existing body must be forwarded for refinement.
    assert "existing_description" in kwargs
    assert kwargs["existing_description"] is not None
    assert "FrobnicationProtocol" in kwargs["existing_description"]
    assert "planner.core" in kwargs["existing_description"]
    # The last_analyzed marker must be forwarded for change-scoping.
    assert kwargs.get("last_analyzed") == "2026-01-15T00:00:00+00:00"


# ---------------------------------------------------------------------------
# 2. Missing .md -> create mode (no existing_description)
# ---------------------------------------------------------------------------


def test_missing_md_invokes_create_mode(golden_repos_dir: Path) -> None:
    alias = "fresh"
    (golden_repos_dir / alias).mkdir()
    # No .md file pre-created.

    invoker = _RecordingInvoker()
    runner = _make_runner(golden_repos_dir, invoker)
    runner._process_one_repo(alias, "job-1094")

    assert len(invoker.calls) == 1
    _args, kwargs = invoker.calls[0]
    # Create mode: existing_description must be falsy (None or empty).
    assert not kwargs.get("existing_description")


def test_empty_body_md_invokes_create_mode(golden_repos_dir: Path) -> None:
    alias = "emptybody"
    (golden_repos_dir / alias).mkdir()
    meta_path = golden_repos_dir / "cidx-meta" / f"{alias}.md"
    # Frontmatter present but body whitespace-only.
    _write_md(meta_path, {"last_analyzed": "2026-01-15T00:00:00+00:00"}, "   ")

    invoker = _RecordingInvoker()
    runner = _make_runner(golden_repos_dir, invoker)
    runner._process_one_repo(alias, "job-1094")

    assert len(invoker.calls) == 1
    _args, kwargs = invoker.calls[0]
    assert not kwargs.get("existing_description")


# ---------------------------------------------------------------------------
# 3. Corrupt frontmatter raises BEFORE the CLI invocation
# ---------------------------------------------------------------------------


def test_corrupt_frontmatter_raises_before_invocation(golden_repos_dir: Path) -> None:
    alias = "corrupt"
    (golden_repos_dir / alias).mkdir()
    meta_path = golden_repos_dir / "cidx-meta" / f"{alias}.md"
    # Starts with '---' but the YAML is unparseable (tab indentation under a key).
    corrupt_content = (
        "---\n"
        "last_analyzed: 2026-04-28T10:00:00Z\n"
        "bad:\n\t- not valid yaml indentation\n"
        "---\n\nbody\n"
    )
    meta_path.write_text(corrupt_content, encoding="utf-8")

    invoker = _RecordingInvoker()
    runner = _make_runner(golden_repos_dir, invoker)

    with pytest.raises(Exception):
        runner._process_one_repo(alias, "job-1094")

    # Critical: the corrupt file must be detected BEFORE the Claude CLI is called,
    # so we never burn an invocation on a repo whose metadata we cannot merge.
    assert len(invoker.calls) == 0, (
        "invoker must NOT be called when frontmatter is corrupt"
    )


# ---------------------------------------------------------------------------
# 4. last_analyzed is freshly stamped on successful write
# ---------------------------------------------------------------------------


def test_last_analyzed_freshly_stamped(golden_repos_dir: Path) -> None:
    alias = "stamp"
    (golden_repos_dir / alias).mkdir()
    meta_path = golden_repos_dir / "cidx-meta" / f"{alias}.md"
    stale = "2020-01-01T00:00:00+00:00"
    _write_md(meta_path, {"last_analyzed": stale, "name": "stamp"}, "Old body.")

    before = datetime.now(timezone.utc)
    invoker = _RecordingInvoker()
    runner = _make_runner(golden_repos_dir, invoker)
    runner._process_one_repo(alias, "job-1094")
    after = datetime.now(timezone.utc)

    content = meta_path.read_text(encoding="utf-8")
    fm, _ = split_frontmatter_and_body(content)
    assert "last_analyzed" in fm
    stamped_raw = fm["last_analyzed"]
    # Must NOT be the stale value anymore.
    assert str(stamped_raw) != stale, "last_analyzed must be refreshed, not preserved"
    # Must be a fresh UTC timestamp within the call window.
    stamped = datetime.fromisoformat(str(stamped_raw))
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=timezone.utc)
    assert before <= stamped <= after, (
        f"stamped last_analyzed {stamped} not within [{before}, {after}]"
    )
    # Original non-lifecycle key must still be preserved.
    assert fm.get("name") == "stamp"


def test_last_analyzed_stamped_on_first_write(golden_repos_dir: Path) -> None:
    """Even a brand-new .md (create mode) gets a last_analyzed stamp."""
    alias = "firstwrite"
    (golden_repos_dir / alias).mkdir()
    meta_path = golden_repos_dir / "cidx-meta" / f"{alias}.md"

    before = datetime.now(timezone.utc)
    invoker = _RecordingInvoker()
    runner = _make_runner(golden_repos_dir, invoker)
    runner._process_one_repo(alias, "job-1094")
    after = datetime.now(timezone.utc)

    content = meta_path.read_text(encoding="utf-8")
    fm, _ = split_frontmatter_and_body(content)
    assert "last_analyzed" in fm
    stamped = datetime.fromisoformat(str(fm["last_analyzed"]))
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=timezone.utc)
    assert before <= stamped <= after
