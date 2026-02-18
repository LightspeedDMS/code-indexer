"""
Unit tests for DependencyMapAnalyzer (Story #192).

Tests the multi-pass Claude CLI pipeline for generating dependency maps:
- CLAUDE.md orientation file generation
- Pass 1: Domain synthesis (JSON output)
- Pass 2: Per-domain analysis
- Pass 3: Index generation
- Direct subprocess invocation with --max-turns
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer


class TestClaudeMdGeneration:
    """Test CLAUDE.md orientation file generation (AC2)."""

    def test_generate_claude_md_creates_file(self, tmp_path):
        """Test that generate_claude_md creates CLAUDE.md in golden-repos root."""
        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        repo_list = [
            {"alias": "repo1", "description_summary": "First repository"},
            {"alias": "repo2", "description_summary": "Second repository"},
        ]

        analyzer.generate_claude_md(repo_list)

        claude_md = tmp_path / "CLAUDE.md"
        assert claude_md.exists()

        content = claude_md.read_text()
        assert "CIDX Dependency Map Analysis" in content
        assert "Available Repositories" in content
        assert "**repo1**: First repository" in content
        assert "**repo2**: Second repository" in content
        assert "dependency" in content.lower()

    def test_generate_claude_md_overwrites_existing(self, tmp_path):
        """Test that generate_claude_md overwrites existing CLAUDE.md."""
        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("Old content")

        repo_list = [{"alias": "repo1", "description_summary": "New repo"}]
        analyzer.generate_claude_md(repo_list)

        content = claude_md.read_text()
        assert "Old content" not in content
        assert "**repo1**: New repo" in content


class TestPass1Synthesis:
    """Test Pass 1: Domain synthesis (AC1)."""

    @patch("subprocess.run")
    def test_run_pass_1_invokes_claude_cli(
        self, mock_subprocess, tmp_path
    ):
        """Test that run_pass_1_synthesis invokes Claude CLI with correct parameters."""
        # Mock subprocess response
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "name": "authentication",
                        "description": "Auth domain",
                        "participating_repos": ["auth-service", "web-app"],
                        "repo_paths": {
                            "auth-service": "/path/to/auth-service",
                            "web-app": "/path/to/web-app",
                        },
                    }
                ]
            ),
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        repo_descriptions = {"repo1": "Content 1", "repo2": "Content 2"}

        # Provide repo_list with matching aliases and clone_paths for validation
        repo_list = [
            {"alias": "auth-service", "description_summary": "Auth service", "clone_path": "/path/to/auth-service"},
            {"alias": "web-app", "description_summary": "Web application", "clone_path": "/path/to/web-app"},
        ]

        result = analyzer.run_pass_1_synthesis(staging_dir, repo_descriptions, repo_list=repo_list, max_turns=50)

        # Verify subprocess called with correct arguments
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args

        # Check command structure (last element is the prompt)
        # Pass 1 uses allowed_tools=None (no --allowedTools flag - built-in tools available)
        cmd = call_args[0][0]
        assert cmd[0] == "claude"
        assert "--print" in cmd
        assert "--model" in cmd
        assert "--max-turns" in cmd
        assert "-p" in cmd
        assert "Identify domain clusters" in cmd[-1]  # Last element is the prompt
        assert call_args[1]["cwd"] == str(tmp_path)
        assert call_args[1]["timeout"] == 600  # full pass_timeout (Pass 1 is heaviest phase)

        # Verify result
        assert len(result) == 1
        assert result[0]["name"] == "authentication"

    def test_run_pass_1_writes_domains_json(self, tmp_path):
        """Test that run_pass_1_synthesis writes _domains.json to staging directory."""
        with patch("subprocess.run") as mock_subprocess:
            mock_subprocess.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "name": "domain1",
                            "description": "First domain",
                            "participating_repos": ["repo1"],
                            "repo_paths": {"repo1": "/path/to/repo1"},
                        }
                    ]
                ),
            )

            analyzer = DependencyMapAnalyzer(
                golden_repos_root=tmp_path,
                cidx_meta_path=tmp_path / "cidx-meta",
                pass_timeout=600,
            )

            staging_dir = tmp_path / "staging"
            staging_dir.mkdir()

            # Provide repo_list with matching alias and clone_path for validation
            repo_list = [
                {"alias": "repo1", "description_summary": "First repository", "clone_path": "/path/to/repo1"},
            ]

            analyzer.run_pass_1_synthesis(staging_dir, {}, repo_list=repo_list, max_turns=50)

            domains_file = staging_dir / "_domains.json"
            assert domains_file.exists()

            domains = json.loads(domains_file.read_text())
            assert len(domains) == 1
            assert domains[0]["name"] == "domain1"


class TestPass2PerDomain:
    """Test Pass 2: Per-domain analysis (AC1)."""

    @patch("subprocess.run")
    def test_run_pass_2_invokes_claude_cli(
        self, mock_subprocess, tmp_path
    ):
        """Test that run_pass_2_per_domain invokes Claude CLI with domain context."""
        # Generate output >1000 chars to avoid retry logic
        content = "# Authentication Domain\n\nDetailed analysis with sufficient content to avoid retry. " + "X" * 1000
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=content,
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "authentication",
            "description": "Auth domain",
            "participating_repos": ["auth-service", "web-app"],
        }

        domain_list = [domain]

        analyzer.run_pass_2_per_domain(staging_dir, domain, domain_list, repo_list=[], max_turns=60)

        # Verify subprocess called with full timeout
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args

        assert call_args[0][0][0] == "claude"
        assert "--max-turns" in call_args[0][0]
        assert "60" in call_args[0][0]
        assert call_args[1]["timeout"] == 600  # full pass_timeout

    def test_run_pass_2_writes_domain_file_with_frontmatter(self, tmp_path):
        """Test that run_pass_2_per_domain writes domain file with YAML frontmatter and strips meta-commentary."""
        with patch("subprocess.run") as mock_subprocess:
            # Mock stdout with meta-commentary that should be stripped
            # Must be >1000 chars to pass quality gate (Fix 3)
            mock_subprocess.return_value = MagicMock(
                returncode=0,
                stdout="Based on my analysis:\n\n# Authentication\n\nDomain analysis content here. " + "X" * 1000,
            )

            analyzer = DependencyMapAnalyzer(
                golden_repos_root=tmp_path,
                cidx_meta_path=tmp_path / "cidx-meta",
                pass_timeout=600,
            )

            staging_dir = tmp_path / "staging"
            staging_dir.mkdir()

            domain = {
                "name": "authentication",
                "description": "Auth domain",
                "participating_repos": ["auth-service", "web-app"],
            }

            analyzer.run_pass_2_per_domain(
                staging_dir, domain, [domain], repo_list=[], max_turns=60
            )

            domain_file = staging_dir / "authentication.md"
            assert domain_file.exists()

            content = domain_file.read_text()
            assert content.startswith("---\n")
            assert "domain: authentication" in content
            assert "last_analyzed:" in content
            assert "participating_repos:" in content
            assert "- auth-service" in content
            assert "- web-app" in content
            assert "---\n" in content
            assert "Domain analysis content here" in content
            # Verify meta-commentary was stripped
            assert "Based on my analysis" not in content

    @patch("subprocess.run")
    def test_run_pass_2_prompt_includes_tech_stack_verification(
        self, mock_subprocess, tmp_path
    ):
        """Test that run_pass_2_per_domain prompt includes Technology Stack Verification mandate."""
        # Generate output >1000 chars to avoid retry logic
        content = "# Domain Analysis\n\nContent here with sufficient length to avoid retry logic. " + "Y" * 1000
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=content,
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1"],
        }

        analyzer.run_pass_2_per_domain(
            staging_dir, domain, [domain], repo_list=[], max_turns=60
        )

        # Verify subprocess was called with prompt containing tech stack verification
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args
        prompt = call_args[0][0][-1]  # Last element is the prompt

        # Verify the Technology Stack Verification section exists
        assert "## MANDATORY: Technology Stack Verification" in prompt
        assert "Search for dependency manifests" in prompt
        assert "Check actual source file extensions" in prompt
        assert "Do NOT assume technology based on tool names" in prompt
        assert "If a repo uses a library written in language X as a binding/wrapper in language Y, the repo's primary language is Y, not X" in prompt


class TestPass3Index:
    """Test Pass 3: Index generation (AC1)."""

    @patch("subprocess.run")
    def test_run_pass_3_invokes_claude_cli(
        self, mock_subprocess, tmp_path
    ):
        """Test that run_pass_3_index invokes Claude CLI with index generation prompt."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout="# Dependency Map Index\n\nCatalog and matrix...",
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain_list = [
            {
                "name": "domain1",
                "description": "First",
                "participating_repos": ["repo1"],
            }
        ]
        repo_list = [{"alias": "repo1", "description_summary": "Repo 1"}]

        analyzer.run_pass_3_index(staging_dir, domain_list, repo_list, max_turns=30)

        # Verify subprocess called
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args

        assert call_args[0][0][0] == "claude"
        assert "--max-turns" in call_args[0][0]
        assert "30" in call_args[0][0]
        assert call_args[1]["timeout"] == 300  # half of pass_timeout

    def test_run_pass_3_writes_index_with_frontmatter(self, tmp_path):
        """Test that run_pass_3_index writes _index.md with YAML frontmatter."""
        with patch("subprocess.run") as mock_subprocess:
            mock_subprocess.return_value = MagicMock(
                returncode=0,
                stdout="# Index\n\nCatalog content...",
            )

            analyzer = DependencyMapAnalyzer(
                golden_repos_root=tmp_path,
                cidx_meta_path=tmp_path / "cidx-meta",
                pass_timeout=600,
            )

            staging_dir = tmp_path / "staging"
            staging_dir.mkdir()

            domain_list = [{"name": "d1", "description": "Domain 1", "participating_repos": []}]
            repo_list = [{"alias": "r1", "description_summary": "Repo 1"}]

            analyzer.run_pass_3_index(staging_dir, domain_list, repo_list, max_turns=30)

            index_file = staging_dir / "_index.md"
            assert index_file.exists()

            content = index_file.read_text()
            assert content.startswith("---\n")
            assert "schema_version:" in content
            assert "last_analyzed:" in content
            assert "repos_analyzed_count: 1" in content
            assert "domains_count: 1" in content
            assert "Catalog content" in content


