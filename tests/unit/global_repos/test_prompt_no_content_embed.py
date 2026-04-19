"""
Bug #840: Prompt Content Duplication Fix

Tests asserting that prompt builders do NOT embed large document content inline.
Design principle: prompts contain task description, rules, and file path pointers.
Prompts must NOT contain embedded document bodies or file content dumps.

Sites covered:
- Site #1: build_delta_merge_prompt (existing_content not embedded)
- Site #4: build_refinement_prompt (existing_body not embedded)
- Site #2: _build_standard_prompt pass-2 previous analysis (not embedded)
- Site #3: _build_output_first_prompt pass-2 previous analysis (not embedded)
- Site #6: build_pass1_prompt repo descriptions conditional staging

Test inventory (8 tests):
1. test_build_delta_merge_prompt_does_not_embed_existing_content
2. test_build_delta_merge_prompt_references_file_workflow
3. test_build_refinement_prompt_does_not_embed_existing_body
4. test_pass2_standard_prompt_does_not_embed_previous_analysis
5. test_pass2_output_first_prompt_does_not_embed_previous_analysis
6. test_pass1_prompt_embeds_descriptions_below_threshold
7. test_pass1_prompt_uses_staging_file_above_threshold
8. test_pass1_prompt_size_bounded_for_large_repo_sets
"""

from pathlib import Path
from typing import Any, Dict, List

from code_indexer.global_repos.dependency_map_analyzer import (
    PASS1_INLINE_DESCRIPTION_THRESHOLD_BYTES,
    DependencyMapAnalyzer,
)

# ---------------------------------------------------------------------------
# Canary marker — must be distinctive and unlikely to appear naturally
# ---------------------------------------------------------------------------

_LARGE_UNIQUE_MARKER = "UNIQUE_CANARY_CONTENT_THAT_MUST_NOT_APPEAR_IN_PROMPT_XYZ123"

# ---------------------------------------------------------------------------
# Content-generation constants — internal to _make_large_content
# ---------------------------------------------------------------------------

# Width of each repeated padding line in generated content (chars before newline)
_CONTENT_LINE_WIDTH = 80

# Number of padding lines per repetition block
_CONTENT_LINES_PER_BLOCK = 10

# Offset added to a count to produce an end-exclusive upper bound for range()
_RANGE_UPPER_BOUND_OFFSET = 1

# ---------------------------------------------------------------------------
# Repo alias indexing conventions
# ---------------------------------------------------------------------------

# Starting index for 1-based repo alias generation (large-domain fixture: repo-1..N)
REPO_INDEX_START = 1

# Starting index for 0-based repo alias generation (pass1 large/huge fixtures)
REPO_ZERO_INDEX_START = 0

# ---------------------------------------------------------------------------
# Site #1: delta merge content sizes
# ---------------------------------------------------------------------------

DELTA_MERGE_LARGE_CONTENT_BYTES = 100_000

# ---------------------------------------------------------------------------
# Site #4: refinement content sizes
# ---------------------------------------------------------------------------

REFINEMENT_LARGE_BODY_BYTES = 80_000

# ---------------------------------------------------------------------------
# Sites #2 and #3: Pass 2 previous analysis file size
# ---------------------------------------------------------------------------

PASS2_PREVIOUS_ANALYSIS_BYTES = 50_000

# ---------------------------------------------------------------------------
# Standard two-repo fixture sizes (used by _make_repo_list)
# ---------------------------------------------------------------------------

FIXTURE_REPO1_TOTAL_BYTES = 500_000
FIXTURE_REPO1_FILE_COUNT = 30
FIXTURE_REPO2_TOTAL_BYTES = 300_000
FIXTURE_REPO2_FILE_COUNT = 20

# ---------------------------------------------------------------------------
# Large-domain fixture (Sites #2/#3 output-first path): 4 repos, 1-based IDs
# ---------------------------------------------------------------------------

