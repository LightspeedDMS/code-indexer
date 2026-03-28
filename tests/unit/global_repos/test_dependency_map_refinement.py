"""
Unit tests for Story #359: Dependency Document Refinement Prompt.

Tests cover Component 1: build_refinement_prompt() method on DependencyMapAnalyzer.

TDD RED PHASE: Tests written before production code changes exist.

The refinement prompt is EDITORIAL (fact-checking existing docs against source code),
not AUTHORIAL (not rewriting from scratch).
"""


def _make_analyzer(tmp_path):
    """Build a minimal DependencyMapAnalyzer for prompt testing."""
    from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer

    return DependencyMapAnalyzer(
        golden_repos_root=tmp_path,
        cidx_meta_path=tmp_path / "cidx-meta",
        pass_timeout=60,
    )


SAMPLE_EXISTING_BODY = """\
# Domain Analysis: auth-domain

## Overview

The auth-domain provides authentication and authorization services.
It handles JWT token issuance, validation, and session management.

## Repository Roles

### auth-service
Issues JWT tokens for authenticated users. Provides REST API for login.

### token-validator
Validates JWT tokens across service boundaries. Used by other services.

## Intra-Domain Dependencies

| Consumer | Provider | Dependency Type | Evidence |
|----------|----------|-----------------|---------|
| token-validator | auth-service | Code-level | Imports JWTConfig from auth-service |

## Cross-Domain Connections

### Outgoing Dependencies

| This Repo | Depends On | Target Domain | Type | Why | Evidence |
|---|---|---|---|---|---|

### Incoming Dependencies

| External Repo | Depends On | Source Domain | Type | Why | Evidence |
|---|---|---|---|---|---|

No verified cross-domain dependencies.
"""

SAMPLE_REPOS = ["auth-service", "token-validator"]


class TestBuildRefinementPromptContainsDomainName:
    """build_refinement_prompt includes the domain name."""

    def test_domain_name_in_prompt(self, tmp_path):
        """Prompt must include the domain name for context."""
        analyzer = _make_analyzer(tmp_path)
        prompt = analyzer.build_refinement_prompt(
            domain_name="auth-domain",
            existing_body=SAMPLE_EXISTING_BODY,
            participating_repos=SAMPLE_REPOS,
        )
        assert "auth-domain" in prompt

    def test_different_domain_name_in_prompt(self, tmp_path):
        """Domain name is parameterized, not hardcoded."""
        analyzer = _make_analyzer(tmp_path)
        different_body = "# Domain Analysis: data-pipeline\n\nPipeline overview.\n"
        prompt = analyzer.build_refinement_prompt(
            domain_name="data-pipeline",
            existing_body=different_body,
            participating_repos=["pipeline-service"],
        )
        assert "data-pipeline" in prompt
        assert "auth-domain" not in prompt


class TestBuildRefinementPromptContainsExistingBody:
    """build_refinement_prompt includes the existing content for fact-checking."""

    def test_existing_body_in_prompt(self, tmp_path):
        """Prompt must include the existing document body."""
        analyzer = _make_analyzer(tmp_path)
        prompt = analyzer.build_refinement_prompt(
            domain_name="auth-domain",
            existing_body=SAMPLE_EXISTING_BODY,
            participating_repos=SAMPLE_REPOS,
        )
        assert SAMPLE_EXISTING_BODY in prompt

    def test_existing_body_section_labeled(self, tmp_path):
        """Existing content should be clearly labeled in the prompt."""
        analyzer = _make_analyzer(tmp_path)
        prompt = analyzer.build_refinement_prompt(
            domain_name="auth-domain",
            existing_body=SAMPLE_EXISTING_BODY,
            participating_repos=SAMPLE_REPOS,
        )
        # The existing content should be in a clearly labeled section
        prompt_lower = prompt.lower()
        assert any(
            kw in prompt_lower for kw in ["existing", "current", "document", "content"]
        ), "Prompt must have a section label for the existing content"


class TestBuildRefinementPromptContainsParticipatingRepos:
    """build_refinement_prompt includes the participating repositories."""

    def test_all_repos_listed(self, tmp_path):
        """All participating repos must appear in the prompt."""
        analyzer = _make_analyzer(tmp_path)
        prompt = analyzer.build_refinement_prompt(
            domain_name="auth-domain",
            existing_body=SAMPLE_EXISTING_BODY,
            participating_repos=["auth-service", "token-validator", "session-store"],
        )
        assert "auth-service" in prompt
        assert "token-validator" in prompt
        assert "session-store" in prompt

    def test_single_repo_listed(self, tmp_path):
        """Works with a single participating repo."""
        analyzer = _make_analyzer(tmp_path)
        prompt = analyzer.build_refinement_prompt(
            domain_name="single-domain",
            existing_body="# Domain Analysis: single-domain\n\n## Overview\nMinimal.",
            participating_repos=["solo-service"],
        )
        assert "solo-service" in prompt