class TestPassOneValidation:
    """Test Pass 1 post-processing validation logic."""

    @patch("subprocess.run")
    def test_strips_markdown_headings_from_auto_created_description(self, mock_subprocess, tmp_path):
        """Unassigned repos with heading-prefixed descriptions get cleaned up."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([]),  # Empty domain list - all repos unassigned
        )
        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )
        staging = tmp_path / "staging"
        staging.mkdir()
        repo_list = [
            {"alias": "my-repo", "description_summary": "## My Repo Description", "clone_path": "/path/to/my-repo"},
        ]
        result = analyzer.run_pass_1_synthesis(staging, {}, repo_list=repo_list, max_turns=50)
        assert len(result) == 1
        assert result[0]["name"] == "my-repo"
        assert result[0]["description"] == "My Repo Description"
        assert "##" not in result[0]["description"]

    @patch("subprocess.run")
    def test_alias_only_description_gets_standalone_suffix(self, mock_subprocess, tmp_path):
        """When description equals alias name, use '(standalone repository)' suffix."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([]),
        )
        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )
        staging = tmp_path / "staging"
        staging.mkdir()
        repo_list = [
            {"alias": "my-repo", "description_summary": "my-repo", "clone_path": "/path/to/my-repo"},
        ]
        result = analyzer.run_pass_1_synthesis(staging, {}, repo_list=repo_list, max_turns=50)
        assert result[0]["description"] == "my-repo (standalone repository)"

    @patch("subprocess.run")
    def test_accepts_versioned_directory_paths(self, mock_subprocess, tmp_path):
        """Repos with .versioned/ paths should not be filtered out."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([{
                "name": "test-domain",
                "description": "Test domain",
                "participating_repos": ["flask-large"],
                "repo_paths": {"flask-large": "/golden-repos/.versioned/flask-large/v_20260214"},
            }]),
        )
        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )
        staging = tmp_path / "staging"
        staging.mkdir()
        repo_list = [
            {"alias": "flask-large", "description_summary": "Flask framework", "clone_path": "/golden-repos/flask-large"},
        ]
        result = analyzer.run_pass_1_synthesis(staging, {}, repo_list=repo_list, max_turns=50)
        # flask-large should NOT be filtered - the .versioned path contains the alias
        domain_repos = result[0]["participating_repos"]
        assert "flask-large" in domain_repos

    @patch("subprocess.run")
    def test_filters_repos_with_wrong_paths(self, mock_subprocess, tmp_path):
        """Repos with paths not containing the alias should be filtered out."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([{
                "name": "test-domain",
                "description": "Test domain",
                "participating_repos": ["my-repo"],
                "repo_paths": {"my-repo": "/totally/wrong/directory"},
            }]),
        )
        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )
        staging = tmp_path / "staging"
        staging.mkdir()
        repo_list = [
            {"alias": "my-repo", "description_summary": "My repository", "clone_path": "/path/to/my-repo"},
        ]
        result = analyzer.run_pass_1_synthesis(staging, {}, repo_list=repo_list, max_turns=50)
        # my-repo should be filtered from the domain and auto-created as standalone
        # The original domain should be removed (empty after filtering)
        # my-repo should appear as an auto-created standalone domain
        standalone = [d for d in result if d["name"] == "my-repo"]
        assert len(standalone) == 1
        assert "Auto-assigned" in standalone[0]["evidence"]

    @patch("subprocess.run")
    def test_short_alias_not_false_positive_in_path(self, mock_subprocess, tmp_path):
        """Short alias like 'db' should not match path containing 'adobe'."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([{
                "name": "test-domain",
                "description": "Test domain",
                "participating_repos": ["db"],
                "repo_paths": {"db": "/home/repos/adobe-tools/src"},
            }]),
        )
        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )
        staging = tmp_path / "staging"
        staging.mkdir()
        repo_list = [
            {"alias": "db", "description_summary": "Database repo", "clone_path": "/path/to/db"},
        ]
        result = analyzer.run_pass_1_synthesis(staging, {}, repo_list=repo_list, max_turns=50)
        # "db" should be filtered because "adobe-tools" doesn't contain "db" as a delimited segment
        standalone = [d for d in result if d["name"] == "db"]
        assert len(standalone) == 1
        assert "Auto-assigned" in standalone[0]["evidence"]


class TestStripMetaCommentary:
    """Test meta-commentary stripping from Pass 2 output."""

    def test_strips_based_on_preamble(self):
        text = "Based on my comprehensive analysis, here are the findings:\n\n## Overview\n\nContent here."
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert result.startswith("## Overview")

    def test_strips_perfect_preamble(self):
        text = "Perfect. Now I have sufficient evidence.\n\n---\n\n## Domain Analysis\n\nContent."
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert result.startswith("## Domain Analysis")

    def test_preserves_content_starting_with_heading(self):
        text = "## Overview\n\nThe domain consists of..."
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert result == text

    def test_preserves_content_starting_with_bold(self):
        text = "**Domain Verification**: CONFIRMED\n\nContent."
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert result == text

    def test_strips_multiple_meta_lines(self):
        text = "Let me compile the findings:\n\n---\n\n# Analysis\n\nDetails."
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert result.startswith("# Analysis")

    def test_empty_input(self):
        assert DependencyMapAnalyzer._strip_meta_commentary("") == ""

    def test_only_meta_commentary(self):
        text = "Based on my analysis, here is the result."
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        # Should return something (at least the text if no content found)
        assert len(result) > 0

    def test_strips_meta_with_interleaved_empty_lines(self):
        text = "Based on analysis.\n\nHere is the result:\n\n## Findings\n\nContent."
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert result.startswith("## Findings")


class TestPass1PromptGuardrails:
    """Test Pass 1 prompt contains guardrails against non-JSON output."""

    @patch("subprocess.run")
    def test_prompt_contains_internal_verification_instruction(self, mock_subprocess, tmp_path):
        """Test that Pass 1 prompt instructs Claude to verify internally without outputting verification text."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([
                {
                    "name": "test-domain",
                    "description": "Test",
                    "participating_repos": ["repo1"],
                    "repo_paths": {"repo1": "/path/to/repo1"},
                }
            ]),
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        repo_list = [
            {"alias": "repo1", "description_summary": "Repo 1", "clone_path": "/path/to/repo1"},
        ]

        analyzer.run_pass_1_synthesis(staging_dir, {}, repo_list=repo_list, max_turns=50)

        # Verify prompt contains critical guardrails
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args
        prompt = call_args[0][0][-1]  # Last element is the prompt

        # Verify COMPLETENESS MANDATE section instructs internal verification
        assert "Verify INTERNALLY that total repos across all domains equals" in prompt
        assert "Do NOT output the verification" in prompt
        assert "All verification must be done INTERNALLY" in prompt
        assert "Your output must contain ONLY JSON" in prompt

        # Verify Output Format section prohibits non-JSON content
        assert "Your ENTIRE response must be ONLY a valid JSON array" in prompt
        assert "Do NOT output completeness checks, summaries, or commentary" in prompt
        assert "ONLY the JSON array" in prompt


class TestPass1JsonParseFailure:
    """Test Pass 1 JSON parse failure raises RuntimeError (FIX 3)."""

    @patch("subprocess.run")
    def test_run_pass_1_raises_on_bad_json(
        self, mock_subprocess, tmp_path
    ):
        """Test that run_pass_1_synthesis raises RuntimeError on unparseable JSON."""
        # Mock subprocess to return invalid JSON
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout="This is not valid JSON at all",
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        with pytest.raises(RuntimeError, match="Pass 1 \\(Synthesis\\) returned unparseable output"):
            analyzer.run_pass_1_synthesis(staging_dir, {}, repo_list=[], max_turns=50)

    @patch("subprocess.run")
    def test_pass_1_single_shot_retry_succeeds(self, mock_subprocess, tmp_path):
        """Test Pass 1 single-shot retry succeeds when agentic attempt returns commentary."""
        # First call (agentic): returns commentary instead of JSON
        # Second call (single-shot): returns valid JSON
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # Agentic attempt: commentary output (no JSON)
                return MagicMock(
                    returncode=0,
                    stdout="The domain synthesis analysis is complete. The JSON output above contains 7 domain clusters...",
                )
            else:
                # Single-shot retry: valid JSON
                return MagicMock(
                    returncode=0,
                    stdout=json.dumps([
                        {
                            "name": "test-domain",
                            "description": "Test domain",
                            "participating_repos": ["repo1"],
                            "repo_paths": {"repo1": "/path/to/repo1"},
                        }
                    ]),
                )

        mock_subprocess.side_effect = side_effect

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        repo_list = [
            {"alias": "repo1", "description_summary": "Repo 1", "clone_path": "/path/to/repo1"},
        ]

        result = analyzer.run_pass_1_synthesis(staging_dir, {}, repo_list=repo_list, max_turns=50)

        # Verify subprocess was called twice (agentic + single-shot retry)
        assert mock_subprocess.call_count == 2

        # Verify first call used max_turns=50 (agentic)
        first_call_args = mock_subprocess.call_args_list[0]
        first_cmd = first_call_args[0][0]
        assert "--max-turns" in first_cmd
        first_turns_idx = first_cmd.index("--max-turns")
        assert first_cmd[first_turns_idx + 1] == "50"

        # Verify second call used max_turns=0 (single-shot - no --max-turns flag)
        second_call_args = mock_subprocess.call_args_list[1]
        second_cmd = second_call_args[0][0]
        assert "--max-turns" not in second_cmd

        # Verify result is from successful retry
        assert len(result) == 1
        assert result[0]["name"] == "test-domain"

    @patch("subprocess.run")
    def test_pass_1_both_attempts_fail(self, mock_subprocess, tmp_path):
        """Test Pass 1 raises RuntimeError when both agentic and single-shot attempts fail."""
        # Both calls return commentary (no JSON)
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout="This is commentary with no JSON array.",
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        repo_list = [
            {"alias": "repo1", "description_summary": "Repo 1", "clone_path": "/path/to/repo1"},
        ]

        # Verify RuntimeError is raised with "both attempts" message
        with pytest.raises(RuntimeError, match="Pass 1 \\(Synthesis\\) returned unparseable output on both attempts"):
            analyzer.run_pass_1_synthesis(staging_dir, {}, repo_list=repo_list, max_turns=50)

        # Verify subprocess was called twice
        assert mock_subprocess.call_count == 2

    @patch("subprocess.run")
    def test_pass_1_first_attempt_succeeds_no_retry(self, mock_subprocess, tmp_path):
        """Test Pass 1 does not retry when first agentic attempt returns valid JSON."""
        # First call returns valid JSON - no retry needed
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([
                {
                    "name": "test-domain",
                    "description": "Test domain",
                    "participating_repos": ["repo1"],
                    "repo_paths": {"repo1": "/path/to/repo1"},
                }
            ]),
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        repo_list = [
            {"alias": "repo1", "description_summary": "Repo 1", "clone_path": "/path/to/repo1"},
        ]

        result = analyzer.run_pass_1_synthesis(staging_dir, {}, repo_list=repo_list, max_turns=50)

        # Verify subprocess was called only once (no retry)
        assert mock_subprocess.call_count == 1

        # Verify result is correct
        assert len(result) == 1
        assert result[0]["name"] == "test-domain"


class TestIncrementalPass2:
    """Test incremental Pass 2 with previous_domain_dir (FIX 9)."""

    @patch("subprocess.run")
    def test_run_pass_2_uses_previous_domain_content(
        self, mock_subprocess, tmp_path
    ):
        """Test that run_pass_2_per_domain includes previous domain content in prompt."""
        # Generate output >1000 chars to avoid retry logic
        content = "# Updated Domain Analysis\n\nNew analysis with sufficient content to avoid retry. " + "Z" * 1000
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=content,
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        # Create previous domain directory with existing content
        # Must be >1000 chars with headings to pass Fix 2 quality check
        previous_dir = tmp_path / "previous"
        previous_dir.mkdir()
        previous_domain_file = previous_dir / "authentication.md"
        previous_content = "---\nOld frontmatter\n---\n\n# Previous Analysis\n\nOld content here. " + "Y" * 1000
        previous_domain_file.write_text(previous_content)

        domain = {
            "name": "authentication",
            "description": "Auth domain",
            "participating_repos": ["auth-service"],
        }

        analyzer.run_pass_2_per_domain(
            staging_dir, domain, [domain], repo_list=[], max_turns=60, previous_domain_dir=previous_dir
        )

        # Verify subprocess was called with prompt containing previous content
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args
        prompt = call_args[0][0][-1]  # Last element is the prompt

        assert "Previous Analysis (refine and improve)" in prompt
        assert "Old content here" in prompt