LARGE_DOMAIN_REPO_COUNT = 4
LARGE_DOMAIN_REPO_TOTAL_BYTES = 200_000
LARGE_DOMAIN_REPO_FILE_COUNT = 10

# ---------------------------------------------------------------------------
# Site #6: Pass 1 description staging thresholds
# ---------------------------------------------------------------------------

# Number of repos (0-based range) that pushes total description bytes above 8KB threshold
PASS1_LARGE_REPO_COUNT = 50

# Zero-padded width for alias IDs in the large-repo scenario (e.g., "repo-042")
PASS1_LARGE_REPO_ID_WIDTH = 3

# Per-repo description padding bytes for the large-repo scenario
PASS1_LARGE_DESC_PADDING_BYTES = 170

# Fixture sizes per repo for the large-repo scenario
PASS1_LARGE_FIXTURE_TOTAL_BYTES = 100_000
PASS1_LARGE_FIXTURE_FILE_COUNT = 5

# Number of repos (0-based range) for the worst-case prompt-size bound test
PASS1_HUGE_REPO_COUNT = 500

# Zero-padded width for alias IDs in the huge-repo scenario (e.g., "repo-0042")
PASS1_HUGE_REPO_ID_WIDTH = 4

# Per-repo description padding bytes for the huge-repo scenario
PASS1_HUGE_DESC_PADDING_BYTES = 290

# Fixture sizes per repo for the huge-repo scenario
PASS1_HUGE_FIXTURE_TOTAL_BYTES = 50_000
PASS1_HUGE_FIXTURE_FILE_COUNT = 3

# Maximum allowed prompt size (bytes) for the huge-repo input
MAX_LARGE_REPO_PROMPT_BYTES = 20_000

# Fixture sizes per repo in the below-threshold (small) scenario
PASS1_SMALL_FIXTURE_TOTAL_BYTES = 100_000
PASS1_SMALL_FIXTURE_FILE_COUNT = 5


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_analyzer(tmp_path: Path) -> DependencyMapAnalyzer:
    golden_repos_root = tmp_path / "golden-repos"
    golden_repos_root.mkdir()
    cidx_meta_path = tmp_path / "cidx-meta"
    cidx_meta_path.mkdir()
    return DependencyMapAnalyzer(
        golden_repos_root=golden_repos_root,
        cidx_meta_path=cidx_meta_path,
        pass_timeout=600,
    )


