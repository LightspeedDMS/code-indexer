"""
Tests for lifecycle prompt loader, byte-identity regression, and lifecycle_schema constant.

Covers AC4, AC7, and the LIFECYCLE_SCHEMA_VERSION constant.
"""

import sys
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).parent.parent.parent.parent / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

PROMPTS_DIR = (
    Path(__file__).parent.parent.parent.parent
    / "src"
    / "code_indexer"
    / "global_repos"
    / "prompts"
)

# ---------------------------------------------------------------------------
# Byte-identity snapshots captured from repo_analyzer.py (source of truth)
# ---------------------------------------------------------------------------

_ORIGINAL_CREATE_PROMPT = """Analyze this repository and generate a comprehensive semantic description.

**Repository Type Discovery:**
Examine the folder structure to determine the repository type:
- Git repository: Contains a .git directory
- Langfuse trace repository: Contains UUID-named folders (e.g., 550e8400-e29b-41d4-a716-446655440000) with JSON trace files matching pattern NNN_turn_HASH.json

**For Git Repositories:**
Examine README, source files, and package files to extract:
- summary: 2-3 sentence description of what this repository does
- technologies: List of all technologies and tools detected
- features: Key features
- use_cases: Primary use cases
- purpose: One of: api, service, library, cli-tool, web-application, data-structure, utility, framework, general-purpose

**For Langfuse Trace Repositories:**
Extract intelligence from trace files (JSON files in UUID folders):
- user_identity: Extract from trace.userId field
- projects_detected: Extract from metadata.project_name field
- activity_summary: Summarize from trace.input and metadata.intel_task_type fields
- features: Key features based on trace patterns
- use_cases: Primary use cases inferred from traces

**Output Format:**
Generate YAML frontmatter + markdown body with these exact fields:
---
name: repository-name
repo_type: git|langfuse
technologies:
  - Technology 1
  - Technology 2
purpose: inferred-purpose
last_analyzed: (current timestamp)
user_identity: (Langfuse only - extracted user IDs)
projects_detected: (Langfuse only - list of project names)
---

# Repository Name

(Summary description)

## Key Features
- Feature 1
- Feature 2

## Technologies
- Tech 1
- Tech 2

## Primary Use Cases
- Use case 1
- Use case 2

## Activity Summary (Langfuse only)
(Summary of user activities based on traces)

**IMPORTANT:**
- Set repo_type field in YAML frontmatter to "git" or "langfuse"
- For Langfuse repos, include user_identity, projects_detected, and activity_summary sections
- Output ONLY the YAML + markdown (no explanations, no code blocks)
"""

# f-string body from _get_refresh_prompt() with placeholders intact
_ORIGINAL_REFRESH_PROMPT = """Update the repository description based on changes since last analysis.

**Last Analyzed:** {last_analyzed}

**Existing Description:**
{existing_description}

**Repository Type Discovery:**
Examine the folder structure to determine the repository type:
- Git repository: Contains a .git directory
- Langfuse trace repository: Contains UUID-named folders with JSON trace files

**For Git Repositories:**
1. Run: git log --since="{last_analyzed}" --oneline
2. If material changes detected (not just cosmetic commits), update the description
3. If no material changes, return the existing description unchanged

**For Langfuse Trace Repositories:**
1. Find files modified after {last_analyzed} using file modification timestamps
2. IMPORTANT: Langfuse traces are immutable once established
3. Focus on NEW trace files only (files with modification time > last_analyzed)
4. Extract new findings from new traces:
   - New user IDs from trace.userId
   - New projects from metadata.project_name
   - New activities from trace.input and metadata.intel_task_type
5. MERGE new findings with existing description (preserve existing user_identity and projects_detected)
6. DO NOT replace existing data - only ADD new discoveries

**Update Strategy:**
- Update description only if material changes detected
- Preserve existing YAML frontmatter structure
- For Langfuse: merge new findings, don't replace
- Update last_analyzed timestamp to current time

**Output Format:**
Return updated YAML frontmatter + markdown body with same structure as original.
Include repo_type field in YAML.
If no material changes: return existing description with updated last_analyzed timestamp only.

**IMPORTANT:**
- Output ONLY the YAML + markdown (no explanations, no code blocks)
- Preserve all existing fields in YAML frontmatter
- For Langfuse: keep existing user_identity and projects_detected, only add new entries
"""

# Inline string from _extract_info_with_claude() — ends at } with NO trailing newline
_ORIGINAL_EXTRACT_JSON_PROMPT = """Analyze this repository. Examine the README, source files, and package files.
Output ONLY a JSON object (no markdown, no explanation) with these exact fields:
{
  "summary": "2-3 sentence description of what this repository does",
  "technologies": ["list", "of", "all", "technologies", "and", "tools", "detected"],
  "features": ["key feature 1", "key feature 2", ...],
  "use_cases": ["primary use case 1", "use case 2", ...],
  "purpose": "one of: api, service, library, cli-tool, web-application, data-structure, utility, framework, general-purpose"
}"""

