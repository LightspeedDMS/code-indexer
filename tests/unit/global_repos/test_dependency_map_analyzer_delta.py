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
        mock_invoke.assert_called_once_with(prompt, timeout, max_turns)
        assert result == "Claude CLI result"