class TestAllowedToolsPerPass:
    """Test Fix 1: Make --allowedTools per-pass configurable."""

    @patch("subprocess.run")
    def test_pass_1_no_allowed_tools(self, mock_subprocess, tmp_path):
        """Test that Pass 1 is called with allowed_tools=None (no --allowedTools flag) and max_turns=0 (single-shot)."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([
                {
                    "name": "test-domain",
                    "description": "Test",
                    "participating_repos": ["repo1"],
                    "repo_paths": {"repo1": "/path/to/repo1"},
                }
            ]),
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        repo_list = [
            {"alias": "repo1", "description_summary": "Repo 1", "clone_path": "/path/to/repo1"},
        ]

        analyzer.run_pass_1_synthesis(staging_dir, {}, repo_list=repo_list, max_turns=0)

        # Verify --allowedTools is NOT present (allowed_tools=None means no flag)
        # Verify --max-turns is NOT present (max_turns=0 means single-shot mode)
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args
        cmd = call_args[0][0]
        assert "--allowedTools" not in cmd
        assert "--max-turns" not in cmd

    @patch("subprocess.run")
    def test_pass_2_has_allowed_tools(self, mock_subprocess, tmp_path):
        """Test that Pass 2 is called with --allowedTools mcp__cidx-local__search_code."""
        # Generate output >1000 chars to avoid retry logic
        content = "# Domain Analysis\n\nContent here with sufficient length to avoid retry logic. " + "W" * 1000
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=content,
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1"],
        }

        analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=[], max_turns=50)

        # Verify --allowedTools is present with correct value
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args
        cmd = call_args[0][0]
        assert "--allowedTools" in cmd
        tools_idx = cmd.index("--allowedTools")
        assert cmd[tools_idx + 1] == "mcp__cidx-local__search_code"

    @patch("subprocess.run")
    def test_pass_3_no_allowed_tools(self, mock_subprocess, tmp_path):
        """Test that Pass 3 is called with allowed_tools=None (no --allowedTools flag) and max_turns=0 (single-shot)."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout="# Index\n\nCatalog here.",
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain_list = [{"name": "d1", "description": "Domain 1", "participating_repos": []}]
        repo_list = [{"alias": "r1", "description_summary": "Repo 1"}]

        analyzer.run_pass_3_index(staging_dir, domain_list, repo_list, max_turns=0)

        # Verify --allowedTools is NOT present (allowed_tools=None means no flag)
        # Verify --max-turns is NOT present (max_turns=0 means single-shot mode)
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args
        cmd = call_args[0][0]
        assert "--allowedTools" not in cmd
        assert "--max-turns" not in cmd


class TestSingleShotVsAgenticMode:
    """Test that max_turns=0 enables single-shot mode (no --max-turns flag)."""

    @patch("subprocess.run")
    def test_single_shot_mode_omits_max_turns(self, mock_subprocess, tmp_path):
        """Test that max_turns=0 omits --max-turns flag (single-shot print mode)."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([
                {
                    "name": "test-domain",
                    "description": "Test",
                    "participating_repos": ["repo1"],
                    "repo_paths": {"repo1": "/path/to/repo1"},
                }
            ]),
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        repo_list = [
            {"alias": "repo1", "description_summary": "Repo 1", "clone_path": "/path/to/repo1"},
        ]

        # max_turns=0 should omit --max-turns from command
        analyzer.run_pass_1_synthesis(staging_dir, {}, repo_list=repo_list, max_turns=0)

        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args
        cmd = call_args[0][0]

        # Verify command structure: claude --print --model opus -p <prompt>
        # WITHOUT --max-turns
        assert cmd[0] == "claude"
        assert "--print" in cmd
        assert "--model" in cmd
        assert "--max-turns" not in cmd
        assert "-p" in cmd

    @patch("subprocess.run")
    def test_agentic_mode_includes_max_turns(self, mock_subprocess, tmp_path):
        """Test that max_turns>0 includes --max-turns flag (agentic mode)."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([
                {
                    "name": "test-domain",
                    "description": "Test",
                    "participating_repos": ["repo1"],
                    "repo_paths": {"repo1": "/path/to/repo1"},
                }
            ]),
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        repo_list = [
            {"alias": "repo1", "description_summary": "Repo 1", "clone_path": "/path/to/repo1"},
        ]

        # max_turns=50 should include --max-turns 50
        analyzer.run_pass_1_synthesis(staging_dir, {}, repo_list=repo_list, max_turns=50)

        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args
        cmd = call_args[0][0]

        # Verify command includes --max-turns 50
        assert cmd[0] == "claude"
        assert "--print" in cmd
        assert "--model" in cmd
        assert "--max-turns" in cmd
        turns_idx = cmd.index("--max-turns")
        assert cmd[turns_idx + 1] == "50"
        assert "-p" in cmd


class TestEmptyOutputDetection:
    """Test Fix 2: Add empty output detection + retry for Pass 2."""

    @patch("subprocess.run")
    def test_pass_2_retries_on_empty_output(self, mock_subprocess, tmp_path):
        """Test that Pass 2 retries with reduced turns when output is empty."""
        # First call returns empty output, second call returns content
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return MagicMock(returncode=0, stdout="")
            else:
                # Must be >1000 chars to pass quality gate (Fix 3)
                return MagicMock(returncode=0, stdout="# Domain Analysis\n\nRetry succeeded. " + "X" * 1000)

        mock_subprocess.side_effect = side_effect

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1"],
        }

        analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=[], max_turns=50)

        # Verify subprocess was called twice
        assert mock_subprocess.call_count == 2

        # Verify second call used max_turns=10
        second_call_args = mock_subprocess.call_args_list[1]
        cmd = second_call_args[0][0]
        assert "--max-turns" in cmd
        turns_idx = cmd.index("--max-turns")
        assert cmd[turns_idx + 1] == "10"

        # Verify output file was written with retry content
        domain_file = staging_dir / "test-domain.md"
        assert domain_file.exists()
        content = domain_file.read_text()
        assert "Retry succeeded" in content

    @patch("subprocess.run")
    def test_pass_2_retries_on_very_short_output(self, mock_subprocess, tmp_path):
        """Test that Pass 2 retries when output is very short (<1000 chars)."""
        # First call returns very short output, second call returns full content
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return MagicMock(returncode=0, stdout="Short")
            else:
                # Must be >1000 chars with heading to pass quality gate (Fix 3)
                return MagicMock(returncode=0, stdout="# Domain Analysis: test-domain\n\n" + "Detailed analysis content. " * 60)

        mock_subprocess.side_effect = side_effect

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1"],
        }

        analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=[], max_turns=50)

        # Verify subprocess was called twice
        assert mock_subprocess.call_count == 2

        # Verify output file was written with retry content
        domain_file = staging_dir / "test-domain.md"
        assert domain_file.exists()
        content = domain_file.read_text()
        assert "# Domain Analysis: test-domain" in content
        assert "Detailed analysis content" in content


class TestYamlFrontmatterStripping:
    """Test Fix 3: Strip Claude's YAML frontmatter from output."""

    def test_strips_yaml_frontmatter_block(self):
        """Test that _strip_meta_commentary strips YAML frontmatter block."""
        text = """---
title: Domain Analysis
author: Claude
---

# Actual Domain Analysis

Content here."""
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert not result.startswith("---")
        assert result.startswith("# Actual Domain Analysis")
        assert "title:" not in result
        assert "author:" not in result

    def test_strips_yaml_frontmatter_before_meta_commentary(self):
        """Test that YAML frontmatter is stripped before meta-commentary patterns."""
        text = """---
schema: 1.0
---

Based on my analysis, here are the findings:

# Domain Analysis

Content."""
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert not result.startswith("---")
        assert result.startswith("# Domain Analysis")
        assert "Based on my analysis" not in result

    def test_preserves_content_without_frontmatter(self):
        """Test that content without frontmatter is preserved."""
        text = "# Domain Analysis\n\nContent here."
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert result == text


class TestAdditionalMetaCommentaryPatterns:
    """Test Fix 4: Add more meta-commentary patterns."""

    def test_strips_i_have_gathered(self):
        text = "I have gathered sufficient evidence from the repositories.\n\n# Analysis\n\nContent."
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert result.startswith("# Analysis")
        assert "I have gathered" not in result

    def test_strips_now_i_can(self):
        text = "Now I can produce the comprehensive domain analysis.\n\n# Domain\n\nContent."
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert result.startswith("# Domain")
        assert "Now I can" not in result

    def test_strips_i_ll(self):
        text = "I'll compile the findings into the analysis.\n\n# Findings\n\nContent."
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert result.startswith("# Findings")
        assert "I'll" not in result

    def test_strips_i_will(self):
        text = "I will now produce the final analysis.\n\n# Final Analysis\n\nContent."
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert result.startswith("# Final Analysis")
        assert "I will" not in result

    def test_strips_numbered_list_pre_findings(self):
        """Test that numbered list items before headings are treated as meta-commentary."""
        text = """1. cidx-meta contains only markdown documentation files
2. The repository structure shows clear patterns
3. Multiple repos share common dependencies

# Domain Analysis

Actual analysis content here."""
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert result.startswith("# Domain Analysis")
        assert "cidx-meta contains" not in result
        assert "repository structure" not in result

    def test_strips_i_have_all_pattern(self):
        """Test that 'I have all' meta-commentary is stripped (Fix 3)."""
        text = "I have all the domain information from the staging directory. Now I'll generate the Domain Catalog.\n\n# Domain Catalog\n\nContent."
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert result.startswith("# Domain Catalog")
        assert "I have all" not in result


class TestInsufficientOutputThreshold:
    """Test Fix 1: Raise Pass 2 insufficient output threshold to 1000 chars."""

    @patch("subprocess.run")
    def test_pass_2_retries_on_insufficient_output_1000_chars(self, mock_subprocess, tmp_path):
        """Test that Pass 2 retries when output is <1000 chars (not just <50)."""
        # First call returns 626 chars (insufficient), second call returns full content
        call_count = [0]
        insufficient_output = "# Analysis\n\n" + "x" * 600  # 626 chars total

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return MagicMock(returncode=0, stdout=insufficient_output)
            else:
                return MagicMock(returncode=0, stdout="# Full Analysis\n\n" + "y" * 2000)

        mock_subprocess.side_effect = side_effect

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1"],
        }

        analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=[], max_turns=50)

        # Verify subprocess was called twice (original + retry)
        assert mock_subprocess.call_count == 2

        # Verify second call used max_turns=10
        second_call_args = mock_subprocess.call_args_list[1]
        cmd = second_call_args[0][0]
        assert "--max-turns" in cmd
        turns_idx = cmd.index("--max-turns")
        assert cmd[turns_idx + 1] == "10"

        # Verify output file was written with retry content
        domain_file = staging_dir / "test-domain.md"
        assert domain_file.exists()
        content = domain_file.read_text()
        assert "Full Analysis" in content
        assert "yyy" in content  # From the 2000-char retry output


class TestYamlStrippingWithoutOpeningDelimiter:
    """Test Fix 2: Strip YAML content without opening --- delimiter."""

    def test_strips_yaml_without_opening_delimiter(self):
        """Test that YAML-like content without opening --- is stripped."""
        text = """domain: some-domain
last_analyzed: 2026-02-14T23:00:00.000000+00:00
participating_repos:
  - repo1
  - repo2
---

# Domain Analysis

Real content here."""
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert result.startswith("# Domain Analysis")
        assert "domain:" not in result
        assert "last_analyzed:" not in result
        assert "participating_repos:" not in result
        assert "Real content here" in result

    def test_strips_yaml_with_schema_version_no_opening(self):
        """Test YAML stripping for schema_version without opening delimiter."""
        text = """schema_version: 1.0
last_analyzed: 2026-02-14
---

# Content

Analysis here."""
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert result.startswith("# Content")
        assert "schema_version:" not in result

    def test_preserves_proper_yaml_stripping(self):
        """Test that existing YAML with opening --- still works."""
        text = """---
domain: test
last_analyzed: 2026-01-01
---

# Analysis

Content."""
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert result.startswith("# Analysis")
        assert "domain:" not in result