# ---------------------------------------------------------------------------
# Centralized parameter sets (single source of truth for test data)
# ---------------------------------------------------------------------------

# All four externalized prompt names
ALL_PROMPT_NAMES = [
    "repo_description_create",
    "repo_description_refresh",
    "repo_description_extract_json",
    "lifecycle_detection",
]

# (name, expected_content) tuples for byte-identity tests
BYTE_IDENTITY_CASES = [
    ("repo_description_create", _ORIGINAL_CREATE_PROMPT),
    ("repo_description_refresh", _ORIGINAL_REFRESH_PROMPT),
    ("repo_description_extract_json", _ORIGINAL_EXTRACT_JSON_PROMPT),
]

# (name, ends_with_newline) for trailing-newline policy tests
TRAILING_NEWLINE_CASES = [
    ("repo_description_create", True),
    ("repo_description_refresh", True),
    ("repo_description_extract_json", False),
]


def _read_prompt_file(name: str) -> str:
    """Read a prompt .md file from the prompts directory."""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


class TestPromptLoader:
    """Tests for the get_prompt() loader function."""

    @pytest.mark.parametrize("prompt_name", ALL_PROMPT_NAMES)
    def test_loader_returns_nonempty_string(self, prompt_name):
        """Loader returns a non-empty string for each known prompt."""
        from code_indexer.global_repos.prompts import get_prompt

        content = get_prompt(prompt_name)
        assert isinstance(content, str)
        assert len(content) > 0

    @pytest.mark.parametrize("prompt_name", ALL_PROMPT_NAMES)
    def test_loader_matches_direct_file_read(self, prompt_name):
        """get_prompt() returns content identical to reading the .md file directly."""
        from code_indexer.global_repos.prompts import get_prompt

        assert get_prompt(prompt_name) == _read_prompt_file(prompt_name)

    def test_loader_raises_for_unknown_prompt(self):
        """Loader raises FileNotFoundError for an unknown prompt name."""
        from code_indexer.global_repos.prompts import get_prompt

        with pytest.raises(FileNotFoundError):
            get_prompt("nonexistent_prompt_name_xyz")

    def test_loader_repeated_call_returns_same_content(self):
        """Calling get_prompt twice returns identical content (deterministic reads)."""
        from code_indexer.global_repos.prompts import get_prompt

        assert get_prompt("repo_description_create") == get_prompt(
            "repo_description_create"
        )

    def test_lifecycle_detection_does_not_start_with_yaml_frontmatter(self):
        """lifecycle_detection prompt is plain text — no frontmatter stripping by loader."""
        from code_indexer.global_repos.prompts import get_prompt

        assert not get_prompt("lifecycle_detection").startswith("---\n")


class TestByteIdentity:
    """Byte-identity regression tests for the three externalized prompt files."""

    @pytest.mark.parametrize("prompt_name,expected", BYTE_IDENTITY_CASES)
    def test_prompt_file_matches_original_byte_for_byte(self, prompt_name, expected):
        """Each .md file content is byte-identical to the original inlined string."""
        md_file = PROMPTS_DIR / f"{prompt_name}.md"
        assert md_file.exists(), f"Prompt file not found: {md_file}"

        file_content = md_file.read_text(encoding="utf-8")
        assert file_content == expected, (
            f"Byte mismatch for {prompt_name}! "
            f"len(file)={len(file_content)}, len(expected)={len(expected)}"
        )

    @pytest.mark.parametrize("prompt_name,ends_with_newline", TRAILING_NEWLINE_CASES)
    def test_prompt_trailing_newline_policy(self, prompt_name, ends_with_newline):
        """Each prompt file ends exactly as specified by the trailing-newline rule."""
        file_content = _read_prompt_file(prompt_name)
        if ends_with_newline:
            assert file_content.endswith("\n"), f"{prompt_name} must end with '\\n'"
            assert not file_content.endswith("\n\n"), (
                f"{prompt_name} must not end with double newline"
            )
        else:
            assert not file_content.endswith("\n"), (
                f"{prompt_name} must NOT end with trailing newline"
            )

    def test_refresh_prompt_retains_literal_placeholders(self):
        """repo_description_refresh.md retains {last_analyzed} and {existing_description}."""
        file_content = _read_prompt_file("repo_description_refresh")
        assert "{last_analyzed}" in file_content
        assert "{existing_description}" in file_content


class TestLifecycleSchema:
    """Tests for the LIFECYCLE_SCHEMA_VERSION constant in lifecycle_schema.py."""

    def test_schema_version_is_integer_one(self):
        """LIFECYCLE_SCHEMA_VERSION is the integer 1."""
        from code_indexer.global_repos.lifecycle_schema import LIFECYCLE_SCHEMA_VERSION

        assert isinstance(LIFECYCLE_SCHEMA_VERSION, int)
        assert LIFECYCLE_SCHEMA_VERSION == 1