class TestBuildRefinementPromptFormat:
    """build_refinement_prompt starts with expected header and has correct structure."""

    def test_prompt_starts_with_heading(self, tmp_path):
        """Prompt must start with a markdown heading referencing the domain."""
        analyzer = _make_analyzer(tmp_path)
        prompt = analyzer.build_refinement_prompt(
            domain_name="auth-domain",
            existing_body=SAMPLE_EXISTING_BODY,
            participating_repos=SAMPLE_REPOS,
        )
        # First non-empty line should be a markdown heading
        lines = [line for line in prompt.splitlines() if line.strip()]
        assert lines[0].startswith(
            "#"
        ), f"Prompt should start with a markdown heading, got: {lines[0]!r}"

    def test_prompt_is_editorial_not_authorial(self, tmp_path):
        """Prompt must frame the task as fact-checking, not rewriting."""
        analyzer = _make_analyzer(tmp_path)
        prompt = analyzer.build_refinement_prompt(
            domain_name="auth-domain",
            existing_body=SAMPLE_EXISTING_BODY,
            participating_repos=SAMPLE_REPOS,
        )
        prompt_lower = prompt.lower()
        # Should reference fact-checking, verifying, or refining
        assert any(
            kw in prompt_lower
            for kw in ["fact", "verify", "refine", "check", "review", "correct"]
        ), "Prompt must frame the task as editorial fact-checking"

    def test_prompt_instructs_to_preserve_structure(self, tmp_path):
        """Prompt must tell Claude to preserve doc structure, not rewrite from scratch."""
        analyzer = _make_analyzer(tmp_path)
        prompt = analyzer.build_refinement_prompt(
            domain_name="auth-domain",
            existing_body=SAMPLE_EXISTING_BODY,
            participating_repos=SAMPLE_REPOS,
        )
        prompt_lower = prompt.lower()
        assert any(
            kw in prompt_lower
            for kw in ["preserv", "keep", "structure", "maintain", "retain"]
        ), "Prompt must instruct Claude to preserve document structure"

    def test_prompt_prohibits_speculative_content(self, tmp_path):
        """Prompt must prohibit recommendations/suggestions (fact-only output)."""
        analyzer = _make_analyzer(tmp_path)
        prompt = analyzer.build_refinement_prompt(
            domain_name="auth-domain",
            existing_body=SAMPLE_EXISTING_BODY,
            participating_repos=SAMPLE_REPOS,
        )
        prompt_lower = prompt.lower()
        assert any(
            kw in prompt_lower
            for kw in ["prohibited", "do not", "only", "fact", "evidence"]
        ), "Prompt must state what content is prohibited (speculative content)"

    def test_prompt_output_format_instruction(self, tmp_path):
        """Prompt must instruct Claude on output format (full document, no preamble)."""
        analyzer = _make_analyzer(tmp_path)
        prompt = analyzer.build_refinement_prompt(
            domain_name="auth-domain",
            existing_body=SAMPLE_EXISTING_BODY,
            participating_repos=SAMPLE_REPOS,
        )
        prompt_lower = prompt.lower()
        assert any(
            kw in prompt_lower for kw in ["output", "return", "respond", "provide"]
        ), "Prompt must include output format instructions"

    def test_returns_string(self, tmp_path):
        """build_refinement_prompt returns a string."""
        analyzer = _make_analyzer(tmp_path)
        result = analyzer.build_refinement_prompt(
            domain_name="auth-domain",
            existing_body=SAMPLE_EXISTING_BODY,
            participating_repos=SAMPLE_REPOS,
        )
        assert isinstance(result, str)

    def test_prompt_is_non_empty(self, tmp_path):
        """Returned prompt is non-empty."""
        analyzer = _make_analyzer(tmp_path)
        result = analyzer.build_refinement_prompt(
            domain_name="auth-domain",
            existing_body=SAMPLE_EXISTING_BODY,
            participating_repos=SAMPLE_REPOS,
        )
        assert len(result) > 100, "Prompt should be substantial (> 100 chars)"