class TestPass3MetaCommentaryStripping:
    """Test Fix 3: Add _strip_meta_commentary to Pass 3 output."""

    @patch("subprocess.run")
    def test_pass_3_strips_meta_commentary(self, mock_subprocess, tmp_path):
        """Test that Pass 3 strips meta-commentary from output."""
        # Return output with meta-commentary
        meta_output = "I have all the domain information from the staging directory. Now I'll generate the catalog.\n\n# Domain Catalog\n\nTable here."
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=meta_output,
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain_list = [{"name": "d1", "description": "Domain 1", "participating_repos": []}]
        repo_list = [{"alias": "r1", "description_summary": "Repo 1"}]

        analyzer.run_pass_3_index(staging_dir, domain_list, repo_list, max_turns=30)

        # Verify output file does NOT contain meta-commentary
        index_file = staging_dir / "_index.md"
        assert index_file.exists()
        content = index_file.read_text()

        # Should have frontmatter and content, but NOT meta-commentary
        assert content.startswith("---\n")
        assert "# Domain Catalog" in content
        assert "Table here" in content
        assert "I have all the domain information" not in content
        assert "Now I'll generate" not in content


class TestDuplicateYamlStripping:
    """Test Fix 1 (Iteration 9): Strip multiple consecutive YAML frontmatter blocks."""

    def test_strips_two_consecutive_yaml_blocks_with_opening_delimiter(self):
        """Test stripping two consecutive YAML blocks with opening --- delimiters."""
        text = """---
domain: langfuse-telemetry-data
last_analyzed: 2026-02-14T20:00:00.000000+00:00
participating_repos:
  - repo1
---

---
domain: langfuse-telemetry-data
last_analyzed: 2026-02-15T01:57:39.708781+00:00
participating_repos:
  - repo1
---

# Domain Analysis: langfuse-telemetry-data

Actual content here."""
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert result.startswith("# Domain Analysis")
        assert "domain:" not in result
        assert "last_analyzed:" not in result
        assert "participating_repos:" not in result
        assert "Actual content here" in result

    def test_strips_yaml_with_delimiter_then_yaml_without_delimiter(self):
        """Test stripping YAML with --- followed by YAML without opening ---."""
        text = """---
domain: test-domain
last_analyzed: 2026-02-14
---

domain: second-block
last_analyzed: 2026-02-15
---

# Analysis

Content here."""
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert result.startswith("# Analysis")
        assert "domain:" not in result
        assert "last_analyzed:" not in result
        assert "second-block" not in result
        assert "Content here" in result

    def test_strips_yaml_without_delimiter_then_yaml_with_delimiter(self):
        """Test stripping YAML without --- followed by YAML with opening ---."""
        text = """domain: first-block
last_analyzed: 2026-02-14
---

---
domain: second-block
participating_repos:
  - repo1
---

# Content

Analysis text."""
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert result.startswith("# Content")
        assert "domain:" not in result
        assert "first-block" not in result
        assert "second-block" not in result
        assert "Analysis text" in result

    def test_no_infinite_loop_on_non_yaml_content(self):
        """Test that regular content without YAML passes through unchanged."""
        text = "# Regular Content\n\nThis is just normal text with no YAML."
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert result == text


class TestPass2PromptGuardrails:
    """Test Fix 2 (Iteration 9): Prompt guardrails against YAML output and speculative content."""

    @patch("subprocess.run")
    def test_prompt_prohibits_yaml_frontmatter_output(self, mock_subprocess, tmp_path):
        """Test that Pass 2 prompt explicitly prohibits YAML frontmatter output."""
        content = "# Domain Analysis\n\n" + "X" * 1000
        mock_subprocess.return_value = MagicMock(returncode=0, stdout=content)

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1"],
        }

        analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=[], max_turns=60)

        # Verify prompt contains YAML prohibition
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args
        prompt = call_args[0][0][-1]  # Last element is the prompt

        assert "## PROHIBITED Content" in prompt
        assert "YAML frontmatter blocks (the system adds these automatically)" in prompt

    @patch("subprocess.run")
    def test_prompt_prohibits_speculative_content(self, mock_subprocess, tmp_path):
        """Test that Pass 2 prompt prohibits speculative/advisory content."""
        content = "# Domain Analysis\n\n" + "Y" * 1000
        mock_subprocess.return_value = MagicMock(returncode=0, stdout=content)

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1"],
        }

        analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=[], max_turns=60)

        # Verify prompt contains speculative content prohibition
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args
        prompt = call_args[0][0][-1]

        # Check for exact text in PROHIBITED section
        assert "## PROHIBITED Content" in prompt
        assert "Speculative sections" in prompt


class TestHeadingBasedMetaStripping:
    """Test Iteration 10 Fix 1: Heading-based meta-commentary stripping."""

    def test_strips_meta_commentary_before_heading(self):
        """Test that meta-commentary before first heading is stripped."""
        text = """I have enough data from the directory listings I already obtained.

# Domain Analysis: foo

Actual analysis content here."""
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert result.startswith("# Domain Analysis: foo")
        assert "I have enough data" not in result
        assert "Actual analysis content here" in result

    def test_strips_multiline_meta_before_heading(self):
        """Test that multiple paragraphs of meta-commentary before heading are stripped."""
        text = """Good - the code-indexer references to 'txt-db' are purely test fixture data.
This means there are no actual dependencies.

Now let me write the final analysis output.

## Overview

Domain content starts here."""
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert result.startswith("## Overview")
        assert "Good -" not in result
        assert "Now let me write" not in result
        assert "Domain content starts here" in result

    def test_preserves_content_starting_with_heading(self):
        """Test that content starting directly with heading is preserved unchanged."""
        text = """# Domain Analysis

This is the actual content with no preamble."""
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert result == text

    def test_meta_only_no_heading_returns_original(self):
        """Test that input with ONLY meta-commentary (no headings) returns original text."""
        text = """Based on my comprehensive analysis of the repositories, I have identified
the following patterns and dependencies across the codebase."""
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        # Should return original since we can't strip to nothing
        assert result == text

    def test_real_iter9_claude_ai_toolchain_pattern(self):
        """Test the actual Iteration 9 claude-ai-toolchain.md pattern - pure meta-commentary."""
        text = """The domain analysis is complete. All dependencies were verified against source code evidence.

Key findings:
- **Confirmed all 4 repos** belong in this domain with specific source file evidence
- **Verified technology stacks** from actual dependency manifests
- **Documented 8 intra-domain dependencies** with precise source file references"""
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        # No headings, so should return original (this will trigger quality gate failure)
        assert result == text

    def test_real_iter9_langfuse_pattern(self):
        """Test the actual Iteration 9 langfuse pattern - meta before heading."""
        text = """I have enough evidence from my searches to produce the comprehensive domain analysis.

# Domain Analysis: langfuse-telemetry-data

## Overview

Actual analysis content."""
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert result.startswith("# Domain Analysis: langfuse-telemetry-data")
        assert "I have enough evidence" not in result
        assert "Actual analysis content" in result

    def test_yaml_then_meta_then_heading(self):
        """Test full pipeline: YAML frontmatter + meta-commentary + heading content."""
        text = """---
domain: test-domain
last_analyzed: 2026-02-14
---

Based on my analysis, here are the findings.

# Domain Analysis

Real content here."""
        result = DependencyMapAnalyzer._strip_meta_commentary(text)
        assert result.startswith("# Domain Analysis")
        assert "domain:" not in result
        assert "Based on my analysis" not in result
        assert "Real content here" in result

    def test_heading_level_2_and_3_also_detected(self):
        """Test that heading levels 2 and 3 are also detected for stripping."""
        text1 = """Meta-commentary here.

## Section Heading

Content."""
        result1 = DependencyMapAnalyzer._strip_meta_commentary(text1)
        assert result1.startswith("## Section Heading")

        text2 = """Meta-commentary here.

### Subsection Heading

Content."""
        result2 = DependencyMapAnalyzer._strip_meta_commentary(text2)
        assert result2.startswith("### Subsection Heading")


class TestHasMarkdownHeadings:
    """Test _has_markdown_headings helper function."""

    def test_has_headings_true(self):
        """Test that text with # heading returns True."""
        text = "Some content\n\n# Heading\n\nMore content"
        result = DependencyMapAnalyzer._has_markdown_headings(text)
        assert result is True

    def test_has_headings_false(self):
        """Test that text without any headings returns False."""
        text = "Just regular text with no headings at all."
        result = DependencyMapAnalyzer._has_markdown_headings(text)
        assert result is False

    def test_has_headings_h2(self):
        """Test that text with ## heading returns True."""
        text = "Content\n\n## Section\n\nMore"
        result = DependencyMapAnalyzer._has_markdown_headings(text)
        assert result is True

    def test_has_headings_in_code_block_still_counts(self):
        """Test that heading inside code block is acceptable false positive."""
        text = """Some text.

```python
# This is a code comment, not a heading
print("hello")
```

More text."""
        result = DependencyMapAnalyzer._has_markdown_headings(text)
        # This will return True (false positive) - that's acceptable
        assert result is True


class TestIteration10QualityGate:
    """Test Iteration 10 Fix 2: Quality gate for missing headings in Pass 2."""

    @patch("subprocess.run")
    def test_quality_gate_no_headings_triggers_retry(self, mock_subprocess, tmp_path):
        """Test that run_pass_2_per_domain detects and retries when output has no headings."""
        # First call returns output WITHOUT any markdown headings (1.2KB but invalid)
        # Second call returns valid output with headings
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # 1.2KB of meta-commentary with NO headings (claude-ai-toolchain case)
                return MagicMock(
                    returncode=0,
                    stdout="The domain analysis is complete. All dependencies were verified.\n\n" + "X" * 1000,
                )
            else:
                # Valid output with heading
                return MagicMock(
                    returncode=0,
                    stdout="# Domain Analysis: test\n\nValid content here.\n\n" + "Y" * 1000,
                )

        mock_subprocess.side_effect = side_effect

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1"],
        }

        analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=[], max_turns=50)

        # Verify subprocess was called twice (original + retry due to missing headings)
        assert mock_subprocess.call_count == 2

        # Verify second call used max_turns=10 (reduced turns retry)
        second_call_args = mock_subprocess.call_args_list[1]
        cmd = second_call_args[0][0]
        assert "--max-turns" in cmd
        turns_idx = cmd.index("--max-turns")
        assert cmd[turns_idx + 1] == "10"

        # Verify output file was written with retry content (has heading)
        domain_file = staging_dir / "test-domain.md"
        assert domain_file.exists()
        content = domain_file.read_text()
        assert "# Domain Analysis: test" in content
        assert "Valid content here" in content


class TestIteration10PromptReinforcement:
    """Test Iteration 10 Fix 3: Prompt reinforcement for heading requirement."""

    @patch("subprocess.run")
    def test_pass2_prompt_contains_heading_requirement(self, mock_subprocess, tmp_path):
        """Test that Pass 2 prompt includes heading requirement instruction."""
        content = "# Domain Analysis\n\n" + "Z" * 1000
        mock_subprocess.return_value = MagicMock(returncode=0, stdout=content)

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1"],
        }

        analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=[], max_turns=60)

        # Verify prompt contains heading requirement
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args
        prompt = call_args[0][0][-1]  # Last element is the prompt

        assert "CRITICAL: Your output MUST begin with a markdown heading" in prompt
        assert "Do NOT start with summary text, meta-commentary, or a description of what you found" in prompt


