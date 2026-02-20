"""
Unit tests for DependencyMapAnalyzer delta prompt methods (Story #193).

Tests delta-specific prompt construction:
- Delta merge prompts with self-correction mandate (5 critical rules)
- Domain discovery prompts for new repos
- New domain creation prompts
- Public invoke_delta_merge() method for encapsulation
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer


@pytest.fixture
def analyzer(tmp_path):
    """Create DependencyMapAnalyzer instance."""
    golden_repos_root = tmp_path / "golden-repos"
    golden_repos_root.mkdir()
    cidx_meta_path = tmp_path / "cidx-meta"
    cidx_meta_path.mkdir()

    return DependencyMapAnalyzer(
        golden_repos_root=golden_repos_root,
        cidx_meta_path=cidx_meta_path,
        pass_timeout=600,
    )


class TestDeltaMergePrompt:
    """Test delta merge prompt construction with self-correction rules."""

    def test_build_delta_merge_prompt_includes_existing_content(self, analyzer):
        """Test that merge prompt includes existing domain content."""
        existing_content = """---
domain: authentication
---

# Authentication Domain

Existing analysis content.
"""
        changed_repos = ["repo1"]
        new_repos = []
        removed_repos = []
        domain_list = ["authentication", "data-processing"]

        prompt = analyzer.build_delta_merge_prompt(
            domain_name="authentication",
            existing_content=existing_content,
            changed_repos=changed_repos,
            new_repos=new_repos,
            removed_repos=removed_repos,
            domain_list=domain_list,
        )

        # Should include existing content
        assert "Existing analysis content" in prompt
        assert existing_content in prompt

    def test_build_delta_merge_prompt_includes_self_correction_rules(self, analyzer):
        """Test that merge prompt includes 5 critical self-correction rules."""
        existing_content = "# Domain\n\nContent"
        changed_repos = ["repo1"]
        new_repos = []
        removed_repos = []
        domain_list = ["authentication"]

        prompt = analyzer.build_delta_merge_prompt(
            domain_name="authentication",
            existing_content=existing_content,
            changed_repos=changed_repos,
            new_repos=new_repos,
            removed_repos=removed_repos,
            domain_list=domain_list,
        )

        # Should include all 5 self-correction rules
        prompt_lower = prompt.lower()
        assert "critical self-correction rules" in prompt_lower or "self-correction" in prompt_lower
        assert "re-verify all dependencies" in prompt_lower
        assert "remove dependencies" in prompt_lower
        assert "correct dependencies" in prompt_lower
        assert "add new dependencies" in prompt_lower
        assert "preserve existing analysis" in prompt_lower or "unchanged repos" in prompt_lower

    def test_build_delta_merge_prompt_lists_changed_repos(self, analyzer):
        """Test that merge prompt explicitly lists changed repos."""
        existing_content = "# Domain"
        changed_repos = ["repo1", "repo2"]
        new_repos = []
        removed_repos = []
        domain_list = ["authentication"]

        prompt = analyzer.build_delta_merge_prompt(
            domain_name="authentication",
            existing_content=existing_content,
            changed_repos=changed_repos,
            new_repos=new_repos,
            removed_repos=removed_repos,
            domain_list=domain_list,
        )

        # Should list changed repos
        assert "repo1" in prompt
        assert "repo2" in prompt
        assert "changed" in prompt.lower()

    def test_build_delta_merge_prompt_lists_new_repos(self, analyzer):
        """Test that merge prompt explicitly lists new repos to incorporate."""
        existing_content = "# Domain"
        changed_repos = []
        new_repos = ["repo3", "repo4"]
        removed_repos = []
        domain_list = ["authentication"]

        prompt = analyzer.build_delta_merge_prompt(
            domain_name="authentication",
            existing_content=existing_content,
            changed_repos=changed_repos,
            new_repos=new_repos,
            removed_repos=removed_repos,
            domain_list=domain_list,
        )

        # Should list new repos
        assert "repo3" in prompt
        assert "repo4" in prompt
        assert "new" in prompt.lower()

    def test_build_delta_merge_prompt_lists_removed_repos(self, analyzer):
        """Test that merge prompt instructs removal of references to removed repos."""
        existing_content = "# Domain"
        changed_repos = []
        new_repos = []
        removed_repos = ["repo5"]
        domain_list = ["authentication"]

        prompt = analyzer.build_delta_merge_prompt(
            domain_name="authentication",
            existing_content=existing_content,
            changed_repos=changed_repos,
            new_repos=new_repos,
            removed_repos=removed_repos,
            domain_list=domain_list,
        )

        # Should instruct removal
        assert "repo5" in prompt
        assert "removed" in prompt.lower() or "remove" in prompt.lower()

    def test_build_delta_merge_prompt_includes_granularity_guidance(self, analyzer):
        """Test that merge prompt includes MODULE/SUBSYSTEM granularity guidance."""
        existing_content = "# Domain"
        changed_repos = ["repo1"]
        new_repos = []
        removed_repos = []
        domain_list = ["authentication"]

        prompt = analyzer.build_delta_merge_prompt(
            domain_name="authentication",
            existing_content=existing_content,
            changed_repos=changed_repos,
            new_repos=new_repos,
            removed_repos=removed_repos,
            domain_list=domain_list,
        )

        # Should include granularity examples
        assert "MODULE" in prompt or "SUBSYSTEM" in prompt or "granularity" in prompt.lower()
        assert "CORRECT" in prompt and "INCORRECT" in prompt


class TestDomainDiscoveryPrompt:
    """Test domain discovery prompt for new repos."""

    def test_build_domain_discovery_prompt_lists_new_repos(self, analyzer):
        """Test that domain discovery prompt lists new repos."""
        new_repos = [
            {"alias": "repo4", "description_summary": "New repo description"},
            {"alias": "repo5", "description_summary": "Another new repo"},
        ]
        existing_domains = ["authentication", "data-processing"]

        prompt = analyzer.build_domain_discovery_prompt(
            new_repos=new_repos,
            existing_domains=existing_domains,
        )

        # Should list new repos
        assert "repo4" in prompt
        assert "repo5" in prompt

    def test_build_domain_discovery_prompt_lists_existing_domains(self, analyzer):
        """Test that domain discovery prompt provides existing domain context."""
        new_repos = [{"alias": "repo4", "description_summary": "Description"}]
        existing_domains = ["authentication", "data-processing", "frontend"]

        prompt = analyzer.build_domain_discovery_prompt(
            new_repos=new_repos,
            existing_domains=existing_domains,
        )

        # Should list existing domains for context
        assert "authentication" in prompt
        assert "data-processing" in prompt
        assert "frontend" in prompt


class TestNewDomainPrompt:
    """Test new domain creation prompt."""

    def test_build_new_domain_prompt_specifies_domain_name(self, analyzer):
        """Test that new domain prompt specifies the domain name."""
        domain_name = "messaging"
        participating_repos = ["kafka-service", "notification-service"]

        prompt = analyzer.build_new_domain_prompt(
            domain_name=domain_name,
            participating_repos=participating_repos,
        )

        # Should specify domain name
        assert "messaging" in prompt

    def test_build_new_domain_prompt_lists_participating_repos(self, analyzer):
        """Test that new domain prompt lists participating repos."""
        domain_name = "messaging"
        participating_repos = ["kafka-service", "notification-service"]

        prompt = analyzer.build_new_domain_prompt(
            domain_name=domain_name,
            participating_repos=participating_repos,
        )

        # Should list participating repos
        assert "kafka-service" in prompt
        assert "notification-service" in prompt


class TestDeltaMergePromptOutputFormat:
    """
    AC2: Delta merge prompt explicitly requests complete document output (Story #234).

    The prompt must instruct Claude to return the COMPLETE updated domain analysis
    document, not just a change summary. This prevents the data loss bug where
    Claude returns only a brief change summary instead of the full document.
    """

    def test_prompt_instructs_return_complete_document(self, analyzer):
        """
        AC2: Prompt must contain explicit instruction to return the COMPLETE updated
        domain analysis document.
        """
        prompt = analyzer.build_delta_merge_prompt(
            domain_name="authentication",
            existing_content="# Domain\n\nContent",
            changed_repos=["repo1"],
            new_repos=[],
            removed_repos=[],
            domain_list=["authentication"],
        )

        prompt_lower = prompt.lower()
        assert "complete" in prompt_lower and "document" in prompt_lower, (
            "Prompt must instruct Claude to return the COMPLETE updated document. "
            f"Got prompt excerpt: ...{prompt[max(0, prompt.lower().find('output')-50):prompt.lower().find('output')+200]}..."
        )

    def test_prompt_specifies_required_output_sections(self, analyzer):
        """
        AC2: Prompt must specify the required output sections:
        Overview, Repository Roles, Intra-Domain Dependencies, Cross-Domain Connections.
        """
        prompt = analyzer.build_delta_merge_prompt(
            domain_name="authentication",
            existing_content="# Domain\n\nContent",
            changed_repos=["repo1"],
            new_repos=[],
            removed_repos=[],
            domain_list=["authentication"],
        )

        # All four required sections must be named in the prompt
        assert "Overview" in prompt, \
            "Prompt must specify 'Overview' as a required output section"
        assert "Repository Roles" in prompt, \
            "Prompt must specify 'Repository Roles' as a required output section"
        assert "Intra-Domain Dependencies" in prompt or "Intra-Domain" in prompt, \
            "Prompt must specify 'Intra-Domain Dependencies' as a required output section"
        assert "Cross-Domain Connections" in prompt, \
            "Prompt must specify 'Cross-Domain Connections' as a required output section"

    def test_prompt_prohibits_summary_only_response(self, analyzer):
        """
        AC2: Prompt must explicitly instruct Claude NOT to return only a summary of changes.
        """
        prompt = analyzer.build_delta_merge_prompt(
            domain_name="authentication",
            existing_content="# Domain\n\nContent",
            changed_repos=["repo1"],
            new_repos=[],
            removed_repos=[],
            domain_list=["authentication"],
        )

        prompt_lower = prompt.lower()
        # Must contain "do not return only a summary" or equivalent
        assert (
            ("do not return only a summary" in prompt_lower)
            or ("not return only a summary" in prompt_lower)
            or ("do not only return a summary" in prompt_lower)
        ), (
            "Prompt must explicitly say 'Do NOT return only a summary of changes'. "
            f"Prompt output section: {prompt[prompt.lower().find('output format'):prompt.lower().find('output format')+500]}"
        )

    def test_prompt_output_format_section_contains_all_ac2_requirements(self, analyzer):
        """
        AC2: Single comprehensive test verifying all three AC2 requirements together.
        Mirrors the exact Gherkin scenario from the story.
        """
        prompt = analyzer.build_delta_merge_prompt(
            domain_name="cidx-platform",
            existing_content="# Domain Analysis: cidx-platform\n\nExisting content.",
            changed_repos=["repo-a"],
            new_repos=[],
            removed_repos=[],
            domain_list=["cidx-platform", "auth-domain"],
        )

        prompt_lower = prompt.lower()

        # Requirement 1: "return the COMPLETE updated domain analysis document"
        assert "complete" in prompt_lower, \
            "AC2: Prompt must contain the word 'complete' regarding document output"

        # Requirement 2: Required output sections named explicitly
        required_sections = ["Overview", "Repository Roles", "Cross-Domain Connections"]
        for section in required_sections:
            assert section in prompt, \
                f"AC2: Prompt must specify '{section}' as a required output section"

        # Requirement 3: "Do NOT return only a summary of changes"
        assert "summary" in prompt_lower and "not" in prompt_lower, \
            "AC2: Prompt must explicitly prohibit returning only a summary of changes"


# =============================================================================
# Story #235: Fix Delta Analysis Falsely Claiming Dependency Removal for
# Unchanged Repos
# =============================================================================

# Realistic domain file content that contains "### repo-name" patterns
# in the Repository Roles section, as used in production domain files.
_CIDX_PLATFORM_DOMAIN_CONTENT = """\
# Domain Analysis: cidx-platform

## Overview

The cidx-platform domain provides the core indexing and search infrastructure.

## Repository Roles

### code-indexer

The primary CLI and local indexing engine. Implements VoyageAI-based semantic
search and FTS via Tantivy.

### cidx-server

REST/MCP server that exposes indexing capabilities over HTTP.

## Intra-Domain Dependencies

- code-indexer depends on cidx-server for remote index management

## Cross-Domain Connections

### Outgoing

| Domain | Repos Involved | Dependency Type | Evidence |
|--------|----------------|-----------------|----------|
| delphi-trie-structures | tries, tries-with-temporal | Code-level | code-indexer imports trie data structures |

### Incoming

None documented.
"""

_EXISTING_CONTENT_WITH_MULTIPLE_REPOS = """\
# Domain Analysis: auth-domain

## Repository Roles

### auth-service

Handles JWT issuance and validation.

### user-service

Manages user accounts and profiles.

### permission-service

Fine-grained permission evaluation.

## Cross-Domain Connections

None.
"""


class TestUnchangedReposExtraction:
    """
    AC2: Verify unchanged repo extraction from existing domain content.

    The method must parse "### repo-name" headings from the existing_content
    to compute the set of all repos in the domain. Unchanged = all repos minus
    changed, new, and removed.
    """

    def test_extracts_repo_aliases_from_existing_content(self, analyzer):
        """
        AC2: Repo aliases appearing as '### alias' in existing_content are
        identified and used to compute the unchanged set.
        """
        prompt = analyzer.build_delta_merge_prompt(
            domain_name="cidx-platform",
            existing_content=_CIDX_PLATFORM_DOMAIN_CONTENT,
            changed_repos=["code-indexer"],
            new_repos=[],
            removed_repos=[],
            domain_list=["cidx-platform", "delphi-trie-structures"],
        )

        # cidx-server was not in changed/new/removed — it must be listed as unchanged
        assert "cidx-server" in prompt, (
            "cidx-server is in existing_content but not in changed/new/removed, "
            "so it must appear in the Unchanged Repositories section"
        )

    def test_changed_repos_not_in_unchanged_section(self, analyzer):
        """
        AC2: Changed repos must NOT appear in the Unchanged Repositories section.
        The section should only contain repos that had no code changes.
        """
        prompt = analyzer.build_delta_merge_prompt(
            domain_name="cidx-platform",
            existing_content=_CIDX_PLATFORM_DOMAIN_CONTENT,
            changed_repos=["code-indexer"],
            new_repos=[],
            removed_repos=[],
            domain_list=["cidx-platform"],
        )

        # Find the Unchanged Repositories section
        if "## Unchanged Repositories" in prompt:
            section_start = prompt.index("## Unchanged Repositories")
            next_section = prompt.find("\n## ", section_start + 1)
            section_text = (
                prompt[section_start:next_section]
                if next_section != -1
                else prompt[section_start:]
            )
            # code-indexer is a changed repo — must NOT be in the unchanged section
            assert "- code-indexer" not in section_text, (
                "code-indexer is in changed_repos and must not appear in the "
                "Unchanged Repositories section"
            )

    def test_multiple_repos_extraction(self, analyzer):
        """
        AC2: All repos in existing content are correctly extracted; changed ones
        are excluded from the unchanged set.
        """
        prompt = analyzer.build_delta_merge_prompt(
            domain_name="auth-domain",
            existing_content=_EXISTING_CONTENT_WITH_MULTIPLE_REPOS,
            changed_repos=["auth-service"],
            new_repos=[],
            removed_repos=[],
            domain_list=["auth-domain"],
        )

        # user-service and permission-service are unchanged
        assert "user-service" in prompt, (
            "user-service is unchanged and must appear in prompt (Unchanged section)"
        )
        assert "permission-service" in prompt, (
            "permission-service is unchanged and must appear in prompt (Unchanged section)"
        )


class TestUnchangedRepositoriesSection:
    """
    AC1 + AC2: Verify the 'Unchanged Repositories' section is present in the
    delta merge prompt and contains the correct repos.
    """

    def test_unchanged_repositories_section_present(self, analyzer):
        """
        AC2: Prompt must include an 'Unchanged Repositories' section when
        there are repos in existing_content that are not in changed/new/removed.
        """
        prompt = analyzer.build_delta_merge_prompt(
            domain_name="cidx-platform",
            existing_content=_CIDX_PLATFORM_DOMAIN_CONTENT,
            changed_repos=["code-indexer"],
            new_repos=[],
            removed_repos=[],
            domain_list=["cidx-platform"],
        )

        assert "## Unchanged Repositories" in prompt, (
            "Prompt must include '## Unchanged Repositories' section. "
            "This section protects deps involving repos that had no code changes."
        )

    def test_unchanged_section_lists_correct_repos(self, analyzer):
        """
        AC2: The Unchanged Repositories section must list 'cidx-server' when
        code-indexer is the only changed repo and cidx-server is in existing_content.
        """
        prompt = analyzer.build_delta_merge_prompt(
            domain_name="cidx-platform",
            existing_content=_CIDX_PLATFORM_DOMAIN_CONTENT,
            changed_repos=["code-indexer"],
            new_repos=[],
            removed_repos=[],
            domain_list=["cidx-platform"],
        )

        assert "## Unchanged Repositories" in prompt
        section_start = prompt.index("## Unchanged Repositories")
        next_section = prompt.find("\n## ", section_start + 1)
        section_text = (
            prompt[section_start:next_section]
            if next_section != -1
            else prompt[section_start:]
        )

        assert "cidx-server" in section_text, (
            "cidx-server must be listed in the Unchanged Repositories section; "
            f"section content: {section_text!r}"
        )

    def test_unchanged_section_preservation_instruction(self, analyzer):
        """
        AC2: The Unchanged Repositories section must contain explicit instruction
        to preserve those repos' analysis exactly as-is.
        """
        prompt = analyzer.build_delta_merge_prompt(
            domain_name="cidx-platform",
            existing_content=_CIDX_PLATFORM_DOMAIN_CONTENT,
            changed_repos=["code-indexer"],
            new_repos=[],
            removed_repos=[],
            domain_list=["cidx-platform"],
        )

        assert "## Unchanged Repositories" in prompt
        section_start = prompt.index("## Unchanged Repositories")
        next_section = prompt.find("\n## ", section_start + 1)
        section_text = (
            prompt[section_start:next_section]
            if next_section != -1
            else prompt[section_start:]
        )

        section_lower = section_text.lower()
        assert "preserve" in section_lower or "must not" in section_lower or "do not" in section_lower, (
            "Unchanged Repositories section must contain instruction to preserve "
            f"or not modify those repos. Section: {section_text!r}"
        )

    def test_unchanged_section_never_remove_instruction(self, analyzer):
        """
        AC2: The prompt must explicitly state 'NEVER remove' dependencies
        involving unchanged repos.
        """
        prompt = analyzer.build_delta_merge_prompt(
            domain_name="cidx-platform",
            existing_content=_CIDX_PLATFORM_DOMAIN_CONTENT,
            changed_repos=["code-indexer"],
            new_repos=[],
            removed_repos=[],
            domain_list=["cidx-platform"],
        )

        prompt_lower = prompt.lower()
        assert "never remove" in prompt_lower or "never" in prompt_lower, (
            "Prompt must contain explicit 'NEVER remove' instruction for "
            "dependencies involving unchanged repos"
        )


class TestSelfCorrectionRulesScopedToChangedRepos:
    """
    AC2 + AC3: Verify self-correction rules explicitly scope removal to
    CHANGED repos only, and that changed repo dependencies can still be removed.
    """

    def test_removal_rule_scoped_to_changed_repos_only(self, analyzer):
        """
        AC2: The removal self-correction rule must explicitly say it applies to
        CHANGED repos ONLY — not all repos.
        """
        prompt = analyzer.build_delta_merge_prompt(
            domain_name="cidx-platform",
            existing_content=_CIDX_PLATFORM_DOMAIN_CONTENT,
            changed_repos=["code-indexer"],
            new_repos=[],
            removed_repos=[],
            domain_list=["cidx-platform"],
        )

        assert (
            "CHANGED repos ONLY" in prompt
            or "changed repos only" in prompt.lower()
        ), (
            "Self-correction removal rule must be scoped to 'CHANGED repos ONLY'. "
            "Currently it broadly says 'REMOVE dependencies no longer present' "
            "without scoping, which causes false removals for unchanged repos."
        )

    def test_rule_for_unchanged_repos_preservation(self, analyzer):
        """
        AC2: Must have an explicit rule that says unchanged repos' analysis
        must be preserved exactly as-is.
        """
        prompt = analyzer.build_delta_merge_prompt(
            domain_name="cidx-platform",
            existing_content=_CIDX_PLATFORM_DOMAIN_CONTENT,
            changed_repos=["code-indexer"],
            new_repos=[],
            removed_repos=[],
            domain_list=["cidx-platform"],
        )

        prompt_lower = prompt.lower()
        assert "unchanged" in prompt_lower and "preserve" in prompt_lower, (
            "Prompt must have a rule explicitly preserving UNCHANGED repos analysis."
        )

    def test_never_remove_rule_for_unchanged_repos(self, analyzer):
        """
        AC2: Must have an explicit rule 'NEVER remove a cross-domain dependency
        involving an UNCHANGED repo'.
        """
        prompt = analyzer.build_delta_merge_prompt(
            domain_name="cidx-platform",
            existing_content=_CIDX_PLATFORM_DOMAIN_CONTENT,
            changed_repos=["code-indexer"],
            new_repos=[],
            removed_repos=[],
            domain_list=["cidx-platform"],
        )

        prompt_lower = prompt.lower()
        assert "never" in prompt_lower and "unchanged" in prompt_lower, (
            "Prompt must have a NEVER remove rule for unchanged repo dependencies. "
            "This is the key fix for the false removal bug."
        )

    def test_conflicting_remove_if_cannot_confirm_line_absent_or_scoped(self, analyzer):
        """
        AC2: The old conflicting line 'If you cannot confirm a previously
        documented dependency from current source code, REMOVE it' must either
        be absent or be clearly scoped to CHANGED repos only.
        """
        prompt = analyzer.build_delta_merge_prompt(
            domain_name="cidx-platform",
            existing_content=_CIDX_PLATFORM_DOMAIN_CONTENT,
            changed_repos=["code-indexer"],
            new_repos=[],
            removed_repos=[],
            domain_list=["cidx-platform"],
        )

        old_unscoped_line = "If you cannot confirm a previously documented dependency from current source code, REMOVE it."
        if old_unscoped_line in prompt:
            idx = prompt.index(old_unscoped_line)
            surrounding = prompt[max(0, idx - 200):idx + len(old_unscoped_line) + 200]
            assert "CHANGED" in surrounding or "changed" in surrounding.lower(), (
                "The 'cannot confirm ... REMOVE it' line is still present without "
                "scoping to CHANGED repos. This causes false removal of unchanged "
                f"repo deps. Surrounding context: {surrounding!r}"
            )

    def test_ac3_changed_repo_deps_can_still_be_removed(self, analyzer):
        """
        AC3: The prompt must still instruct Claude to re-verify and remove
        stale dependencies for CHANGED repos. The fix must not over-protect.
        """
        prompt = analyzer.build_delta_merge_prompt(
            domain_name="cidx-platform",
            existing_content=_CIDX_PLATFORM_DOMAIN_CONTENT,
            changed_repos=["code-indexer"],
            new_repos=[],
            removed_repos=[],
            domain_list=["cidx-platform"],
        )

        prompt_lower = prompt.lower()
        assert "re-verify" in prompt_lower or "reverify" in prompt_lower, (
            "AC3: Prompt must still instruct re-verification of changed repo deps"
        )
        assert "remove" in prompt_lower and "changed" in prompt_lower, (
            "AC3: Prompt must still allow removal of stale deps for CHANGED repos"
        )


class TestRemovedReposCleanup:
    """
    AC4: Verify that removed repos are still instructed to be fully cleaned up.
    The fix for unchanged repos must not prevent cleanup of removed repos.
    """

    def test_removed_repos_still_cleaned_up(self, analyzer):
        """
        AC4: Repos in removed_repos list must still have ALL references removed
        from the domain file. The unchanged-repo protection must not apply to
        removed repos.
        """
        existing_content_with_old_repo = """\
# Domain Analysis: cidx-platform

## Repository Roles

### code-indexer

Primary indexing engine.

### old-deprecated-repo

This repo was decommissioned.

## Cross-Domain Connections

None.
"""
        prompt = analyzer.build_delta_merge_prompt(
            domain_name="cidx-platform",
            existing_content=existing_content_with_old_repo,
            changed_repos=[],
            new_repos=[],
            removed_repos=["old-deprecated-repo"],
            domain_list=["cidx-platform"],
        )

        prompt_lower = prompt.lower()
        assert "old-deprecated-repo" in prompt, (
            "AC4: Removed repo must still be mentioned in prompt for cleanup"
        )
        assert "remove" in prompt_lower or "removed" in prompt_lower, (
            "AC4: Prompt must instruct removal of references to removed repos"
        )

    def test_removed_repos_not_in_unchanged_section(self, analyzer):
        """
        AC4: Repos in removed_repos must NOT appear in the Unchanged Repositories
        section — they are being deleted, not preserved.
        """
        existing_content_with_old_repo = """\
# Domain Analysis: cidx-platform

## Repository Roles

### code-indexer

Primary indexing engine.

### old-deprecated-repo

This repo was decommissioned.
"""
        prompt = analyzer.build_delta_merge_prompt(
            domain_name="cidx-platform",
            existing_content=existing_content_with_old_repo,
            changed_repos=[],
            new_repos=[],
            removed_repos=["old-deprecated-repo"],
            domain_list=["cidx-platform"],
        )

        if "## Unchanged Repositories" in prompt:
            section_start = prompt.index("## Unchanged Repositories")
            next_section = prompt.find("\n## ", section_start + 1)
            section_text = (
                prompt[section_start:next_section]
                if next_section != -1
                else prompt[section_start:]
            )
            assert "old-deprecated-repo" not in section_text, (
                "AC4: Removed repo must NOT appear in Unchanged Repositories section"
            )


class TestEdgeCases:
    """
    Edge case tests for unchanged repo computation:
    - All repos changed (empty unchanged set)
    - No repos changed (all unchanged)
    - Single-repo domain all changed
    - No existing repos in content (empty extraction)
    - dict-format changed_repos
    """

    def test_all_repos_changed_no_false_unchanged_entries(self, analyzer):
        """
        Edge case: When all repos in existing_content are in changed_repos,
        the unchanged set is empty. No repos should be falsely listed as unchanged.
        """
        prompt = analyzer.build_delta_merge_prompt(
            domain_name="cidx-platform",
            existing_content=_CIDX_PLATFORM_DOMAIN_CONTENT,
            changed_repos=["code-indexer", "cidx-server"],
            new_repos=[],
            removed_repos=[],
            domain_list=["cidx-platform"],
        )

        if "## Unchanged Repositories" in prompt:
            section_start = prompt.index("## Unchanged Repositories")
            next_section = prompt.find("\n## ", section_start + 1)
            section_text = (
                prompt[section_start:next_section]
                if next_section != -1
                else prompt[section_start:]
            )
            assert "- code-indexer" not in section_text, (
                "code-indexer is CHANGED and must not be in Unchanged section"
            )
            assert "- cidx-server" not in section_text, (
                "cidx-server is CHANGED and must not be in Unchanged section"
            )

    def test_no_repos_changed_existing_repos_appear_unchanged(self, analyzer):
        """
        Edge case: When no repos changed (only new repos trigger the delta),
        ALL existing repos should appear as unchanged.
        """
        prompt = analyzer.build_delta_merge_prompt(
            domain_name="cidx-platform",
            existing_content=_CIDX_PLATFORM_DOMAIN_CONTENT,
            changed_repos=[],
            new_repos=["brand-new-repo"],
            removed_repos=[],
            domain_list=["cidx-platform"],
        )

        if "## Unchanged Repositories" in prompt:
            section_start = prompt.index("## Unchanged Repositories")
            next_section = prompt.find("\n## ", section_start + 1)
            section_text = (
                prompt[section_start:next_section]
                if next_section != -1
                else prompt[section_start:]
            )
            assert "code-indexer" in section_text or "cidx-server" in section_text, (
                "With no changed repos, existing repos should appear in the "
                "Unchanged Repositories section"
            )

    def test_single_repo_domain_changed_not_in_unchanged_section(self, analyzer):
        """
        Edge case: Single-repo domain where that repo is the changed one.
        No unchanged repos — section must not falsely list the changed repo.
        """
        single_repo_content = """\
# Domain Analysis: single-domain

## Repository Roles

### only-repo

The only repository in this domain.
"""
        prompt = analyzer.build_delta_merge_prompt(
            domain_name="single-domain",
            existing_content=single_repo_content,
            changed_repos=["only-repo"],
            new_repos=[],
            removed_repos=[],
            domain_list=["single-domain"],
        )

        if "## Unchanged Repositories" in prompt:
            section_start = prompt.index("## Unchanged Repositories")
            next_section = prompt.find("\n## ", section_start + 1)
            section_text = (
                prompt[section_start:next_section]
                if next_section != -1
                else prompt[section_start:]
            )
            assert "- only-repo" not in section_text, (
                "only-repo is the changed repo and must not be in Unchanged section"
            )

    def test_no_repo_headings_in_content_does_not_crash(self, analyzer):
        """
        Edge case: existing_content has no '### repo-name' headings.
        Extraction returns empty set — must not crash.
        """
        minimal_content = "# Domain Analysis: minimal\n\nNo repo roles documented yet.\n"

        prompt = analyzer.build_delta_merge_prompt(
            domain_name="minimal",
            existing_content=minimal_content,
            changed_repos=["some-repo"],
            new_repos=[],
            removed_repos=[],
            domain_list=["minimal"],
        )

        assert isinstance(prompt, str) and len(prompt) > 0

    def test_dict_format_changed_repos_excludes_alias_from_unchanged(self, analyzer):
        """
        Edge case: changed_repos may be a list of dicts (with 'alias' key).
        The alias must be excluded from the unchanged set even in dict format.
        """
        prompt = analyzer.build_delta_merge_prompt(
            domain_name="cidx-platform",
            existing_content=_CIDX_PLATFORM_DOMAIN_CONTENT,
            changed_repos=[{"alias": "code-indexer", "clone_path": "/repos/code-indexer"}],
            new_repos=[],
            removed_repos=[],
            domain_list=["cidx-platform"],
        )

        if "## Unchanged Repositories" in prompt:
            section_start = prompt.index("## Unchanged Repositories")
            next_section = prompt.find("\n## ", section_start + 1)
            section_text = (
                prompt[section_start:next_section]
                if next_section != -1
                else prompt[section_start:]
            )
            assert "cidx-server" in section_text, (
                "cidx-server must be in Unchanged section even when changed_repos "
                "uses dict format"
            )
            assert "- code-indexer" not in section_text, (
                "code-indexer (dict format alias) must not appear as unchanged"
            )


class TestNonRepoHeadingExclusion:
    """
    Finding 1 (Critical): Verify that non-repo ### headings are NOT falsely
    identified as repo aliases.

    The regex must only extract ### headings from the "Repository Roles" section,
    NOT from Cross-Domain Connections (### Outgoing, ### Incoming) or any other
    section that uses ### headings.
    """

    def test_outgoing_and_incoming_not_in_unchanged_repos(self, analyzer):
        """
        Finding 1: ### Outgoing and ### Incoming headings in Cross-Domain
        Connections MUST NOT appear as repo aliases in the unchanged repos set.

        This is the explicit regression test for the false-positive bug where
        the regex r"^### ([\w][\w\-\.]*)" matched ALL ### headings in the
        entire document, not just those under Repository Roles.
        """
        prompt = analyzer.build_delta_merge_prompt(
            domain_name="cidx-platform",
            existing_content=_CIDX_PLATFORM_DOMAIN_CONTENT,
            changed_repos=["code-indexer"],
            new_repos=[],
            removed_repos=[],
            domain_list=["cidx-platform"],
        )

        # Find the Unchanged Repositories section if present
        if "## Unchanged Repositories" in prompt:
            section_start = prompt.index("## Unchanged Repositories")
            next_section = prompt.find("\n## ", section_start + 1)
            section_text = (
                prompt[section_start:next_section]
                if next_section != -1
                else prompt[section_start:]
            )
            assert "- Outgoing" not in section_text, (
                "FINDING 1: 'Outgoing' (from ### Outgoing in Cross-Domain Connections) "
                "must NOT appear as a repo alias in the Unchanged Repositories section. "
                f"Unchanged section: {section_text!r}"
            )
            assert "- Incoming" not in section_text, (
                "FINDING 1: 'Incoming' (from ### Incoming in Cross-Domain Connections) "
                "must NOT appear as a repo alias in the Unchanged Repositories section. "
                f"Unchanged section: {section_text!r}"
            )

    def test_only_repository_roles_headings_extracted(self, analyzer):
        """
        Finding 1: Only ### headings within the "## Repository Roles" section
        should be identified as repo aliases. Headings in other sections
        (Intra-Domain Dependencies, Cross-Domain Connections, etc.) must be ignored.
        """
        content_with_multiple_sections = """\
# Domain Analysis: test-domain

## Repository Roles

### real-repo-a

A real repository.

### real-repo-b

Another real repository.

## Intra-Domain Dependencies

### Some Heading

Notes about dependencies.

## Cross-Domain Connections

### Outgoing

| Domain | Repos Involved | Dependency Type | Evidence |
|--------|----------------|-----------------|----------|

### Incoming

None documented.
"""
        prompt = analyzer.build_delta_merge_prompt(
            domain_name="test-domain",
            existing_content=content_with_multiple_sections,
            changed_repos=["real-repo-a"],
            new_repos=[],
            removed_repos=[],
            domain_list=["test-domain"],
        )

        if "## Unchanged Repositories" in prompt:
            section_start = prompt.index("## Unchanged Repositories")
            next_section = prompt.find("\n## ", section_start + 1)
            section_text = (
                prompt[section_start:next_section]
                if next_section != -1
                else prompt[section_start:]
            )
            # real-repo-b is in Repository Roles and unchanged - must appear
            assert "real-repo-b" in section_text, (
                "real-repo-b is in Repository Roles and is unchanged; "
                "it must appear in the Unchanged Repositories section"
            )
            # Outgoing, Incoming, and Some Heading are NOT repo aliases - must not appear
            assert "- Outgoing" not in section_text, (
                "FINDING 1: 'Outgoing' from Cross-Domain Connections must not be "
                "treated as a repo alias"
            )
            assert "- Incoming" not in section_text, (
                "FINDING 1: 'Incoming' from Cross-Domain Connections must not be "
                "treated as a repo alias"
            )
            assert "- Some Heading" not in section_text, (
                "FINDING 1: 'Some Heading' from Intra-Domain Dependencies must not "
                "be treated as a repo alias"
            )


class TestInvokeDeltaMerge:
    """Test public invoke_delta_merge() method for encapsulation (Code Review H1)."""

    @patch.object(DependencyMapAnalyzer, "_invoke_claude_cli")
    def test_invoke_delta_merge_calls_internal_method(self, mock_invoke, analyzer):
        """Test that invoke_delta_merge() properly delegates to _invoke_claude_cli()."""
        # Setup mock
        mock_invoke.return_value = "Claude CLI result"

        # Call public method
        prompt = "Test delta merge prompt"
        timeout = 600
        max_turns = 10

        result = analyzer.invoke_delta_merge(
            prompt=prompt,
            timeout=timeout,
            max_turns=max_turns,
        )

        # Verify internal method was called with correct parameters
        mock_invoke.assert_called_once_with(prompt, timeout, max_turns, allowed_tools="mcp__cidx-local__search_code")
        assert result == "Claude CLI result"
