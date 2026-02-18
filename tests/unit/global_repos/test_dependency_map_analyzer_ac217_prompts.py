"""
Unit tests for Story #217 (AC1-AC5, AC7): Structured Cross-Domain prompt schema.

Tests cover:
- AC1: Pass 2 output-first prompt includes structured Cross-Domain table schema
- AC2: Pass 2 standard prompt includes structured Cross-Domain table schema
- AC3: Delta merge prompt includes structured Cross-Domain table schema
- AC4: New domain prompt includes structured Cross-Domain table schema
- AC5: All 4 prompts require sentinel text "No verified cross-domain dependencies."
- AC7: NEGATION_INDICATORS constant removed entirely from module

TDD RED PHASE: Tests written before production code changes exist.
"""

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

STRUCTURED_TABLE_HEADER = "| This Repo | Depends On | Target Domain | Type | Why | Evidence |"
SENTINEL_TEXT = "No verified cross-domain dependencies."

DEPENDENCY_TYPES_ENUM = [
    "Code-level",
    "Data contracts",
    "Service integration",
    "External tool",
    "Configuration coupling",
    "Deployment dependency",
]


def _make_analyzer(tmp_path):
    """Build a minimal DependencyMapAnalyzer for prompt testing."""
    from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer
    return DependencyMapAnalyzer(
        golden_repos_root=tmp_path,
        cidx_meta_path=tmp_path / "cidx-meta",
        pass_timeout=60,
    )


# ─────────────────────────────────────────────────────────────────────────────
# AC7: NEGATION_INDICATORS constant must not exist in the module
# ─────────────────────────────────────────────────────────────────────────────


class TestNegationIndicatorsRemoved:
    """AC7: NEGATION_INDICATORS constant is removed from the module."""

    def test_negation_indicators_not_in_module(self):
        """AC7: NEGATION_INDICATORS must not be importable from dependency_map_analyzer."""
        import code_indexer.global_repos.dependency_map_analyzer as mod
        assert not hasattr(mod, "NEGATION_INDICATORS"), (
            "NEGATION_INDICATORS constant must be removed from dependency_map_analyzer module"
        )


# ─────────────────────────────────────────────────────────────────────────────
# AC1: Pass 2 output-first prompt includes structured Cross-Domain table schema
# ─────────────────────────────────────────────────────────────────────────────


def _build_output_first_prompt(tmp_path):
    """Build output-first prompt using domain with >3 repos."""
    analyzer = _make_analyzer(tmp_path)
    domain = {
        "name": "test-domain",
        "description": "A test domain",
        "participating_repos": ["repo-1", "repo-2", "repo-3", "repo-4"],
        "evidence": "Some evidence",
    }
    domain_list = [
        {
            "name": "test-domain",
            "description": "A test domain",
            "participating_repos": ["repo-1", "repo-2", "repo-3", "repo-4"],
        },
        {
            "name": "other-domain",
            "description": "Another domain",
            "participating_repos": ["other-repo"],
        },
    ]
    repo_list = [
        {
            "alias": "repo-1",
            "clone_path": str(tmp_path / "repo-1"),
            "total_bytes": 1000000,
            "file_count": 50,
        },
        {
            "alias": "repo-2",
            "clone_path": str(tmp_path / "repo-2"),
            "total_bytes": 500000,
            "file_count": 30,
        },
        {
            "alias": "repo-3",
            "clone_path": str(tmp_path / "repo-3"),
            "total_bytes": 300000,
            "file_count": 20,
        },
        {
            "alias": "repo-4",
            "clone_path": str(tmp_path / "repo-4"),
            "total_bytes": 200000,
            "file_count": 10,
        },
    ]
    return analyzer._build_output_first_prompt(domain, domain_list, repo_list)


class TestPass2OutputFirstPromptSchema:
    """AC1: Output-first prompt includes structured table schema."""

    def test_has_outgoing_dependencies_section(self, tmp_path):
        """AC1: Output-first prompt has '### Outgoing Dependencies' subsection."""
        prompt = _build_output_first_prompt(tmp_path)
        assert "### Outgoing Dependencies" in prompt

    def test_has_incoming_dependencies_section(self, tmp_path):
        """AC1: Output-first prompt has '### Incoming Dependencies' subsection."""
        prompt = _build_output_first_prompt(tmp_path)
        assert "### Incoming Dependencies" in prompt

    def test_has_structured_table_header(self, tmp_path):
        """AC1: Output-first prompt contains structured table with all required columns."""
        prompt = _build_output_first_prompt(tmp_path)
        assert STRUCTURED_TABLE_HEADER in prompt

    def test_has_sentinel_text(self, tmp_path):
        """AC5: Output-first prompt requires sentinel for no-dependency case."""
        prompt = _build_output_first_prompt(tmp_path)
        assert SENTINEL_TEXT in prompt

    def test_has_dependency_types_enum(self, tmp_path):
        """AC1: Output-first prompt lists constrained dependency type values."""
        prompt = _build_output_first_prompt(tmp_path)
        for dep_type in DEPENDENCY_TYPES_ENUM:
            assert dep_type in prompt, f"Missing dependency type: {dep_type}"


# ─────────────────────────────────────────────────────────────────────────────
# AC2: Pass 2 standard prompt includes structured Cross-Domain table schema
# ─────────────────────────────────────────────────────────────────────────────