class TestIteration11Fix2QualityCheckPrevious:
    """Test Iteration 11 Fix 2: Quality-check previous analysis before feeding it back."""

    @patch("subprocess.run")
    def test_skips_low_quality_previous_analysis(self, mock_subprocess, tmp_path):
        """Test that low-quality previous analysis (no headings or <1000 chars) is NOT fed into prompt."""
        content = "# Domain Analysis\n\n" + "Y" * 1000
        mock_subprocess.return_value = MagicMock(returncode=0, stdout=content)

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        # Create previous domain directory with LOW-QUALITY content (no headings, short)
        previous_dir = tmp_path / "previous"
        previous_dir.mkdir()
        previous_domain_file = previous_dir / "test-domain.md"
        # 485 bytes of meta-commentary with no headings (iteration 10 garbage)
        low_quality_content = "---\nOld frontmatter\n---\n\nPlease approve my write to test-domain.md. " + "X" * 400
        previous_domain_file.write_text(low_quality_content)

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1"],
        }

        analyzer.run_pass_2_per_domain(
            staging_dir, domain, [domain], repo_list=[], max_turns=60, previous_domain_dir=previous_dir
        )

        # Verify subprocess was called with prompt that does NOT include previous content
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args
        prompt = call_args[0][0][-1]  # Last element is the prompt

        # Should NOT contain "Previous Analysis" section
        assert "Previous Analysis (refine and improve)" not in prompt
        assert "Please approve my write" not in prompt

    @patch("subprocess.run")
    def test_includes_high_quality_previous_analysis(self, mock_subprocess, tmp_path):
        """Test that high-quality previous analysis (has headings AND >1000 chars) IS fed into prompt."""
        content = "# Updated Analysis\n\n" + "Z" * 1000
        mock_subprocess.return_value = MagicMock(returncode=0, stdout=content)

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        # Create previous domain directory with HIGH-QUALITY content (has headings, >1000 chars)
        previous_dir = tmp_path / "previous"
        previous_dir.mkdir()
        previous_domain_file = previous_dir / "test-domain.md"
        high_quality_content = "---\nOld frontmatter\n---\n\n# Previous Analysis\n\nGood quality content here. " + "Y" * 1000
        previous_domain_file.write_text(high_quality_content)

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1"],
        }

        analyzer.run_pass_2_per_domain(
            staging_dir, domain, [domain], repo_list=[], max_turns=60, previous_domain_dir=previous_dir
        )

        # Verify subprocess was called with prompt that DOES include previous content
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args
        prompt = call_args[0][0][-1]  # Last element is the prompt

        # Should contain "Previous Analysis" section
        assert "Previous Analysis (refine and improve)" in prompt
        assert "Good quality content here" in prompt


class TestIteration12Fix3TrailingMetaCommentary:
    """Test Iteration 12 Fix 3: Strip trailing meta-commentary patterns."""

    def test_strips_multiple_trailing_meta_patterns(self):
        """Test that trailing conversational patterns like 'Please let me know...' are stripped."""
        analyzer = DependencyMapAnalyzer(
            golden_repos_root=MagicMock(),
            cidx_meta_path=MagicMock(),
            pass_timeout=600,
        )

        # Content with valid analysis followed by conversational ending
        content = """# Domain Analysis

Valid analysis content here.

## Dependencies

More valid content.

Please let me know if you need changes."""

        result = analyzer._strip_meta_commentary(content)

        # Should strip the trailing conversational line
        assert "Please let me know" not in result
        assert "# Domain Analysis" in result
        assert "Valid analysis content" in result
        assert "## Dependencies" in result

    def test_strips_trailing_separator_and_meta(self):
        """Test that trailing --- separator followed by meta-commentary is stripped."""
        analyzer = DependencyMapAnalyzer(
            golden_repos_root=MagicMock(),
            cidx_meta_path=MagicMock(),
            pass_timeout=600,
        )

        # Content with valid analysis followed by separator and conversational text
        content = """# Domain Analysis

Valid analysis content here.

## Dependencies

More valid content.

---

Let me know if you'd like me to expand on anything."""

        result = analyzer._strip_meta_commentary(content)

        # Should strip both the --- separator and the trailing conversational line
        assert "---" not in result
        assert "Let me know" not in result
        assert "# Domain Analysis" in result
        assert "Valid analysis content" in result
        assert result.strip().endswith("More valid content.")

    def test_strips_would_you_like_pattern(self):
        """Regression test: 'Would you like...' was the exact iter 11 _index.md leakage."""
        analyzer = DependencyMapAnalyzer(
            golden_repos_root=MagicMock(),
            cidx_meta_path=MagicMock(),
            pass_timeout=600,
        )

        text = (
            "# Domain Analysis\n\n"
            "Good content here.\n\n"
            "Would you like to approve the file write permission so I can save this to `_index.md`?"
        )
        result = analyzer._strip_meta_commentary(text)
        assert "Would you like" not in result
        assert "Good content here." in result


class TestIteration11Fix3SkipGarbageWrite:
    """Test Iteration 11 Fix 3: Skip writing file when both attempts produce garbage."""

    @patch("subprocess.run")
    def test_skips_file_write_when_both_attempts_fail(self, mock_subprocess, tmp_path):
        """Test that domain file is NOT written when both attempts return garbage (no headings)."""
        # Both calls return garbage (no headings)
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First attempt: 1232 bytes but no headings (claude-ai-toolchain case)
                return MagicMock(returncode=0, stdout="Meta-commentary about analysis. " + "X" * 1200)
            else:
                # Retry: 485 bytes, no headings (please approve my write case)
                return MagicMock(returncode=0, stdout="Please approve my write to the file. " + "Y" * 400)

        mock_subprocess.side_effect = side_effect

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1"],
        }

        analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=[], max_turns=50)

        # Verify subprocess was called twice
        assert mock_subprocess.call_count == 2

        # Verify domain file was NOT written (FIX 3)
        domain_file = staging_dir / "test-domain.md"
        assert not domain_file.exists(), "Domain file should NOT be written when both attempts fail quality checks"

    @patch("subprocess.run")
    def test_writes_file_when_retry_succeeds(self, mock_subprocess, tmp_path):
        """Test that domain file IS written when retry succeeds (has headings, >1000 chars)."""
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First attempt: fails (no headings)
                return MagicMock(returncode=0, stdout="Meta-commentary. " + "X" * 100)
            else:
                # Retry: succeeds (has heading, >1000 chars)
                return MagicMock(returncode=0, stdout="# Domain Analysis\n\nValid content. " + "Y" * 1000)

        mock_subprocess.side_effect = side_effect

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1"],
        }

        analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=[], max_turns=50)

        # Verify domain file WAS written with retry content
        domain_file = staging_dir / "test-domain.md"
        assert domain_file.exists()
        content = domain_file.read_text()
        assert "# Domain Analysis" in content
        assert "Valid content" in content

    @patch("subprocess.run")
    def test_short_output_still_retries(self, mock_subprocess, tmp_path):
        """Short non-max-turns output should still trigger retry (existing behavior preserved)."""
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return MagicMock(returncode=0, stdout="Short output without headings.")
            else:
                return MagicMock(returncode=0, stdout="# Domain Analysis\n\n" + "X" * 1000)

        mock_subprocess.side_effect = side_effect

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1"],
        }

        analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=[], max_turns=50)

        # Should be called TWICE (retry for short output that is NOT max-turns exhaustion)
        assert mock_subprocess.call_count == 2


class TestIteration13HookThresholdFix:
    """Test hook threshold calculation fixes (Iteration 13)."""

    @patch("subprocess.run")
    def test_hook_thresholds_fixed_default(self, mock_subprocess, tmp_path):
        """Verify early=max(5, int(50*0.3))=15 and late=max(10, int(50*0.6))=30 for max_turns=50."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout="# Domain Analysis\n\nContent. " + "X" * 1000,
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1"],
        }

        analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=[], max_turns=50)

        # Verify subprocess was called with correct thresholds in --settings JSON
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args[0][0]

        # Find --settings argument
        settings_idx = call_args.index("--settings")
        settings_json = call_args[settings_idx + 1]
        settings = json.loads(settings_json)

        # Extract bash script that contains threshold checks
        bash_script = settings["hooks"]["PostToolUse"][0]["command"]

        # Verify thresholds: early=15, late=30
        assert "[ \"$C\" -gt 30 ]" in bash_script, "Late threshold should be 30"
        assert "[ \"$C\" -gt 15 ]" in bash_script, "Early threshold should be 15"

    @patch("subprocess.run")
    def test_hook_thresholds_custom_override(self, mock_subprocess, tmp_path):
        """Verify hook_thresholds=(7,17) overrides default calculation."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout="# Domain Analysis\n\nContent. " + "X" * 1000,
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1", "repo2", "repo3", "repo4", "repo5"],  # Large domain
        }

        analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=[], max_turns=50)

        # Verify subprocess was called with custom thresholds
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args[0][0]

        settings_idx = call_args.index("--settings")
        settings_json = call_args[settings_idx + 1]
        settings = json.loads(settings_json)

        bash_script = settings["hooks"]["PostToolUse"][0]["command"]

        # For large domain (5 repos), thresholds should be (7, 17) not (15, 30)
        assert "[ \"$C\" -gt 17 ]" in bash_script, "Late threshold should be 17 for large domain"
        assert "[ \"$C\" -gt 7 ]" in bash_script, "Early threshold should be 7 for large domain"

    @patch("subprocess.run")
    def test_hook_thresholds_small_max_turns(self, mock_subprocess, tmp_path):
        """Verify max_turns=10 gives early=max(5,3)=5 and late=max(10,6)=10."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout="# Domain Analysis\n\nContent. " + "X" * 1000,
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1"],
        }

        analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=[], max_turns=10)

        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args[0][0]

        settings_idx = call_args.index("--settings")
        settings_json = call_args[settings_idx + 1]
        settings = json.loads(settings_json)

        bash_script = settings["hooks"]["PostToolUse"][0]["command"]

        # early = max(5, int(10*0.3)) = max(5, 3) = 5
        # late = max(10, int(10*0.6)) = max(10, 6) = 10
        assert "[ \"$C\" -gt 10 ]" in bash_script, "Late threshold should be 10"
        assert "[ \"$C\" -gt 5 ]" in bash_script, "Early threshold should be 5"


class TestIteration13LargeDomainDetection:
    """Test large domain detection and output-first prompt selection (Iteration 13)."""

    @patch("subprocess.run")
    def test_large_domain_uses_output_first_prompt(self, mock_subprocess, tmp_path):
        """With 4+ repos, verify prompt starts with WRITE YOUR ANALYSIS FIRST."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout="# Domain Analysis: test-domain\n\nContent. " + "X" * 1000,
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1", "repo2", "repo3", "repo4"],  # 4 repos = large
        }

        analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=[], max_turns=50)

        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args[0][0]
        prompt = call_args[-1]  # Last element is the prompt

        # Verify output-first prompt characteristics
        assert "WRITE YOUR ANALYSIS FIRST" in prompt
        assert "Source Code Exploration Mandate" not in prompt
        assert "Required Searches" not in prompt
        assert "run at least one search" not in prompt.lower()
        assert "AT MOST 5" in prompt  # Limited searches
        assert "OPTIONAL" in prompt  # Searches are optional

    @patch("subprocess.run")
    def test_small_domain_uses_standard_prompt(self, mock_subprocess, tmp_path):
        """With 3 or fewer repos, verify prompt DOES contain Source Code Exploration Mandate."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout="# Domain Analysis\n\nContent. " + "X" * 1000,
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1", "repo2", "repo3"],  # 3 repos = small
        }

        analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=[], max_turns=50)

        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args[0][0]
        prompt = call_args[-1]

        # Verify standard prompt characteristics (existing behavior)
        assert "Source Code Exploration Mandate" in prompt
        assert "Required Searches" in prompt

    @patch("subprocess.run")
    def test_large_domain_earlier_hook_thresholds(self, mock_subprocess, tmp_path):
        """Verify 5-repo domain with max_turns=50 uses hook thresholds (7,17) not default (15,30)."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout="# Domain Analysis\n\nContent. " + "X" * 1000,
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1", "repo2", "repo3", "repo4", "repo5"],
        }

        analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=[], max_turns=50)

        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args[0][0]

        settings_idx = call_args.index("--settings")
        settings_json = call_args[settings_idx + 1]
        settings = json.loads(settings_json)

        bash_script = settings["hooks"]["PostToolUse"][0]["command"]

        # Large domain should use earlier thresholds: (7, 17)
        assert "[ \"$C\" -gt 17 ]" in bash_script
        assert "[ \"$C\" -gt 7 ]" in bash_script


