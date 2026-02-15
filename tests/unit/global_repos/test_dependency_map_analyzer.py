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

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
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
        assert call_args[0][0][:-1] == ["claude", "--print", "--model", "opus", "--max-turns", "50", "-p"]
        assert "Identify domain clusters" in call_args[0][0][-1]
        assert call_args[1]["cwd"] == str(tmp_path)
        assert call_args[1]["timeout"] == 300  # half of pass_timeout

        # Verify result
        assert len(result) == 1
        assert result[0]["name"] == "authentication"

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
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

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
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

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    def test_run_pass_2_writes_domain_file_with_frontmatter(self, tmp_path):
        """Test that run_pass_2_per_domain writes domain file with YAML frontmatter and strips meta-commentary."""
        with patch("subprocess.run") as mock_subprocess:
            # Mock stdout with meta-commentary that should be stripped
            mock_subprocess.return_value = MagicMock(
                returncode=0,
                stdout="Based on my analysis:\n\n# Authentication\n\nDomain analysis content here.",
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

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
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

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
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

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
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

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
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

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
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

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
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

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
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

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
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


class TestPass1JsonParseFailure:
    """Test Pass 1 JSON parse failure raises RuntimeError (FIX 3)."""

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
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


class TestApiKeyValidation:
    """Test API key validation (FIX 2)."""

    @patch.dict("os.environ", {}, clear=True)
    @patch("subprocess.run")
    def test_invoke_claude_cli_raises_if_no_api_key(
        self, mock_subprocess, tmp_path
    ):
        """Test that _invoke_claude_cli raises RuntimeError if ANTHROPIC_API_KEY missing."""
        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
        )

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        # Try to run Pass 1 - should raise before subprocess.run is called
        with pytest.raises(RuntimeError, match="Claude API key not available"):
            analyzer.run_pass_1_synthesis(staging_dir, {}, repo_list=[], max_turns=50)

        # Verify subprocess was never called
        mock_subprocess.assert_not_called()


class TestIncrementalPass2:
    """Test incremental Pass 2 with previous_domain_dir (FIX 9)."""

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
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
        previous_dir = tmp_path / "previous"
        previous_dir.mkdir()
        previous_domain_file = previous_dir / "authentication.md"
        previous_content = "---\nOld frontmatter\n---\n\n# Previous Analysis\n\nOld content here."
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

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
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

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
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

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
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

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
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

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
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

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
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
                return MagicMock(returncode=0, stdout="# Domain Analysis\n\nRetry succeeded.")

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

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
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
                return MagicMock(returncode=0, stdout="# Domain Analysis\n\nFull content here.")

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

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
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

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
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

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
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

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
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
