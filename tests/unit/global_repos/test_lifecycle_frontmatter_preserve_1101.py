"""
Unit tests for Bug #1101 — lifecycle frontmatter preserve-by-default merge.

Defect 1 (primary): On refresh, the lifecycle sub-dict was entirely replaced by
whatever the model returned, even when the model silently omitted or degraded
existing keys.  The fix introduces _merge_lifecycle_dict, a preserve-by-default
deep merge for the lifecycle sub-dict: existing sub-keys survive unless the new
analysis provides a present, non-empty contradicting value.

Defect 2 (secondary): The refresh addendum must instruct the model to silently
REMOVE fabricated claims rather than writing negations that name the false
feature and pollute semantic search.

Test inventory:
  1. test_merge_lifecycle_dict_preserves_omitted_key
     Model omits a key entirely -> existing value preserved.
  2. test_merge_lifecycle_dict_preserves_none_value
     Model returns None for a key -> existing value preserved.
  3. test_merge_lifecycle_dict_preserves_empty_string_value
     Model returns "" for a key -> existing value preserved.
  4. test_merge_lifecycle_dict_updates_contradicting_value
     Model returns a genuinely new non-empty value -> existing value replaced.
  5. test_merge_lifecycle_dict_preserves_ci_required_checks
     Existing ci.required_checks list is preserved when model omits/degrades it.
  6. test_merge_lifecycle_dict_preserves_branch_environment_map
     Existing branch_environment_map is preserved when model omits it.
  7. test_process_one_repo_preserves_lifecycle_sub_keys_on_refresh
     Integration: _process_one_repo with an existing .md preserves lifecycle
     sub-keys not present in the model's output.
  8. test_process_one_repo_updates_contradicting_lifecycle_sub_key
     Integration: _process_one_repo replaces a lifecycle sub-key when the model
     returns a contradicting non-empty value.
  9. test_addendum_contains_hallucination_removal_instruction
     The lifecycle_refresh_addendum.md must contain explicit instruction to
     silently remove fabricated content rather than write negations.
 10. test_merge_lifecycle_dict_new_key_from_model_is_added
     A brand-new sub-key returned by the model that does not exist in existing
     is added to the result (no key from the model silently dropped).

No mocks for merge logic or _process_one_repo (Messi Rule #1).
Only the invoker callable is a recording stub so no real Claude CLI is called.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, cast

import pytest
import yaml

from code_indexer.global_repos.lifecycle_batch_runner import (
    LifecycleBatchRunner,
    _merge_lifecycle_dict,
)
from code_indexer.global_repos.repo_analyzer import split_frontmatter_and_body
from code_indexer.global_repos.unified_response_parser import (
    CURRENT_LIFECYCLE_SCHEMA_VERSION,
    UnifiedResult,
)


# ---------------------------------------------------------------------------
# Addendum path — resolved at module level (mirrors invoker module convention)
# ---------------------------------------------------------------------------

_ADDENDUM_PATH: Path = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "code_indexer"
    / "server"
    / "prompts"
    / "lifecycle_refresh_addendum.md"
)


# ---------------------------------------------------------------------------
# Fixtures for lifecycle dicts representing real-world starlette scenario
# ---------------------------------------------------------------------------

# Existing (richer, correct) lifecycle values as found in cidx-meta before refresh
_EXISTING_LIFECYCLE: Dict[str, Any] = {
    "ci_system": "github-actions",
    "deployment_target": "pypi",
    "language_ecosystem": "python",
    "build_system": "uv/hatchling",
    "testing_framework": "pytest",
    "confidence": "high",
    "ci": {
        "required_checks": ["check", "zizmor"],
        "trigger_events": ["push", "pull_request"],
        "deploy_on": "tag",
    },
    "branching": {
        "model": "github-flow",
        "main_branch": "master",
        "release_branches": ["master"],
    },
    "branch_environment_map": {
        "master": "production",
    },
}

# Degraded lifecycle returned by model on refresh (simulates Defect 1)
_DEGRADED_MODEL_LIFECYCLE: Dict[str, Any] = {
    "ci_system": "github-actions",
    "deployment_target": "pypi",
    "language_ecosystem": "python",
    "build_system": "hatchling",  # degraded from "uv/hatchling"
    "testing_framework": "pytest",
    "confidence": "high",
    "ci": {
        "required_checks": ["check"],  # degraded: zizmor dropped
        "trigger_events": ["push", "pull_request"],
        "deploy_on": "tag",
    },
    # branching: omitted entirely by model
    # branch_environment_map: omitted entirely by model
}


# ---------------------------------------------------------------------------
# Stubs (mirrors pattern from test_lifecycle_batch_runner_940_frontmatter_merge)
# ---------------------------------------------------------------------------


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


def _make_runner(
    golden_repos_dir: Path,
    invoker_lifecycle: Optional[Dict[str, Any]] = None,
    invoker_description: str = "Refined description.",
) -> LifecycleBatchRunner:
    lifecycle = (
        invoker_lifecycle
        if invoker_lifecycle is not None
        else dict(_DEGRADED_MODEL_LIFECYCLE)
    )

    def _invoker(alias: str, repo_path: Path, **_kwargs: object) -> UnifiedResult:
        return UnifiedResult(
            description=invoker_description,
            lifecycle=dict(lifecycle),
        )

    return LifecycleBatchRunner(
        golden_repos_dir=golden_repos_dir,
        job_tracker=_StubJobTracker(),
        refresh_scheduler=_StubScheduler(),
        debouncer=_StubDebouncer(),
        claude_cli_invoker=_invoker,
        concurrency=1,
        sub_batch_size_override=10,
    )


def _write_md(path: Path, frontmatter: Dict[str, Any], body: str) -> None:
    fm_yaml = yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True)
    path.write_text(f"---\n{fm_yaml}---\n\n{body}\n", encoding="utf-8")


def _read_frontmatter(path: Path) -> Dict[str, Any]:
    content = path.read_text(encoding="utf-8")
    frontmatter, _ = split_frontmatter_and_body(content)
    return cast(Dict[str, Any], frontmatter)


@pytest.fixture()
def golden_repos_dir(tmp_path: Path) -> Path:
    (tmp_path / "cidx-meta").mkdir(parents=True)
    return tmp_path


# ---------------------------------------------------------------------------
# 1. _merge_lifecycle_dict: omitted key preserves existing value
# ---------------------------------------------------------------------------


def test_merge_lifecycle_dict_preserves_omitted_key() -> None:
    """When model omits a key entirely, the existing value is preserved."""
    existing = {"build_system": "uv/hatchling", "ci_system": "github-actions"}
    new = {"ci_system": "github-actions"}  # build_system omitted

    merged = _merge_lifecycle_dict(existing, new)

    assert merged["build_system"] == "uv/hatchling", (
        "build_system must be preserved when model omits it"
    )
    assert merged["ci_system"] == "github-actions"


# ---------------------------------------------------------------------------
# 2. _merge_lifecycle_dict: None value preserves existing
# ---------------------------------------------------------------------------


def test_merge_lifecycle_dict_preserves_none_value() -> None:
    """When model returns None for a key, the existing value is preserved."""
    existing = {"build_system": "uv/hatchling", "ci_system": "github-actions"}
    new = {"ci_system": "github-actions", "build_system": None}

    merged = _merge_lifecycle_dict(existing, new)

    assert merged["build_system"] == "uv/hatchling", (
        "build_system must be preserved when model returns None"
    )


# ---------------------------------------------------------------------------
# 3. _merge_lifecycle_dict: empty string preserves existing
# ---------------------------------------------------------------------------


def test_merge_lifecycle_dict_preserves_empty_string_value() -> None:
    """When model returns empty string for a key, the existing value is preserved."""
    existing = {"build_system": "uv/hatchling", "ci_system": "github-actions"}
    new = {"ci_system": "github-actions", "build_system": ""}

    merged = _merge_lifecycle_dict(existing, new)

    assert merged["build_system"] == "uv/hatchling", (
        "build_system must be preserved when model returns empty string"
    )


# ---------------------------------------------------------------------------
# 4. _merge_lifecycle_dict: genuinely new contradicting value updates
# ---------------------------------------------------------------------------


def test_merge_lifecycle_dict_updates_contradicting_value() -> None:
    """When model returns a genuinely new non-empty value, existing is replaced."""
    existing = {"build_system": "setuptools", "ci_system": "jenkins"}
    new = {"ci_system": "github-actions", "build_system": "uv/hatchling"}

    merged = _merge_lifecycle_dict(existing, new)

    assert merged["build_system"] == "uv/hatchling", (
        "build_system must be updated when model returns non-empty contradicting value"
    )
    assert merged["ci_system"] == "github-actions"


# ---------------------------------------------------------------------------
# 5. _merge_lifecycle_dict: ci.required_checks list preserved on degradation
# ---------------------------------------------------------------------------


def test_merge_lifecycle_dict_preserves_ci_required_checks() -> None:
    """Existing ci.required_checks list is preserved when model returns fewer items."""
    existing = {
        "ci_system": "github-actions",
        "ci": {"required_checks": ["check", "zizmor"], "trigger_events": ["push"]},
    }
    new = {
        "ci_system": "github-actions",
        "ci": {"required_checks": ["check"], "trigger_events": ["push"]},
    }

    merged = _merge_lifecycle_dict(existing, new)

    ci = merged.get("ci", {})
    required_checks = ci.get("required_checks")
    assert required_checks == ["check", "zizmor"], (
        "required_checks must be preserved when model returns subset; "
        f"got: {required_checks}"
    )


# ---------------------------------------------------------------------------
# 6. _merge_lifecycle_dict: branch_environment_map preserved when model omits it
# ---------------------------------------------------------------------------


def test_merge_lifecycle_dict_preserves_branch_environment_map() -> None:
    """Existing branch_environment_map is preserved when model omits it entirely."""
    existing = {
        "ci_system": "github-actions",
        "branch_environment_map": {"master": "production"},
    }
    new = {
        "ci_system": "github-actions",
        # branch_environment_map absent
    }

    merged = _merge_lifecycle_dict(existing, new)

    assert "branch_environment_map" in merged, (
        "branch_environment_map key must be preserved when model omits it"
    )
    assert merged["branch_environment_map"] == {"master": "production"}, (
        "branch_environment_map value must be preserved unchanged"
    )


# ---------------------------------------------------------------------------
# 7. Integration: _process_one_repo preserves lifecycle sub-keys on refresh
# ---------------------------------------------------------------------------


def test_process_one_repo_preserves_lifecycle_sub_keys_on_refresh(
    golden_repos_dir: Path,
) -> None:
    """
    When cidx-meta/<alias>.md has a richer lifecycle (ci.required_checks with 2
    entries, branch_environment_map, build_system=uv/hatchling) and the model
    returns a degraded lifecycle (omits branch_environment_map, drops zizmor from
    required_checks, degrades build_system to 'hatchling'), the written file must
    preserve the richer existing values.
    """
    alias = "starlette"
    (golden_repos_dir / alias).mkdir()
    meta_path = golden_repos_dir / "cidx-meta" / f"{alias}.md"

    existing_frontmatter: Dict[str, Any] = {
        "last_analyzed": "2026-05-01T00:00:00Z",
        "lifecycle": dict(_EXISTING_LIFECYCLE),
        "lifecycle_schema_version": CURRENT_LIFECYCLE_SCHEMA_VERSION,
    }
    _write_md(meta_path, existing_frontmatter, "Existing starlette description.")

    # Model returns degraded lifecycle (simulates Defect 1)
    runner = _make_runner(
        golden_repos_dir, invoker_lifecycle=dict(_DEGRADED_MODEL_LIFECYCLE)
    )
    runner._process_one_repo(alias, "job-1101")

    written_fm = _read_frontmatter(meta_path)
    written_lifecycle = written_fm.get("lifecycle", {})

    # build_system must preserve the richer "uv/hatchling" over degraded "hatchling"
    assert written_lifecycle.get("build_system") == "uv/hatchling", (
        "build_system must be preserved as 'uv/hatchling' (richer), "
        f"got: {written_lifecycle.get('build_system')!r}"
    )

    # ci.required_checks must preserve ["check", "zizmor"]
    ci = written_lifecycle.get("ci", {})
    required_checks = ci.get("required_checks")
    assert required_checks == ["check", "zizmor"], (
        "ci.required_checks must preserve ['check', 'zizmor'], "
        f"got: {required_checks!r}"
    )

    # branch_environment_map must be preserved (model omitted it)
    assert "branch_environment_map" in written_lifecycle, (
        "branch_environment_map must be preserved when model omits it"
    )
    assert written_lifecycle["branch_environment_map"] == {"master": "production"}, (
        "branch_environment_map value must match existing"
    )


# ---------------------------------------------------------------------------
# 8. Integration: _process_one_repo replaces genuinely contradicting lifecycle sub-key
# ---------------------------------------------------------------------------


def test_process_one_repo_updates_contradicting_lifecycle_sub_key(
    golden_repos_dir: Path,
) -> None:
    """
    When the model returns a genuinely new non-empty lifecycle value that
    contradicts the existing one (e.g. ci_system changed from 'jenkins' to
    'github-actions'), the new value must win.
    """
    alias = "myrepo"
    (golden_repos_dir / alias).mkdir()
    meta_path = golden_repos_dir / "cidx-meta" / f"{alias}.md"

    existing_frontmatter: Dict[str, Any] = {
        "last_analyzed": "2026-01-01T00:00:00Z",
        "lifecycle": {
            "ci_system": "jenkins",
            "deployment_target": "bare-metal",
            "language_ecosystem": "python",
            "build_system": "setuptools",
            "testing_framework": "unittest",
            "confidence": "low",
        },
        "lifecycle_schema_version": CURRENT_LIFECYCLE_SCHEMA_VERSION,
    }
    _write_md(meta_path, existing_frontmatter, "Old description.")

    # Model returns github-actions / hatchling — genuinely new values
    new_lifecycle: Dict[str, Any] = {
        "ci_system": "github-actions",
        "deployment_target": "pypi",
        "language_ecosystem": "python",
        "build_system": "hatchling",
        "testing_framework": "pytest",
        "confidence": "high",
    }
    runner = _make_runner(golden_repos_dir, invoker_lifecycle=new_lifecycle)
    runner._process_one_repo(alias, "job-1101")

    written_fm = _read_frontmatter(meta_path)
    written_lifecycle = written_fm.get("lifecycle", {})

    assert written_lifecycle.get("ci_system") == "github-actions", (
        "ci_system must be updated from jenkins to github-actions"
    )
    assert written_lifecycle.get("build_system") == "hatchling", (
        "build_system must be updated from setuptools to hatchling"
    )
    assert written_lifecycle.get("confidence") == "high"


# ---------------------------------------------------------------------------
# 9. Prompt-content guard: addendum contains hallucination-removal instruction
# ---------------------------------------------------------------------------


def test_addendum_contains_hallucination_removal_instruction() -> None:
    """
    lifecycle_refresh_addendum.md must contain an explicit instruction to
    SILENTLY REMOVE fabricated content rather than write negations.

    This is a content guard (not a behavioural LLM test) — it asserts that
    the instruction text is present, protecting against prompt regression.
    The instruction must convey:
      - Remove fabricated/unverifiable claims silently
      - Do NOT write negations that name the false feature
    """
    assert _ADDENDUM_PATH.exists(), (
        f"lifecycle_refresh_addendum.md not found at {_ADDENDUM_PATH}"
    )
    addendum_text = _ADDENDUM_PATH.read_text(encoding="utf-8")

    # Must contain an explicit instruction about silent removal of fabrications
    # (case-insensitive — the exact wording may differ, but the concept must be present)
    addendum_lower = addendum_text.lower()

    assert "silently" in addendum_lower or "silent" in addendum_lower, (
        "Addendum must instruct to silently remove fabricated content "
        "(keyword 'silent' or 'silently' not found)"
    )

    # Must instruct against negations / naming the false feature
    has_negation_guidance = (
        "negation" in addendum_lower
        or "do not" in addendum_lower
        or "never" in addendum_lower
    )
    assert has_negation_guidance, (
        "Addendum must instruct against writing negations that name false features "
        "(keywords 'negation', 'do not', or 'never' not found)"
    )


# ---------------------------------------------------------------------------
# 10. _merge_lifecycle_dict: new key from model is added (no suppression)
# ---------------------------------------------------------------------------


def test_merge_lifecycle_dict_new_key_from_model_is_added() -> None:
    """
    A sub-key returned by the model that does not exist in the existing lifecycle
    must be added to the merged result (new information is never suppressed).
    """
    existing = {"ci_system": "github-actions", "build_system": "poetry"}
    new = {
        "ci_system": "github-actions",
        "build_system": "poetry",
        "new_key": "new_value",
    }

    merged = _merge_lifecycle_dict(existing, new)

    assert "new_key" in merged, (
        "A new key from the model must be added to the merged lifecycle"
    )
    assert merged["new_key"] == "new_value"