class TestIteration13LargeDomainRetry:
    """Test large domain max-turns retry uses write-only mode (Iteration 13)."""

    @patch("subprocess.run")
    def test_large_domain_max_turns_retry_uses_write_only(self, mock_subprocess, tmp_path):
        """Verify max-turns exhaustion for large domain retries with max_turns=8 and no MCP tools."""
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First attempt: max-turns exhaustion
                return MagicMock(returncode=0, stdout="Error: Reached max turns (50)")
            else:
                # Retry: succeeds
                return MagicMock(returncode=0, stdout="# Domain Analysis\n\nContent. " + "X" * 1000)

        mock_subprocess.side_effect = side_effect

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1", "repo2", "repo3", "repo4"],  # 4 repos = large
        }

        analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=[], max_turns=50)

        # Should be called twice
        assert mock_subprocess.call_count == 2

        # Verify retry call (second call) has max_turns=8 and empty allowed_tools
        retry_call_args = mock_subprocess.call_args_list[1][0][0]

        # Check max_turns=8
        max_turns_idx = retry_call_args.index("--max-turns")
        assert retry_call_args[max_turns_idx + 1] == "8"

        # When allowed_tools="" (empty string), --allowedTools is present with empty value
        assert "--allowedTools" in retry_call_args
        allowed_tools_idx = retry_call_args.index("--allowedTools")
        assert retry_call_args[allowed_tools_idx + 1] == ""

    @patch("subprocess.run")
    def test_small_domain_max_turns_retry_uses_budget_search(self, mock_subprocess, tmp_path):
        """Verify max-turns exhaustion for small domain uses max_turns=15 with search tools (existing behavior)."""
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return MagicMock(returncode=0, stdout="Error: Reached max turns (50)")
            else:
                return MagicMock(returncode=0, stdout="# Domain Analysis\n\nContent. " + "X" * 1000)

        mock_subprocess.side_effect = side_effect

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1", "repo2"],  # 2 repos = small
        }

        analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=[], max_turns=50)

        assert mock_subprocess.call_count == 2

        # Verify retry call has max_turns=15 and search tools allowed
        retry_call_args = mock_subprocess.call_args_list[1][0][0]

        max_turns_idx = retry_call_args.index("--max-turns")
        assert retry_call_args[max_turns_idx + 1] == "15"

        # Should have --allowedTools with search_code
        assert "--allowedTools" in retry_call_args
        allowed_tools_idx = retry_call_args.index("--allowedTools")
        assert "search_code" in retry_call_args[allowed_tools_idx + 1]


class TestIteration13OutputFirstPrompt:
    """Test _build_output_first_prompt method (Iteration 13)."""

    def test_output_first_prompt_has_template_sections(self, tmp_path):
        """Verify _build_output_first_prompt output contains all required template sections."""
        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        domain = {
            "name": "test-domain",
            "description": "Test domain description",
            "participating_repos": ["repo1", "repo2"],
            "evidence": "Some evidence from Pass 1",
        }

        domain_list = [domain]
        repo_list = [
            {"alias": "repo1", "clone_path": "/path/to/repo1"},
            {"alias": "repo2", "clone_path": "/path/to/repo2"},
        ]

        prompt = analyzer._build_output_first_prompt(domain, domain_list, repo_list, None)

        # Verify template sections present
        assert "## Overview" in prompt
        assert "## Repository Roles" in prompt
        assert "## Intra-Domain Dependencies" in prompt
        assert "## Cross-Domain Connections" in prompt

    def test_output_first_prompt_limits_search_calls(self, tmp_path):
        """Verify prompt contains 'AT MOST 5' and 'OPTIONAL'."""
        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1"],
        }

        prompt = analyzer._build_output_first_prompt(domain, [domain], [], None)

        assert "AT MOST 5" in prompt
        assert "OPTIONAL" in prompt

    def test_output_first_prompt_includes_pass1_evidence(self, tmp_path):
        """Verify prompt includes evidence from domain dict."""
        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1"],
            "evidence": "Critical evidence from Pass 1 analysis",
        }

        prompt = analyzer._build_output_first_prompt(domain, [domain], [], None)

        assert "Critical evidence from Pass 1 analysis" in prompt
        assert "Pass 1 Evidence (PRIMARY SOURCE)" in prompt

    def test_output_first_prompt_skips_low_quality_previous(self, tmp_path):
        """Verify low-quality previous analysis is NOT included."""
        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1"],
        }

        # Create previous domain directory with low-quality content (short, no headings)
        previous_dir = tmp_path / "previous"
        previous_dir.mkdir()
        previous_file = previous_dir / "test-domain.md"
        previous_file.write_text("Short low quality content")

        prompt = analyzer._build_output_first_prompt(domain, [domain], [], previous_dir)

        # Should NOT include previous analysis section
        assert "Previous Analysis" not in prompt
        assert "Short low quality content" not in prompt

    def test_output_first_prompt_includes_high_quality_previous_with_improvement_mandate(self, tmp_path):
        """Verify high-quality previous analysis includes EXTEND/IMPROVE/CORRECT instructions."""
        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1"],
        }

        # Create previous domain directory with high-quality content
        previous_dir = tmp_path / "previous"
        previous_dir.mkdir()
        previous_file = previous_dir / "test-domain.md"
        previous_file.write_text("# Previous Analysis\n\nGood quality content here. " + "Y" * 1000)

        prompt = analyzer._build_output_first_prompt(domain, [domain], [], previous_dir)

        # Should include previous analysis with improvement mandate
        assert "EXTEND, IMPROVE, and CORRECT" in prompt
        assert "Preserve" in prompt
        assert "Correct" in prompt
        assert "Extend" in prompt
        assert "Do NOT start from scratch" in prompt
        assert "Good quality content here" in prompt


class TestIteration14PurposeDrivenHooks:
    """Test Iteration 14: Purpose-driven hook reminders and retry fixes."""

    def test_hook_reminder_contains_purpose(self, tmp_path):
        """Test that hook_reminder includes purpose-driven language about inter-repo navigation and conciseness."""
        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1"],
        }

        # We need to extract hook_reminder from run_pass_2_per_domain
        # The hook_reminder is built inside the method, so we'll mock _invoke_claude_cli
        # to capture it
        with patch.object(analyzer, "_invoke_claude_cli") as mock_invoke:
            mock_invoke.return_value = "# Domain Analysis\n\nContent. " + "X" * 1000

            try:
                analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=[], max_turns=50)
            except Exception:
                pass  # We just want to capture the call

            # Extract post_tool_hook from the call
            assert mock_invoke.called
            call_kwargs = mock_invoke.call_args[1]
            hook_reminder = call_kwargs.get("post_tool_hook", "")

            # Verify purpose-driven language
            assert "inter-repository navigation" in hook_reminder or "inter-repo" in hook_reminder
            assert "concise" in hook_reminder.lower()
            assert "# Domain Analysis" in hook_reminder

    def test_threshold_messages_contain_purpose(self, tmp_path):
        """Test that CRITICAL and WARNING threshold messages mention conciseness, not 'complete analysis'."""
        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        # Test _invoke_claude_cli with hook_thresholds to check generated messages
        with patch("subprocess.run") as mock_subprocess:
            # Mock sufficient output to avoid "very short stdout" warning
            mock_subprocess.return_value = MagicMock(returncode=0, stdout="# Test\n\nOutput. " + "X" * 1000)

            # Call with hook thresholds (simulating large domain)
            result = analyzer._invoke_claude_cli(
                prompt="test",
                timeout=300,
                max_turns=50,
                allowed_tools="mcp__cidx-local__search_code",
                post_tool_hook="TEST HOOK",
                hook_thresholds=(3, 8)
            )

            # Extract the --settings JSON from subprocess call
            call_args = mock_subprocess.call_args[0][0]
            assert "--settings" in call_args
            settings_idx = call_args.index("--settings")
            settings_json = call_args[settings_idx + 1]
            settings = json.loads(settings_json)

            # Extract bash script from PostToolUse hook
            bash_command = settings["hooks"]["PostToolUse"][0]["command"]
            assert "bash -c" in bash_command
            # Extract the quoted bash script
            bash_script = bash_command.split("bash -c ", 1)[1].strip("'\"")

            # Verify CRITICAL message mentions conciseness and NOT "complete analysis"
            assert "CRITICAL" in bash_script
            assert "concise" in bash_script.lower()
            assert "complete analysis" not in bash_script

            # Verify WARNING message mentions conciseness
            assert "WARNING" in bash_script
            # The WARNING message should also mention conciseness

    def test_standard_prompt_conciseness_guidelines(self, tmp_path):
        """Test that standard prompt (small domains <=3 repos) includes Content Guidelines with conciseness constraints."""
        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        # Small domain with 2 repos (<=3 triggers standard prompt)
        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1", "repo2"],
        }

        with patch.object(analyzer, "_invoke_claude_cli") as mock_invoke:
            mock_invoke.return_value = "# Domain Analysis\n\nContent. " + "X" * 1000

            try:
                analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=[], max_turns=50)
            except Exception:
                pass

            # Extract prompt from the call
            assert mock_invoke.called
            call_args = mock_invoke.call_args[0]
            prompt = call_args[0]

            # Verify Content Guidelines section exists
            assert "## Content Guidelines" in prompt
            assert "CONCISE" in prompt or "concise" in prompt
            assert "inter-repository navigation" in prompt or "inter-repo" in prompt
            assert "no code snippets" in prompt.lower() or "not full code snippets" in prompt.lower()
            assert "3-8 sentences" in prompt or "shorter is better" in prompt.lower()

    @patch("subprocess.run")
    def test_insufficient_output_retry_is_write_only(self, mock_subprocess, tmp_path):
        """Test that insufficient-output retry uses allowed_tools='' (write-only) and includes write-focused prompt."""
        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1"],
        }

        # First call returns insufficient output (no headings, <1000 chars)
        # Second call (retry) should be write-only
        mock_subprocess.side_effect = [
            MagicMock(returncode=0, stdout="Short output without headings"),
            MagicMock(returncode=0, stdout="# Domain Analysis\n\nRetry content. " + "Y" * 1000)
        ]

        analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=[], max_turns=50)

        # Should have been called twice (primary + retry)
        assert mock_subprocess.call_count == 2

        # Check retry call (second call)
        retry_call = mock_subprocess.call_args_list[1]
        retry_cmd = retry_call[0][0]

        # Verify allowed_tools="" (write-only mode)
        if "--allowedTools" in retry_cmd:
            tools_idx = retry_cmd.index("--allowedTools")
            assert retry_cmd[tools_idx + 1] == "", "Retry should use allowed_tools='' (write-only)"

        # Verify retry prompt includes write-focused language
        retry_prompt = retry_cmd[-1]
        assert "Write your dependency analysis NOW" in retry_prompt
        assert "NO searching" in retry_prompt or "without searching" in retry_prompt.lower()