def _build_standard_prompt(tmp_path):
    """Build standard prompt via the new _build_standard_prompt helper."""
    analyzer = _make_analyzer(tmp_path)
    domain_list = [
        {
            "name": "test-domain",
            "description": "A test domain",
            "participating_repos": ["repo-1", "repo-2"],
        },
        {
            "name": "other-domain",
            "description": "Another domain",
            "participating_repos": ["other-repo"],
        },
    ]
    repo_list = [
        {
            "alias": "repo-1",
            "clone_path": str(tmp_path / "repo-1"),
            "total_bytes": 500000,
            "file_count": 30,
        },
        {
            "alias": "repo-2",
            "clone_path": str(tmp_path / "repo-2"),
            "total_bytes": 300000,
            "file_count": 20,
        },
    ]
    return analyzer._build_standard_prompt(
        domain={
            "name": "test-domain",
            "description": "A test domain",
            "participating_repos": ["repo-1", "repo-2"],
            "evidence": "",
        },
        domain_list=domain_list,
        repo_list=repo_list,
        previous_domain_dir=None,
    )


class TestPass2StandardPromptSchema:
    """AC2: Standard prompt (<=3 repos) includes structured table schema."""

    def test_has_outgoing_dependencies_section(self, tmp_path):
        """AC2: Standard prompt has '### Outgoing Dependencies' subsection."""
        prompt = _build_standard_prompt(tmp_path)
        assert "### Outgoing Dependencies" in prompt

    def test_has_incoming_dependencies_section(self, tmp_path):
        """AC2: Standard prompt has '### Incoming Dependencies' subsection."""
        prompt = _build_standard_prompt(tmp_path)
        assert "### Incoming Dependencies" in prompt

    def test_has_structured_table_header(self, tmp_path):
        """AC2: Standard prompt contains structured table with all required columns."""
        prompt = _build_standard_prompt(tmp_path)
        assert STRUCTURED_TABLE_HEADER in prompt

    def test_has_sentinel_text(self, tmp_path):
        """AC5: Standard prompt requires sentinel for no-dependency case."""
        prompt = _build_standard_prompt(tmp_path)
        assert SENTINEL_TEXT in prompt

    def test_has_dependency_types_enum(self, tmp_path):
        """AC2: Standard prompt lists constrained dependency type values."""
        prompt = _build_standard_prompt(tmp_path)
        for dep_type in DEPENDENCY_TYPES_ENUM:
            assert dep_type in prompt, f"Missing dependency type: {dep_type}"


# ─────────────────────────────────────────────────────────────────────────────
# AC3: Delta merge prompt includes structured Cross-Domain table schema
# ─────────────────────────────────────────────────────────────────────────────


def _build_delta_merge_prompt(tmp_path):
    """Build delta merge prompt."""
    analyzer = _make_analyzer(tmp_path)
    return analyzer.build_delta_merge_prompt(
        domain_name="test-domain",
        existing_content="# Domain Analysis: test-domain\n\n## Overview\nSome content.",
        changed_repos=[{"alias": "repo-1", "clone_path": "/fake/repo-1"}],
        new_repos=[],
        removed_repos=[],
        domain_list=["test-domain", "other-domain"],
    )


class TestDeltaMergePromptSchema:
    """AC3: build_delta_merge_prompt includes structured table schema."""

    def test_has_outgoing_dependencies_section(self, tmp_path):
        """AC3: Delta merge prompt has '### Outgoing Dependencies' subsection."""
        prompt = _build_delta_merge_prompt(tmp_path)
        assert "### Outgoing Dependencies" in prompt

    def test_has_incoming_dependencies_section(self, tmp_path):
        """AC3: Delta merge prompt has '### Incoming Dependencies' subsection."""
        prompt = _build_delta_merge_prompt(tmp_path)
        assert "### Incoming Dependencies" in prompt

    def test_has_structured_table_header(self, tmp_path):
        """AC3: Delta merge prompt contains structured table schema."""
        prompt = _build_delta_merge_prompt(tmp_path)
        assert STRUCTURED_TABLE_HEADER in prompt

    def test_has_sentinel_text(self, tmp_path):
        """AC5: Delta merge prompt requires sentinel text."""
        prompt = _build_delta_merge_prompt(tmp_path)
        assert SENTINEL_TEXT in prompt

    def test_self_correction_references_table_format(self, tmp_path):
        """AC3: Delta merge self-correction rules reference table format."""
        prompt = _build_delta_merge_prompt(tmp_path)
        assert "table" in prompt.lower(), (
            "Delta merge self-correction rules must reference table format"
        )


# ─────────────────────────────────────────────────────────────────────────────
# AC4: New domain prompt includes structured Cross-Domain table schema
# ─────────────────────────────────────────────────────────────────────────────


def _build_new_domain_prompt(tmp_path):
    """Build new domain prompt."""
    analyzer = _make_analyzer(tmp_path)
    return analyzer.build_new_domain_prompt(
        domain_name="new-domain",
        participating_repos=["repo-a", "repo-b"],
    )


class TestNewDomainPromptSchema:
    """AC4: build_new_domain_prompt includes structured table schema."""

    def test_has_outgoing_dependencies_section(self, tmp_path):
        """AC4: New domain prompt has '### Outgoing Dependencies' subsection."""
        prompt = _build_new_domain_prompt(tmp_path)
        assert "### Outgoing Dependencies" in prompt

    def test_has_incoming_dependencies_section(self, tmp_path):
        """AC4: New domain prompt has '### Incoming Dependencies' subsection."""
        prompt = _build_new_domain_prompt(tmp_path)
        assert "### Incoming Dependencies" in prompt

    def test_has_structured_table_header(self, tmp_path):
        """AC4: New domain prompt contains structured table schema."""
        prompt = _build_new_domain_prompt(tmp_path)
        assert STRUCTURED_TABLE_HEADER in prompt

    def test_has_sentinel_text(self, tmp_path):
        """AC5: New domain prompt requires sentinel text."""
        prompt = _build_new_domain_prompt(tmp_path)
        assert SENTINEL_TEXT in prompt
