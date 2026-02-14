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

        # Provide repo_list with matching aliases for validation
        repo_list = [
            {"alias": "auth-service", "description_summary": "Auth service"},
            {"alias": "web-app", "description_summary": "Web application"},
        ]

        result = analyzer.run_pass_1_synthesis(staging_dir, repo_descriptions, repo_list=repo_list, max_turns=50)

        # Verify subprocess called with correct arguments
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args

        # Check command structure (last element is the prompt)
        assert call_args[0][0][:-1] == ["claude", "--print", "--max-turns", "50", "--allowedTools", "mcp__cidx-local__search_code", "-p"]
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

            # Provide repo_list with matching alias for validation
            repo_list = [
                {"alias": "repo1", "description_summary": "First repository"},
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
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout="# Authentication Domain\n\nDetailed analysis...",
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
        """Test that run_pass_2_per_domain writes domain file with YAML frontmatter."""
        with patch("subprocess.run") as mock_subprocess:
            mock_subprocess.return_value = MagicMock(
                returncode=0,
                stdout="# Authentication\n\nDomain analysis content here.",
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
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout="# Updated Domain Analysis\n\nNew analysis...",
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
