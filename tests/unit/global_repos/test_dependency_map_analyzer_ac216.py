"""
Unit tests for DependencyMapAnalyzer Story #216 ACs.

Tests for:
- AC2: Programmatic _generate_index_md replaces Pass 3 Claude call
"""

import json

from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer


def _make_analyzer(tmp_path):
    return DependencyMapAnalyzer(
        golden_repos_root=tmp_path,
        cidx_meta_path=tmp_path / "cidx-meta",
        pass_timeout=600,
    )


# ─────────────────────────────────────────────────────────────────────────────
# AC2: Programmatic _generate_index_md
# ─────────────────────────────────────────────────────────────────────────────


class TestGenerateIndexMd:
    """AC2: _generate_index_md() builds _index.md programmatically from _domains.json data."""

    def test_creates_index_file(self, tmp_path):
        """AC2: _generate_index_md() writes _index.md to the staging directory."""
        analyzer = _make_analyzer(tmp_path)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain_list = [
            {"name": "auth", "description": "Auth", "participating_repos": ["auth-svc"]}
        ]
        repo_list = [{"alias": "auth-svc", "description_summary": "Auth service"}]

        analyzer._generate_index_md(staging_dir, domain_list, repo_list)

        assert (staging_dir / "_index.md").exists()

    def test_has_repo_to_domain_matrix_heading(self, tmp_path):
        """AC2: _index.md contains exact hardcoded heading '## Repo-to-Domain Matrix'."""
        analyzer = _make_analyzer(tmp_path)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain_list = [
            {"name": "auth", "description": "Auth", "participating_repos": ["auth-svc"]}
        ]
        repo_list = [{"alias": "auth-svc", "description_summary": "Auth service"}]

        analyzer._generate_index_md(staging_dir, domain_list, repo_list)

        content = (staging_dir / "_index.md").read_text()
        assert "## Repo-to-Domain Matrix" in content

    def test_has_cross_domain_dependencies_heading(self, tmp_path):
        """AC2: _index.md contains exact hardcoded heading '## Cross-Domain Dependencies'."""
        analyzer = _make_analyzer(tmp_path)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain_list = [
            {"name": "auth", "description": "Auth", "participating_repos": ["auth-svc"]}
        ]
        repo_list = [{"alias": "auth-svc", "description_summary": "Auth service"}]

        analyzer._generate_index_md(staging_dir, domain_list, repo_list)

        content = (staging_dir / "_index.md").read_text()
        assert "## Cross-Domain Dependencies" in content

    def test_matrix_contains_all_repos(self, tmp_path):
        """AC2: Repo-to-Domain Matrix contains all repos from domain_list."""
        analyzer = _make_analyzer(tmp_path)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain_list = [
            {
                "name": "auth",
                "description": "Auth",
                "participating_repos": ["auth-svc", "api-gw"],
            },
            {
                "name": "billing",
                "description": "Billing",
                "participating_repos": ["bill-svc"],
            },
        ]
        repo_list = [
            {"alias": "auth-svc", "description_summary": "Auth"},
            {"alias": "api-gw", "description_summary": "API gateway"},
            {"alias": "bill-svc", "description_summary": "Billing"},
        ]

        analyzer._generate_index_md(staging_dir, domain_list, repo_list)

        content = (staging_dir / "_index.md").read_text()
        assert "auth-svc" in content
        assert "api-gw" in content
        assert "bill-svc" in content

    def test_has_yaml_frontmatter(self, tmp_path):
        """AC2: _index.md starts with YAML frontmatter."""
        analyzer = _make_analyzer(tmp_path)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain_list = [
            {"name": "auth", "description": "Auth", "participating_repos": ["auth-svc"]}
        ]
        repo_list = [{"alias": "auth-svc", "description_summary": "Auth service"}]

        analyzer._generate_index_md(staging_dir, domain_list, repo_list)

        content = (staging_dir / "_index.md").read_text()
        assert content.startswith("---")
        assert "schema_version" in content