class TestIteration15InsideOutAndConciseness:
    """Test Iteration 15: Inside-out mapping with repo sizes, conciseness template, and journal resumability."""

    @patch("subprocess.run")
    def test_repo_sizes_in_pass1_prompt(self, mock_subprocess, tmp_path):
        """Test that Pass 1 prompt includes file_count and MB size for each repo."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([
                {
                    "name": "test-domain",
                    "description": "Test",
                    "participating_repos": ["repo1"],
                    "repo_paths": {"repo1": "/path/to/repo1"},
                }
            ]),
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        repo_list = [
            {
                "alias": "repo1",
                "description_summary": "Repo 1",
                "clone_path": "/path/to/repo1",
                "file_count": 150,
                "total_bytes": 5242880,  # 5 MB
            },
        ]

        analyzer.run_pass_1_synthesis(staging_dir, {}, repo_list=repo_list, max_turns=50)

        # Extract prompt from subprocess call
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args[0][0]
        prompt = call_args[-1]

        # Verify file count and MB size in prompt
        assert "150 files" in prompt
        assert "5.0 MB" in prompt or "5 MB" in prompt

    @patch("subprocess.run")
    def test_pass2_inside_out_instruction_present(self, mock_subprocess, tmp_path):
        """Test that Pass 2 prompt includes INSIDE-OUT ANALYSIS STRATEGY section."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout="# Domain Analysis\n\nContent. " + "X" * 1000,
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1", "repo2"],
        }

        repo_list = [
            {"alias": "repo1", "clone_path": "/path/to/repo1", "total_bytes": 10000000},
            {"alias": "repo2", "clone_path": "/path/to/repo2", "total_bytes": 5000000},
        ]

        analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=repo_list, max_turns=50)

        # Extract prompt
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args[0][0]
        prompt = call_args[-1]

        # Verify INSIDE-OUT section present
        assert "INSIDE-OUT ANALYSIS STRATEGY" in prompt
        assert "largest repository" in prompt

    @patch("subprocess.run")
    def test_participating_repos_sorted_by_size(self, mock_subprocess, tmp_path):
        """Test that participating repos are sorted by size (largest first) in Pass 2 prompt."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout="# Domain Analysis\n\nContent. " + "X" * 1000,
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["small-repo", "large-repo", "medium-repo"],
        }

        repo_list = [
            {"alias": "small-repo", "clone_path": "/path/small", "total_bytes": 1000000},
            {"alias": "large-repo", "clone_path": "/path/large", "total_bytes": 10000000},
            {"alias": "medium-repo", "clone_path": "/path/medium", "total_bytes": 5000000},
        ]

        analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=repo_list, max_turns=50)

        # Extract prompt
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args[0][0]
        prompt = call_args[-1]

        # Extract the "Repository Filesystem Locations" section where repos should be sorted
        repo_section_start = prompt.find("## Repository Filesystem Locations")
        assert repo_section_start >= 0, "Repository Filesystem Locations section not found"
        next_section_start = prompt.find("##", repo_section_start + 10)
        if next_section_start >= 0:
            repo_section = prompt[repo_section_start:next_section_start]
        else:
            repo_section = prompt[repo_section_start:]

        # Find the order repos appear in the Repository Filesystem Locations section
        large_idx = repo_section.find("large-repo")
        medium_idx = repo_section.find("medium-repo")
        small_idx = repo_section.find("small-repo")

        # Verify repos appear in size-descending order in the Repository Filesystem Locations section
        assert large_idx < medium_idx < small_idx, (
            f"Repos not sorted by size in Repository Filesystem Locations section: large@{large_idx}, medium@{medium_idx}, small@{small_idx}"
        )

    @patch("subprocess.run")
    def test_standard_prompt_has_output_template(self, mock_subprocess, tmp_path):
        """Test that standard prompt (<=3 repos) includes OUTPUT TEMPLATE section with headings."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout="# Domain Analysis\n\nContent. " + "X" * 1000,
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        # Small domain (3 repos) should use standard prompt
        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1", "repo2", "repo3"],
        }

        analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=[], max_turns=50)

        # Extract prompt
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args[0][0]
        prompt = call_args[-1]

        # Verify OUTPUT TEMPLATE section with required headings
        assert "OUTPUT TEMPLATE" in prompt
        assert "## Overview" in prompt
        assert "## Repository Roles" in prompt
        assert "## Intra-Domain Dependencies" in prompt
        assert "## Cross-Domain Connections" in prompt

    @patch("subprocess.run")
    def test_standard_prompt_has_output_budget(self, mock_subprocess, tmp_path):
        """Test that standard prompt includes Output Budget section with 3,000-10,000 character limit."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout="# Domain Analysis\n\nContent. " + "X" * 1000,
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1"],
        }

        analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=[], max_turns=50)

        # Extract prompt
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args[0][0]
        prompt = call_args[-1]

        # Verify Output Budget section
        assert "Output Budget" in prompt
        assert "3,000" in prompt or "3000" in prompt
        assert "10,000" in prompt or "10000" in prompt

    @patch("subprocess.run")
    def test_prohibited_content_includes_search_audit(self, mock_subprocess, tmp_path):
        """Test that PROHIBITED Content section explicitly forbids 'MCP Searches Performed' sections."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout="# Domain Analysis\n\nContent. " + "X" * 1000,
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1"],
        }

        analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=[], max_turns=50)

        # Extract prompt
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args[0][0]
        prompt = call_args[-1]

        # Verify PROHIBITED section mentions MCP Searches
        assert "PROHIBITED" in prompt
        assert "MCP Searches Performed" in prompt or "search audit trail" in prompt

    @patch("subprocess.run")
    def test_hook_reminder_includes_budget(self, mock_subprocess, tmp_path):
        """Test that hook_reminder includes character budget guidance (3,000-10,000 chars)."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout="# Domain Analysis\n\nContent. " + "X" * 1000,
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1"],
        }

        # We need to capture the hook reminder from the settings JSON
        analyzer.run_pass_2_per_domain(staging_dir, domain, [domain], repo_list=[], max_turns=50)

        # Extract settings from subprocess call
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args[0][0]

        # Find --settings argument
        if "--settings" in call_args:
            settings_idx = call_args.index("--settings")
            settings_json = call_args[settings_idx + 1]
            settings = json.loads(settings_json)

            # Extract bash script from PostToolUse hook
            bash_script = settings["hooks"]["PostToolUse"][0]["command"]

            # Verify character budget mentioned in hook messages
            assert "3,000" in bash_script or "3000" in bash_script or "10,000" in bash_script or "10000" in bash_script


class TestIteration16CrossDomainGraph:
    """Test Iteration 16: Cross-domain dependency graph in Pass 3."""

    def test_extract_cross_domain_section_basic(self):
        """Test standard heading extraction from domain file."""
        content = """# Domain Analysis: test-domain

## Overview
Domain overview here.

## Cross-Domain Connections

This domain connects to **other-domain** via repo1 and repo2.

## Dependencies
More content."""

        result = DependencyMapAnalyzer._extract_cross_domain_section(content)

        assert "This domain connects to" in result
        assert "repo1 and repo2" in result
        assert "## Dependencies" not in result  # Should stop at next heading
        assert "## Overview" not in result

    def test_extract_cross_domain_section_missing(self):
        """Test returns empty string when no Cross-Domain heading."""
        content = """# Domain Analysis: test-domain

## Overview
No cross-domain section here.