def _make_large_content(size_bytes: int) -> str:
    """Return a string of roughly `size_bytes` that contains the canary marker."""
    padding_line = "A" * _CONTENT_LINE_WIDTH + "\n"
    block = _LARGE_UNIQUE_MARKER + " " + padding_line * _CONTENT_LINES_PER_BLOCK
    repeats = (size_bytes // len(block)) + _RANGE_UPPER_BOUND_OFFSET
    return (block * repeats)[:size_bytes]


def _make_repo_alias(index: int, id_width: int) -> str:
    """Return a zero-padded repo alias string, e.g. 'repo-042'."""
    return f"repo-{index:0{id_width}d}"


def _write_previous_analysis(tmp_path: Path, domain_name: str, size_bytes: int) -> Path:
    """Write a realistic previous-analysis markdown file; return its parent dir."""
    prev_dir = tmp_path / "prev_analysis"
    prev_dir.mkdir(exist_ok=True)
    body = _make_large_content(size_bytes)
    (prev_dir / f"{domain_name}.md").write_text(
        f"# Domain Analysis: {domain_name}\n\n## Overview\n\n{body}"
    )
    return prev_dir


def _make_domain() -> Dict[str, Any]:
    return {
        "name": "test-domain",
        "description": "A domain for testing",
        "participating_repos": ["repo-1", "repo-2"],
        "evidence": "repo-1 imports repo-2",
    }


def _make_domain_list() -> List[Dict[str, Any]]:
    return [
        {
            "name": "test-domain",
            "description": "A domain for testing",
            "participating_repos": ["repo-1", "repo-2"],
        },
        {
            "name": "other-domain",
            "description": "Another domain",
            "participating_repos": ["other-repo"],
        },
    ]


def _make_repo_list(tmp_path: Path) -> List[Dict[str, Any]]:
    return [
        {
            "alias": "repo-1",
            "clone_path": str(tmp_path / "repo-1"),
            "total_bytes": FIXTURE_REPO1_TOTAL_BYTES,
            "file_count": FIXTURE_REPO1_FILE_COUNT,
        },
        {
            "alias": "repo-2",
            "clone_path": str(tmp_path / "repo-2"),
            "total_bytes": FIXTURE_REPO2_TOTAL_BYTES,
            "file_count": FIXTURE_REPO2_FILE_COUNT,
        },
    ]


def _make_large_domain_repo_list(tmp_path: Path) -> List[Dict[str, Any]]:
    """Build a repo list for the large-domain (output-first) fixture (1-based IDs)."""
    return [
        {
            "alias": f"repo-{i}",
            "clone_path": str(tmp_path / f"repo-{i}"),
            "total_bytes": LARGE_DOMAIN_REPO_TOTAL_BYTES,
            "file_count": LARGE_DOMAIN_REPO_FILE_COUNT,
        }
        for i in range(
            REPO_INDEX_START,
            LARGE_DOMAIN_REPO_COUNT + _RANGE_UPPER_BOUND_OFFSET,
        )
    ]


# ---------------------------------------------------------------------------
# Test 1: Site #1 — build_delta_merge_prompt does not embed existing_content
# ---------------------------------------------------------------------------


def test_build_delta_merge_prompt_does_not_embed_existing_content(
    tmp_path: Path,
) -> None:
    """Site #1: build_delta_merge_prompt must NOT embed existing_content verbatim."""
    analyzer = _make_analyzer(tmp_path)
    large_content = _make_large_content(DELTA_MERGE_LARGE_CONTENT_BYTES)

    prompt = analyzer.build_delta_merge_prompt(
        domain_name="test-domain",
        existing_content=large_content,
        changed_repos=[],
        new_repos=[],
        removed_repos=[],
        domain_list=["test-domain"],
    )

    assert _LARGE_UNIQUE_MARKER not in prompt, (
        "build_delta_merge_prompt must not embed existing_content inline. "
        "Instruct Claude to use the Read tool on the temp file instead."
    )


# ---------------------------------------------------------------------------
# Test 2: Site #1 — build_delta_merge_prompt instructs Read+Edit workflow
# ---------------------------------------------------------------------------


def test_build_delta_merge_prompt_references_file_workflow(
    tmp_path: Path,
) -> None:
    """Site #1: Prompt must instruct Claude to Read the file and Edit it in place."""
    analyzer = _make_analyzer(tmp_path)

    prompt = analyzer.build_delta_merge_prompt(
        domain_name="test-domain",
        existing_content="some existing content",
        changed_repos=[],
        new_repos=[],
        removed_repos=[],
        domain_list=["test-domain"],
    )

    prompt_lower = prompt.lower()
    assert "read" in prompt_lower, (
        "Prompt must mention the Read tool so Claude loads the temp file."
    )
    assert "edit" in prompt_lower, (
        "Prompt must mention the Edit tool so Claude applies surgical edits."
    )


# ---------------------------------------------------------------------------
# Test 3: Site #4 — build_refinement_prompt does not embed existing_body
# ---------------------------------------------------------------------------


def test_build_refinement_prompt_does_not_embed_existing_body(
    tmp_path: Path,
) -> None:
    """Site #4: build_refinement_prompt must NOT embed existing_body verbatim."""
    analyzer = _make_analyzer(tmp_path)
    large_body = _make_large_content(REFINEMENT_LARGE_BODY_BYTES)

    prompt = analyzer.build_refinement_prompt(
        domain_name="test-domain",
        existing_body=large_body,
        participating_repos=["repo-1", "repo-2"],
    )

    assert _LARGE_UNIQUE_MARKER not in prompt, (
        "build_refinement_prompt must not embed existing_body inline. "
        "Instruct Claude to use the Read tool on the temp file instead."
    )


# ---------------------------------------------------------------------------
# Test 4: Site #2 — _build_standard_prompt does not embed previous analysis
# ---------------------------------------------------------------------------


def test_pass2_standard_prompt_does_not_embed_previous_analysis(
    tmp_path: Path,
) -> None:
    """Site #2: _build_standard_prompt must NOT embed previous analysis content."""
    analyzer = _make_analyzer(tmp_path)
    domain_name = "test-domain"
    prev_dir = _write_previous_analysis(
        tmp_path, domain_name, PASS2_PREVIOUS_ANALYSIS_BYTES
    )

    prompt = analyzer._build_standard_prompt(
        domain=_make_domain(),
        domain_list=_make_domain_list(),
        repo_list=_make_repo_list(tmp_path),
        previous_domain_dir=prev_dir,
    )

    assert _LARGE_UNIQUE_MARKER not in prompt, (
        "_build_standard_prompt must not embed previous analysis content inline. "
        "Pass the file path and instruct Claude to Read it."
    )


# ---------------------------------------------------------------------------
# Test 5: Site #3 — _build_output_first_prompt does not embed previous analysis
# ---------------------------------------------------------------------------


def test_pass2_output_first_prompt_does_not_embed_previous_analysis(
    tmp_path: Path,
) -> None:
    """Site #3: _build_output_first_prompt must NOT embed previous analysis content."""
    analyzer = _make_analyzer(tmp_path)
    domain_name = "test-domain"
    prev_dir = _write_previous_analysis(
        tmp_path, domain_name, PASS2_PREVIOUS_ANALYSIS_BYTES
    )

    repo_aliases = [
        f"repo-{i}"
        for i in range(
            REPO_INDEX_START,
            LARGE_DOMAIN_REPO_COUNT + _RANGE_UPPER_BOUND_OFFSET,
        )
    ]
    large_domain = {
        "name": domain_name,
        "description": "A large domain for testing",
        "participating_repos": repo_aliases,
        "evidence": "all repos interconnected",
    }

    prompt = analyzer._build_output_first_prompt(
        domain=large_domain,
        domain_list=[large_domain],
        repo_list=_make_large_domain_repo_list(tmp_path),
        previous_domain_dir=prev_dir,
    )

    assert _LARGE_UNIQUE_MARKER not in prompt, (
        "_build_output_first_prompt must not embed previous analysis content inline. "
        "Pass the file path and instruct Claude to Read it."
    )


# ---------------------------------------------------------------------------
# Test 6: Site #6 — build_pass1_prompt embeds descriptions below threshold
# ---------------------------------------------------------------------------


def test_pass1_prompt_embeds_descriptions_below_threshold(
    tmp_path: Path,
) -> None:
    """Site #6: Small repo set (total <= threshold) must embed descriptions inline."""
    analyzer = _make_analyzer(tmp_path)
    repo_descriptions = {
        "repo-a": "Small service for auth.",
        "repo-b": "Small service for storage.",
        "repo-c": "Small service for gateway.",
    }
    repo_list = [
        {
            "alias": alias,
            "clone_path": str(tmp_path / alias),
            "total_bytes": PASS1_SMALL_FIXTURE_TOTAL_BYTES,
            "file_count": PASS1_SMALL_FIXTURE_FILE_COUNT,
        }
        for alias in repo_descriptions
    ]

    prompt = analyzer.build_pass1_prompt(
        repo_descriptions=repo_descriptions,
        repo_list=repo_list,
    )

    for alias, desc in repo_descriptions.items():
        assert desc in prompt, (
            f"Description for '{alias}' must be embedded inline when total "
            "description bytes are below PASS1_INLINE_DESCRIPTION_THRESHOLD_BYTES."
        )


# ---------------------------------------------------------------------------
# Test 7: Site #6 — build_pass1_prompt stages descriptions above threshold
# ---------------------------------------------------------------------------


def test_pass1_prompt_uses_staging_file_above_threshold(
    tmp_path: Path,
) -> None:
    """Site #6: Large repo set (total > threshold) must NOT embed descriptions inline.

    The prompt must reference a staging file path instead.
    """
    analyzer = _make_analyzer(tmp_path)
    repo_descriptions = {
        _make_repo_alias(i, PASS1_LARGE_REPO_ID_WIDTH): (
            f"REPO_{i:0{PASS1_LARGE_REPO_ID_WIDTH}d}_DESCRIPTION_MARKER "
            + "X" * PASS1_LARGE_DESC_PADDING_BYTES
        )
        for i in range(REPO_ZERO_INDEX_START, PASS1_LARGE_REPO_COUNT)
    }
    total_bytes = sum(len(v) for v in repo_descriptions.values())
    assert total_bytes > PASS1_INLINE_DESCRIPTION_THRESHOLD_BYTES, (
        f"Test setup error: total_bytes={total_bytes} must exceed "
        f"PASS1_INLINE_DESCRIPTION_THRESHOLD_BYTES={PASS1_INLINE_DESCRIPTION_THRESHOLD_BYTES}"
    )

    repo_list = [
        {
            "alias": alias,
            "clone_path": str(tmp_path / alias),
            "total_bytes": PASS1_LARGE_FIXTURE_TOTAL_BYTES,
            "file_count": PASS1_LARGE_FIXTURE_FILE_COUNT,
        }
        for alias in repo_descriptions
    ]

    prompt = analyzer.build_pass1_prompt(
        repo_descriptions=repo_descriptions,
        repo_list=repo_list,
    )

    # Derive sample key from the same alias helper and start index — no duplication
    sample_key = _make_repo_alias(REPO_ZERO_INDEX_START, PASS1_LARGE_REPO_ID_WIDTH)
    sample_desc = repo_descriptions[sample_key]
    assert sample_desc not in prompt, (
        "Repo description content must NOT be embedded inline when total bytes "
        "exceed PASS1_INLINE_DESCRIPTION_THRESHOLD_BYTES."
    )
    assert "Read" in prompt or ".json" in prompt or "staging" in prompt.lower(), (
        "Prompt must reference a staging file when descriptions exceed threshold."
    )


# ---------------------------------------------------------------------------
# Test 8: Site #6 — build_pass1_prompt size bounded for large repo sets
# ---------------------------------------------------------------------------


def test_pass1_prompt_size_bounded_for_large_repo_sets(
    tmp_path: Path,
) -> None:
    """Site #6: Prompt for 500 repos must stay under 20KB (descriptions staged out)."""
    analyzer = _make_analyzer(tmp_path)
    repo_descriptions = {
        _make_repo_alias(i, PASS1_HUGE_REPO_ID_WIDTH): (
            "Z" * PASS1_HUGE_DESC_PADDING_BYTES
            + " "
            + _make_repo_alias(i, PASS1_HUGE_REPO_ID_WIDTH)
        )
        for i in range(REPO_ZERO_INDEX_START, PASS1_HUGE_REPO_COUNT)
    }
    repo_list = [
        {
            "alias": alias,
            "clone_path": str(tmp_path / alias),
            "total_bytes": PASS1_HUGE_FIXTURE_TOTAL_BYTES,
            "file_count": PASS1_HUGE_FIXTURE_FILE_COUNT,
        }
        for alias in repo_descriptions
    ]

    prompt = analyzer.build_pass1_prompt(
        repo_descriptions=repo_descriptions,
        repo_list=repo_list,
    )

    prompt_size = len(prompt.encode("utf-8"))
    assert prompt_size < MAX_LARGE_REPO_PROMPT_BYTES, (
        f"Prompt size is {prompt_size} bytes for {PASS1_HUGE_REPO_COUNT} repos — "
        f"must be under {MAX_LARGE_REPO_PROMPT_BYTES} bytes. "
        "Descriptions must be staged to a file, not embedded inline."
    )