# ─────────────────────────────────────────────────────────────────────────────
# AC4: Ghost domain reconciliation
# ─────────────────────────────────────────────────────────────────────────────


class TestReconcileDomainsJson:
    """AC4: _reconcile_domains_json() removes domains without .md files on disk."""

    def test_removes_domain_without_md_file(self, tmp_path):
        """AC4: Domain without .md file is removed from returned list."""
        analyzer = _make_analyzer(tmp_path)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain_list = [
            {
                "name": "auth",
                "description": "Auth",
                "participating_repos": ["auth-svc"],
            },
            {
                "name": "ghost",
                "description": "Ghost",
                "participating_repos": ["ghost-svc"],
            },
        ]
        (staging_dir / "auth.md").write_text(
            "# Domain Analysis: auth\n\n## Overview\nValid.\n"
        )

        result = analyzer._reconcile_domains_json(staging_dir, domain_list)
        names = [d["name"] for d in result]
        assert "auth" in names
        assert "ghost" not in names

    def test_keeps_domain_with_md_file(self, tmp_path):
        """AC4: Domain with .md file is kept in returned list."""
        analyzer = _make_analyzer(tmp_path)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain_list = [
            {"name": "auth", "description": "Auth", "participating_repos": ["auth-svc"]}
        ]
        (staging_dir / "auth.md").write_text(
            "# Domain Analysis: auth\n\n## Overview\nValid.\n"
        )

        result = analyzer._reconcile_domains_json(staging_dir, domain_list)
        assert len(result) == 1
        assert result[0]["name"] == "auth"

    def test_updates_domains_json_file(self, tmp_path):
        """AC4: _reconcile_domains_json() overwrites _domains.json without ghost domains."""
        analyzer = _make_analyzer(tmp_path)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain_list = [
            {
                "name": "auth",
                "description": "Auth",
                "participating_repos": ["auth-svc"],
            },
            {
                "name": "ghost",
                "description": "Ghost",
                "participating_repos": ["ghost-svc"],
            },
        ]
        (staging_dir / "_domains.json").write_text(json.dumps(domain_list))
        (staging_dir / "auth.md").write_text("# Domain Analysis: auth\n\nContent.\n")

        analyzer._reconcile_domains_json(staging_dir, domain_list)

        updated = json.loads((staging_dir / "_domains.json").read_text())
        names = [d["name"] for d in updated]
        assert "auth" in names
        assert "ghost" not in names


# ─────────────────────────────────────────────────────────────────────────────
# AC5: Pass 1 domain stability - build_pass1_prompt
# ─────────────────────────────────────────────────────────────────────────────


class TestPass1DomainStability:
    """AC5: build_pass1_prompt() includes previous domain structure for stability."""

    def test_includes_previous_domains_when_exists(self, tmp_path):
        """AC5: Prompt includes 'Previous Domain Structure' when _domains.json exists."""
        analyzer = _make_analyzer(tmp_path)

        final_dir = tmp_path / "cidx-meta" / "dependency-map"
        final_dir.mkdir(parents=True)
        previous_domains = [
            {
                "name": "auth",
                "description": "Auth domain",
                "participating_repos": ["auth-svc"],
            },
        ]
        (final_dir / "_domains.json").write_text(json.dumps(previous_domains))

        repo_list = [
            {
                "alias": "auth-svc",
                "clone_path": "/fake/auth",
                "file_count": 10,
                "total_bytes": 1000,
            },
        ]
        prompt = analyzer.build_pass1_prompt(
            repo_list=repo_list,
            previous_domains_dir=final_dir,
        )

        assert "Previous Domain Structure" in prompt
        assert "auth" in prompt

    def test_excludes_previous_domains_when_none(self, tmp_path):
        """AC5: Prompt does NOT include 'Previous Domain Structure' when previous_domains_dir=None."""
        analyzer = _make_analyzer(tmp_path)

        repo_list = [
            {
                "alias": "auth-svc",
                "clone_path": "/fake/auth",
                "file_count": 10,
                "total_bytes": 1000,
            },
        ]

        prompt = analyzer.build_pass1_prompt(
            repo_list=repo_list,
            previous_domains_dir=None,
        )

        assert "Previous Domain Structure" not in prompt

    def test_stability_instruction_present_when_previous_exists(self, tmp_path):
        """AC5: Stability instruction text is present when previous domains provided."""
        analyzer = _make_analyzer(tmp_path)

        final_dir = tmp_path / "cidx-meta" / "dependency-map"
        final_dir.mkdir(parents=True)
        previous_domains = [
            {
                "name": "auth",
                "description": "Auth",
                "participating_repos": ["auth-svc"],
            },
        ]
        (final_dir / "_domains.json").write_text(json.dumps(previous_domains))

        repo_list = [
            {
                "alias": "auth-svc",
                "clone_path": "/fake/auth",
                "file_count": 10,
                "total_bytes": 1000,
            },
        ]
        prompt = analyzer.build_pass1_prompt(
            repo_list=repo_list,
            previous_domains_dir=final_dir,
        )

        assert "stable" in prompt.lower() or "stability" in prompt.lower()