## Dependencies
Some deps."""

        result = DependencyMapAnalyzer._extract_cross_domain_section(content)
        assert result == ""

    def test_extract_cross_domain_section_last_section(self):
        """Test section extraction when Cross-Domain is the last section (no following ##)."""
        content = """# Domain Analysis: test-domain

## Overview
Overview content.

## Cross-Domain Connections

This is the last section with cross-domain info.
No more headings after this."""

        result = DependencyMapAnalyzer._extract_cross_domain_section(content)

        assert "This is the last section" in result
        assert "No more headings after this" in result

    def test_extract_cross_domain_section_variant_heading(self):
        """Test works with 'Cross-Domain' heading without 'Connections'."""
        content = """# Domain Analysis: test-domain

## Overview
Overview.

## Cross-Domain

Content with variant heading.

## Other Section
Should not be included."""

        result = DependencyMapAnalyzer._extract_cross_domain_section(content)

        assert "Content with variant heading" in result
        assert "Should not be included" not in result

    def test_build_cross_domain_graph_detects_edges(self, tmp_path):
        """Test that edges are detected from structured Outgoing Dependencies tables."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        # Create domain files with structured cross-domain tables
        domain_a = staging_dir / "domain-a.md"
        domain_a.write_text(
            "---\ndomain: domain-a\n---\n\n"
            "# Domain Analysis: domain-a\n\n"
            "## Cross-Domain Connections\n\n"
            "### Outgoing Dependencies\n\n"
            "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
            "| repo-a-one | repo-b-alpha | domain-b | Service integration | A calls B API | client.py |\n"
        )

        domain_b = staging_dir / "domain-b.md"
        domain_b.write_text(
            "---\ndomain: domain-b\n---\n\n"
            "# Domain Analysis: domain-b\n\n"
            "## Cross-Domain Connections\n\n"
            "No verified cross-domain dependencies.\n"
        )

        domain_list = [
            {"name": "domain-a", "participating_repos": ["repo-a-one"]},
            {"name": "domain-b", "participating_repos": ["repo-b-alpha", "repo-b-beta"]},
        ]

        graph_section = DependencyMapAnalyzer._build_cross_domain_graph(staging_dir, domain_list)

        assert "## Cross-Domain Dependency Graph" in graph_section
        assert "domain-a" in graph_section
        assert "domain-b" in graph_section
        assert "repo-a-one" in graph_section

    def test_build_cross_domain_graph_no_self_edges(self, tmp_path):
        """Test that mentioning own repos does not create self-edges."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain_a = staging_dir / "domain-a.md"
        domain_a.write_text("""---
domain: domain-a
---

# Domain Analysis: domain-a

## Cross-Domain Connections

Within domain-a, **repo-a-one** calls **repo-a-two**.
No external connections.
""")

        domain_list = [
            {"name": "domain-a", "participating_repos": ["repo-a-one", "repo-a-two"]},
        ]

        graph_section = DependencyMapAnalyzer._build_cross_domain_graph(staging_dir, domain_list)

        # Should return empty string (no cross-domain edges)
        assert graph_section == ""

    def test_build_cross_domain_graph_empty_for_standalone(self, tmp_path):
        """Test no output for isolated domains with no cross-domain connections."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain_a = staging_dir / "domain-a.md"
        domain_a.write_text("""---
domain: domain-a
---

# Domain Analysis: domain-a

## Overview
Standalone domain.
""")

        domain_b = staging_dir / "domain-b.md"
        domain_b.write_text("""---
domain: domain-b
---

# Domain Analysis: domain-b

## Overview
Another standalone domain.
""")

        domain_list = [
            {"name": "domain-a", "participating_repos": ["repo-a"]},
            {"name": "domain-b", "participating_repos": ["repo-b"]},
        ]

        graph_section = DependencyMapAnalyzer._build_cross_domain_graph(staging_dir, domain_list)

        # No edges, should return empty string
        assert graph_section == ""

    def test_build_cross_domain_graph_word_boundary(self, tmp_path):
        """Test that short aliases like 'db' don't match 'adobe'."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain_a = staging_dir / "domain-a.md"
        domain_a.write_text("""---
domain: domain-a
---

# Domain Analysis: domain-a

## Cross-Domain Connections

We use adobe for graphics processing.
""")

        domain_list = [
            {"name": "domain-a", "participating_repos": ["repo-a"]},
            {"name": "domain-b", "participating_repos": ["db"]},
        ]

        graph_section = DependencyMapAnalyzer._build_cross_domain_graph(staging_dir, domain_list)

        # "db" should NOT match "adobe", so no edges
        assert graph_section == ""

    def test_build_cross_domain_graph_bidirectional(self, tmp_path):
        """Test that A→B and B→A edges are both detected from structured tables."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain_a = staging_dir / "domain-a.md"
        domain_a.write_text(
            "---\ndomain: domain-a\n---\n\n"
            "# Domain Analysis: domain-a\n\n"
            "## Cross-Domain Connections\n\n"
            "### Outgoing Dependencies\n\n"
            "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
            "| repo-a | repo-b | domain-b | Code-level | A uses B | import.py |\n"
        )

        domain_b = staging_dir / "domain-b.md"
        domain_b.write_text(
            "---\ndomain: domain-b\n---\n\n"
            "# Domain Analysis: domain-b\n\n"
            "## Cross-Domain Connections\n\n"
            "### Outgoing Dependencies\n\n"
            "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
            "| repo-b | repo-a | domain-a | Service integration | B calls A API | client.py |\n"
        )

        domain_list = [
            {"name": "domain-a", "participating_repos": ["repo-a"]},
            {"name": "domain-b", "participating_repos": ["repo-b"]},
        ]

        graph_section = DependencyMapAnalyzer._build_cross_domain_graph(staging_dir, domain_list)

        # Should have both edges
        assert "domain-a" in graph_section
        assert "domain-b" in graph_section
        # Should have table with both directions
        lines = graph_section.split("\n")
        table_rows = [l for l in lines if l.startswith("| ") and "Source Domain" not in l and "---|" not in l]
        assert len(table_rows) == 2, f"Expected 2 table rows for bidirectional edges, got {len(table_rows)}"

    def test_build_cross_domain_graph_summary(self, tmp_path):
        """Test that summary shows correct edge count and standalone list."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        # Domain A → B (via structured table)
        domain_a = staging_dir / "domain-a.md"
        domain_a.write_text(
            "---\ndomain: domain-a\n---\n\n"
            "# Domain Analysis: domain-a\n\n"
            "## Cross-Domain Connections\n\n"
            "### Outgoing Dependencies\n\n"
            "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
            "| repo-a | repo-b | domain-b | Code-level | A uses B | import.py |\n"
        )

        # Domain B (no cross-domain connections)
        domain_b = staging_dir / "domain-b.md"
        domain_b.write_text(
            "---\ndomain: domain-b\n---\n\n"
            "# Domain Analysis: domain-b\n\n"
            "## Overview\nNo cross-domain connections.\n"
        )

        # Domain C (standalone, no file)
        # Domain D → A (via structured table)
        domain_d = staging_dir / "domain-d.md"
        domain_d.write_text(
            "---\ndomain: domain-d\n---\n\n"
            "# Domain Analysis: domain-d\n\n"
            "## Cross-Domain Connections\n\n"
            "### Outgoing Dependencies\n\n"
            "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
            "| repo-d | repo-a | domain-a | Service integration | D calls A | client.py |\n"
        )

        domain_list = [
            {"name": "domain-a", "participating_repos": ["repo-a"]},
            {"name": "domain-b", "participating_repos": ["repo-b"]},
            {"name": "domain-c", "participating_repos": ["repo-c"]},
            {"name": "domain-d", "participating_repos": ["repo-d"]},
        ]

        graph_section = DependencyMapAnalyzer._build_cross_domain_graph(staging_dir, domain_list)

        # Should have summary at the end
        assert "**Summary**" in graph_section or "Summary:" in graph_section
        assert "2 cross-domain edges" in graph_section or "2 edges" in graph_section
        # Standalone domains: domain-b and domain-c (A and D have edges)
        assert "domain-b" in graph_section
        assert "domain-c" in graph_section

    def test_build_cross_domain_graph_missing_file(self, tmp_path):
        """Test graceful skip when domain file is missing."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        # Only create domain-a file, domain-b file is missing
        domain_a = staging_dir / "domain-a.md"
        domain_a.write_text("""---
domain: domain-a
---

# Domain Analysis: domain-a

## Cross-Domain Connections

Uses **repo-b** from domain-b.
""")

        domain_list = [
            {"name": "domain-a", "participating_repos": ["repo-a"]},
            {"name": "domain-b", "participating_repos": ["repo-b"]},
        ]

        # Should not raise exception
        graph_section = DependencyMapAnalyzer._build_cross_domain_graph(staging_dir, domain_list)

        # May have partial edge from domain-a, or empty if logic requires both files
        # At minimum, should not crash
        assert isinstance(graph_section, str)

    def test_cross_domain_graph_appended_to_index(self, tmp_path):
        """Test that graph section appears in _index.md after Pass 3."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        # Create domain files with structured cross-domain tables
        domain_a = staging_dir / "domain-a.md"
        domain_a.write_text(
            "---\ndomain: domain-a\n---\n\n"
            "# Domain Analysis: domain-a\n\n"
            "## Cross-Domain Connections\n\n"
            "### Outgoing Dependencies\n\n"
            "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
            "| repo-a | repo-b | domain-b | Code-level | A uses B | import.py |\n"
        )

        domain_b = staging_dir / "domain-b.md"
        domain_b.write_text(
            "---\ndomain: domain-b\n---\n\n"
            "# Domain Analysis: domain-b\n\n"
            "## Cross-Domain Connections\n\n"
            "No verified cross-domain dependencies.\n"
        )

        domain_list = [
            {"name": "domain-a", "participating_repos": ["repo-a"]},
            {"name": "domain-b", "participating_repos": ["repo-b"]},
        ]

        repo_list = [
            {"alias": "repo-a", "description_summary": "Repo A"},
            {"alias": "repo-b", "description_summary": "Repo B"},
        ]

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        with patch.object(analyzer, "_invoke_claude_cli") as mock_invoke:
            mock_invoke.return_value = "# Index Content\n\nGenerated index."

            analyzer.run_pass_3_index(staging_dir, domain_list, repo_list, max_turns=10)

        # Check that _index.md contains cross-domain graph section
        index_file = staging_dir / "_index.md"
        assert index_file.exists()
        content = index_file.read_text()

        assert "## Cross-Domain Dependency Graph" in content
        assert "domain-a" in content
        assert "domain-b" in content

    def test_cross_domain_graph_not_appended_when_no_edges(self, tmp_path):
        """Test no graph section when no edges exist."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        # Create domain files with NO cross-domain connections
        domain_a = staging_dir / "domain-a.md"
        domain_a.write_text("""---
domain: domain-a
---

# Domain Analysis: domain-a

## Overview
Standalone domain.
""")

        domain_list = [
            {"name": "domain-a", "participating_repos": ["repo-a"]},
        ]

        repo_list = [
            {"alias": "repo-a", "description_summary": "Repo A"},
        ]

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        with patch.object(analyzer, "_invoke_claude_cli") as mock_invoke:
            mock_invoke.return_value = "# Index Content\n\nGenerated index."

            analyzer.run_pass_3_index(staging_dir, domain_list, repo_list, max_turns=10)

        index_file = staging_dir / "_index.md"
        content = index_file.read_text()

        # Should NOT contain cross-domain graph section
        assert "## Cross-Domain Dependency Graph" not in content

    def test_build_cross_domain_graph_deterministic(self, tmp_path):
        """Test same input produces same output, alphabetical sort."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        def _outgoing_table(source_repo, depends_on, target_domain):
            return (
                "## Cross-Domain Connections\n\n"
                "### Outgoing Dependencies\n\n"
                "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
                "|---|---|---|---|---|---|\n"
                f"| {source_repo} | {depends_on} | {target_domain} | Code-level | uses | src.py |\n"
            )

        # Create domain files using structured tables
        (staging_dir / "domain-c.md").write_text(
            "# Domain Analysis: domain-c\n\n" + _outgoing_table("repo-c", "repo-a", "domain-a")
        )
        (staging_dir / "domain-a.md").write_text(
            "# Domain Analysis: domain-a\n\n" + _outgoing_table("repo-a", "repo-b", "domain-b")
        )
        (staging_dir / "domain-b.md").write_text(
            "# Domain Analysis: domain-b\n\n" + _outgoing_table("repo-b", "repo-c", "domain-c")
        )

        # Domain list in arbitrary order
        domain_list = [
            {"name": "domain-b", "participating_repos": ["repo-b"]},
            {"name": "domain-a", "participating_repos": ["repo-a"]},
            {"name": "domain-c", "participating_repos": ["repo-c"]},
        ]

        # Build graph twice
        result1 = DependencyMapAnalyzer._build_cross_domain_graph(staging_dir, domain_list)
        result2 = DependencyMapAnalyzer._build_cross_domain_graph(staging_dir, domain_list)

        # Should be identical
        assert result1 == result2

        # Extract table rows and verify alphabetical order
        lines = result1.split("\n")
        table_rows = [l for l in lines if l.startswith("| ") and "Source Domain" not in l and "---|" not in l]

        # Should have 3 edges (a→b, b→c, c→a)
        assert len(table_rows) == 3

        # Extract source domains from table rows
        sources = []
        for row in table_rows:
            parts = [p.strip() for p in row.split("|") if p.strip()]
            if parts:
                sources.append(parts[0])

        # Verify alphabetical order
        assert sources == sorted(sources), f"Sources not alphabetically sorted: {sources}"

    def test_build_cross_domain_graph_negation_filter(self, tmp_path):
        """Test that structured table edges are detected; prose negation does not suppress them."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain_list = [
            {"name": "domain-a", "participating_repos": ["repo1"]},
            {"name": "domain-b", "participating_repos": ["repo2"]},
            {"name": "domain-c", "participating_repos": ["repo3"]},
        ]

        # domain-a has a structured outgoing table to domain-b only
        # Prose negation about repo3 is present but does NOT affect structured parsing
        (staging_dir / "domain-a.md").write_text(
            "## Cross-Domain Connections\n\n"
            "Note: FTS searches across repo3 returned zero results. "
            "No functional dependency exists.\n\n"
            "### Outgoing Dependencies\n\n"
            "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
            "| repo1 | repo2 | domain-b | Service integration | A calls B | client.py |\n\n"
            "### Incoming Dependencies\n\n"
            "No verified cross-domain dependencies.\n"
        )
        (staging_dir / "domain-b.md").write_text(
            "## Cross-Domain Connections\n\nNo verified cross-domain dependencies.\n"
        )
        (staging_dir / "domain-c.md").write_text(
            "## Cross-Domain Connections\n\nNo verified cross-domain dependencies.\n"
        )

        result = DependencyMapAnalyzer._build_cross_domain_graph(staging_dir, domain_list)

        # Should detect edge to domain-b (from structured table)
        assert "domain-b" in result
        assert "repo1" in result

        # domain-c should NOT appear (not in any outgoing table)
        lines = [line for line in result.split('\n') if line.startswith('| domain-a')]
        assert len(lines) == 1  # Only one edge from domain-a
        assert "domain-c" not in lines[0]

    def test_build_cross_domain_graph_negation_filter_standalone(self, tmp_path):
        """Test fully standalone domain with only negation text produces zero edges."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain_list = [
            {"name": "domain-a", "participating_repos": ["repo1"]},
            {"name": "domain-b", "participating_repos": ["repo2"]},
        ]

        (staging_dir / "domain-a.md").write_text(
            "## Cross-Domain Connections\n\n"
            "**None verified.** FTS search for repo1 across repo2 returned zero results. "
            "No functional dependency.\n"
        )
        (staging_dir / "domain-b.md").write_text("## Cross-Domain Connections\n\nNone.\n")

        result = DependencyMapAnalyzer._build_cross_domain_graph(staging_dir, domain_list)
        assert result == ""  # No edges

    def test_build_cross_domain_graph_negation_unrelated(self, tmp_path):
        """Test 'unrelated' keyword filters mentions."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain_list = [
            {"name": "domain-a", "participating_repos": ["repo1"]},
            {"name": "domain-b", "participating_repos": ["repo2"]},
        ]

        (staging_dir / "domain-a.md").write_text(
            "## Cross-Domain Connections\n\n"
            "Semantic search returned coincidentally similar but functionally unrelated "
            "code from repo2. This is not a dependency.\n"
        )
        (staging_dir / "domain-b.md").write_text("## Cross-Domain Connections\n\nNone.\n")

        result = DependencyMapAnalyzer._build_cross_domain_graph(staging_dir, domain_list)
        assert result == ""