# ─────────────────────────────────────────────────────────────────────────────
# Domain concept clarity in Phase 1 prompts
# ─────────────────────────────────────────────────────────────────────────────


class TestPass1DomainConceptClarity:
    """Domain concept clarity in Phase 1 — definition lives in CLAUDE.md (Bug #995 Phase 2).

    The domain definition was moved from the Pass 1 prompt into CLAUDE.md so that
    Claude CLI loads it automatically as project context. Tests verify CLAUDE.md
    content rather than prompt content.
    """

    def test_claude_md_contains_domain_definition_section(self, tmp_path):
        """CLAUDE.md generated by generate_orientation_files includes 'What Domains Are' section."""
        analyzer = _make_analyzer(tmp_path)
        # generate_orientation_files writes CLAUDE.md to golden_repos_root (tmp_path here)
        analyzer.generate_orientation_files([])
        claude_md = tmp_path / "CLAUDE.md"
        assert claude_md.exists(), "generate_orientation_files must create CLAUDE.md"
        content = claude_md.read_text()
        assert "What Domains Are" in content

    def test_claude_md_states_domain_is_not_repository(self, tmp_path):
        """CLAUDE.md explicitly states a domain is NOT a repository."""
        analyzer = _make_analyzer(tmp_path)
        analyzer.generate_orientation_files([])
        content = (tmp_path / "CLAUDE.md").read_text()
        assert "domain is NOT a" in content

    def test_claude_md_states_grouping_purpose(self, tmp_path):
        """CLAUDE.md explains the purpose of grouping repos into domains."""
        analyzer = _make_analyzer(tmp_path)
        analyzer.generate_orientation_files([])
        content = (tmp_path / "CLAUDE.md").read_text()
        assert (
            "group repositories" in content.lower()
            or "understood at a higher abstraction" in content.lower()
        )

    def test_build_pass1_prompt_does_not_contain_domain_definition(self, tmp_path):
        """Pass 1 prompt does NOT contain 'What Is a Domain?' — it lives in CLAUDE.md."""
        analyzer = _make_analyzer(tmp_path)
        repo_list = [
            {
                "alias": "svc-a",
                "clone_path": "/fake/a",
                "file_count": 10,
                "total_bytes": 1000,
            }
        ]
        prompt = analyzer.build_pass1_prompt(repo_list)
        # Definition moved to CLAUDE.md; prompt should not duplicate it
        assert "## What Is a Domain?" not in prompt
        # But prompt must still have output format section
        assert "## Output Format" in prompt

    def test_build_domain_definition_section_no_longer_exists(self, tmp_path):
        """_build_domain_definition_section() was deleted in Bug #995 Phase 2."""
        analyzer = _make_analyzer(tmp_path)
        assert not hasattr(analyzer, "_build_domain_definition_section"), (
            "_build_domain_definition_section() must be deleted — "
            "domain definition now lives in CLAUDE.md"
        )
