"""
Unit tests for dep-map prompt orientation delivery and absolute guideline paths.

Follow-up to the neutral-cwd isolation fix: once the agent's subprocess cwd
became a neutral scratch directory (never golden_repos_root), the CLI's own
project-config auto-load no longer picks up the server-written CLAUDE.md
orientation file at golden_repos_root, and every dep-map prompt reference to
the canonical guideline files (_dep_types.md / _analysis_guidelines.md) using
a path relative to golden_repos_root became unreachable from the agent's
actual cwd.

These tests assert that every dependency-map prompt now:
- Instructs the agent to Read the orientation CLAUDE.md by absolute path
  (restoring the orientation content and the prompt-injection guard the
  agent used to receive via auto-load), and
- References the guideline files by absolute path rather than a path
  relative to a cwd the agent's process no longer has.

They also assert that generate_orientation_files() no longer states the now
-false claim that the agent's cwd is golden_repos_root, replacing it with a
statement that stays true regardless of where the CLI subprocess actually
runs.
"""

from pathlib import Path
from typing import Any, Dict, List

from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_analyzer(tmp_path: Path) -> DependencyMapAnalyzer:
    golden_repos_root = tmp_path / "golden-repos"
    golden_repos_root.mkdir()
    return DependencyMapAnalyzer(
        golden_repos_root=golden_repos_root,
        cidx_meta_path=golden_repos_root / "cidx-meta",
        pass_timeout=600,
    )


def _dep_types_abs(analyzer: DependencyMapAnalyzer) -> str:
    return str(
        analyzer.golden_repos_root / "cidx-meta" / "dependency-map" / "_dep_types.md"
    )


def _analysis_guidelines_abs(analyzer: DependencyMapAnalyzer) -> str:
    return str(
        analyzer.golden_repos_root
        / "cidx-meta"
        / "dependency-map"
        / "_analysis_guidelines.md"
    )


def _orientation_file_abs(analyzer: DependencyMapAnalyzer) -> str:
    return str(analyzer.golden_repos_root / "CLAUDE.md")


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
        }
    ]


def _make_repo_list(tmp_path: Path) -> List[Dict[str, Any]]:
    return [
        {
            "alias": "repo-1",
            "clone_path": str(tmp_path / "repo-1"),
            "total_bytes": 500_000,
            "file_count": 30,
        },
        {
            "alias": "repo-2",
            "clone_path": str(tmp_path / "repo-2"),
            "total_bytes": 300_000,
            "file_count": 20,
        },
    ]


# ---------------------------------------------------------------------------
# New small path-helper methods
# ---------------------------------------------------------------------------


class TestPathHelpers:
    """New helpers centralize absolute-path construction for the guideline files."""

    def test_dep_types_abs_path_is_absolute_under_golden_repos_root(self, tmp_path):
        analyzer = _make_analyzer(tmp_path)
        assert str(analyzer._dep_types_abs_path()) == _dep_types_abs(analyzer)

    def test_analysis_guidelines_abs_path_is_absolute_under_golden_repos_root(
        self, tmp_path
    ):
        analyzer = _make_analyzer(tmp_path)
        assert str(
            analyzer._analysis_guidelines_abs_path()
        ) == _analysis_guidelines_abs(analyzer)


class TestOrientationReminder:
    """New helper builds the 'Read the orientation file first' prompt fragment."""

    def test_reminder_references_orientation_file_by_absolute_path(self, tmp_path):
        analyzer = _make_analyzer(tmp_path)
        reminder = analyzer._build_orientation_reminder()
        assert _orientation_file_abs(analyzer) in reminder

    def test_reminder_instructs_reading_the_file(self, tmp_path):
        analyzer = _make_analyzer(tmp_path)
        reminder = analyzer._build_orientation_reminder()
        assert "Read" in reminder

    def test_reminder_does_not_mention_staging(self, tmp_path):
        """build_pass1_prompt asserts 'staging' is absent from its output — the
        reminder text must not introduce that word."""
        analyzer = _make_analyzer(tmp_path)
        reminder = analyzer._build_orientation_reminder()
        assert "staging" not in reminder.lower()


# ---------------------------------------------------------------------------
# generate_orientation_files: truthful "where am I" statement
# ---------------------------------------------------------------------------


class TestOrientationFileTruthfulCwdStatement:
    def test_false_golden_repos_root_cwd_claim_removed(self, tmp_path):
        analyzer = _make_analyzer(tmp_path)
        analyzer.generate_orientation_files([])
        content = (analyzer.golden_repos_root / "CLAUDE.md").read_text()
        assert "You are running in the golden-repos root directory" not in content

    def test_states_cwd_is_not_the_workspace_root(self, tmp_path):
        analyzer = _make_analyzer(tmp_path)
        analyzer.generate_orientation_files([])
        content = (analyzer.golden_repos_root / "CLAUDE.md").read_text()
        assert "not this workspace root" in content

    def test_states_cwd_is_a_neutral_scratch_directory(self, tmp_path):
        analyzer = _make_analyzer(tmp_path)
        analyzer.generate_orientation_files([])
        content = (analyzer.golden_repos_root / "CLAUDE.md").read_text()
        assert "neutral scratch directory" in content


# ---------------------------------------------------------------------------
# Pass 1 (testable extraction): build_pass1_prompt
# ---------------------------------------------------------------------------


class TestBuildPass1PromptOrientationAndPaths:
    def test_reference_material_uses_absolute_paths(self, tmp_path):
        analyzer = _make_analyzer(tmp_path)
        prompt = analyzer.build_pass1_prompt(repo_list=_make_repo_list(tmp_path))
        assert _dep_types_abs(analyzer) in prompt
        assert _analysis_guidelines_abs(analyzer) in prompt

    def test_includes_orientation_reminder(self, tmp_path):
        analyzer = _make_analyzer(tmp_path)
        prompt = analyzer.build_pass1_prompt(repo_list=_make_repo_list(tmp_path))
        assert _orientation_file_abs(analyzer) in prompt


# ---------------------------------------------------------------------------
# Pass 1 (production path): run_pass_1_synthesis's own inline prompt
# ---------------------------------------------------------------------------


class TestRunPass1SynthesisOrientationAndPaths:
    def _capture_prompt(self, analyzer, staging_dir, repo_list):
        captured: Dict[str, str] = {}

        def fake_invoke(prompt, timeout, dangerously_skip_permissions=False, **kwargs):
            captured["value"] = prompt
            return "[]"

        analyzer._invoke_pass1_dispatcher = fake_invoke
        analyzer.run_pass_1_synthesis(staging_dir, repo_list, max_turns=10)
        return captured["value"]

    def test_reference_material_uses_absolute_paths(self, tmp_path):
        analyzer = _make_analyzer(tmp_path)
        staging_dir = (
            analyzer.golden_repos_root / "cidx-meta" / "dependency-map.staging"
        )
        staging_dir.mkdir(parents=True)
        prompt = self._capture_prompt(analyzer, staging_dir, _make_repo_list(tmp_path))
        assert _dep_types_abs(analyzer) in prompt
        assert _analysis_guidelines_abs(analyzer) in prompt

    def test_includes_orientation_reminder(self, tmp_path):
        analyzer = _make_analyzer(tmp_path)
        staging_dir = (
            analyzer.golden_repos_root / "cidx-meta" / "dependency-map.staging"
        )
        staging_dir.mkdir(parents=True)
        prompt = self._capture_prompt(analyzer, staging_dir, _make_repo_list(tmp_path))
        assert _orientation_file_abs(analyzer) in prompt


# ---------------------------------------------------------------------------
# Pass 2: _build_output_first_prompt (large domains) and _build_standard_prompt
# (small domains)
# ---------------------------------------------------------------------------


class TestOutputFirstPromptOrientationAndPaths:
    def test_analysis_methodology_and_prohibited_section_use_absolute_paths(
        self, tmp_path
    ):
        analyzer = _make_analyzer(tmp_path)
        prompt = analyzer._build_output_first_prompt(
            domain=_make_domain(),
            domain_list=_make_domain_list(),
            repo_list=_make_repo_list(tmp_path),
        )
        assert _dep_types_abs(analyzer) in prompt
        assert _analysis_guidelines_abs(analyzer) in prompt

    def test_includes_orientation_reminder(self, tmp_path):
        analyzer = _make_analyzer(tmp_path)
        prompt = analyzer._build_output_first_prompt(
            domain=_make_domain(),
            domain_list=_make_domain_list(),
            repo_list=_make_repo_list(tmp_path),
        )
        assert _orientation_file_abs(analyzer) in prompt


class TestStandardPromptOrientationAndPaths:
    def test_exploration_evidence_and_verification_sections_use_absolute_paths(
        self, tmp_path
    ):
        analyzer = _make_analyzer(tmp_path)
        prompt = analyzer._build_standard_prompt(
            domain=_make_domain(),
            domain_list=_make_domain_list(),
            repo_list=_make_repo_list(tmp_path),
        )
        assert _dep_types_abs(analyzer) in prompt
        assert _analysis_guidelines_abs(analyzer) in prompt

    def test_output_section_prohibited_content_uses_absolute_path(self, tmp_path):
        analyzer = _make_analyzer(tmp_path)
        prompt = analyzer._build_standard_prompt(
            domain=_make_domain(),
            domain_list=_make_domain_list(),
            repo_list=_make_repo_list(tmp_path),
        )
        # _build_std_output_section's own PROHIBITED Content reference
        assert prompt.count(_analysis_guidelines_abs(analyzer)) >= 2

    def test_includes_orientation_reminder(self, tmp_path):
        analyzer = _make_analyzer(tmp_path)
        prompt = analyzer._build_standard_prompt(
            domain=_make_domain(),
            domain_list=_make_domain_list(),
            repo_list=_make_repo_list(tmp_path),
        )
        assert _orientation_file_abs(analyzer) in prompt


# ---------------------------------------------------------------------------
# Delta merge, new-domain, refinement prompts
# ---------------------------------------------------------------------------


class TestDeltaMergePromptOrientationAndPaths:
    def test_dependency_types_and_methodology_sections_use_absolute_paths(
        self, tmp_path
    ):
        analyzer = _make_analyzer(tmp_path)
        prompt = analyzer.build_delta_merge_prompt(
            domain_name="test-domain",
            existing_content="existing content",
            changed_repos=[],
            new_repos=[],
            removed_repos=[],
            domain_list=["test-domain"],
        )
        assert _dep_types_abs(analyzer) in prompt
        assert _analysis_guidelines_abs(analyzer) in prompt

    def test_includes_orientation_reminder(self, tmp_path):
        analyzer = _make_analyzer(tmp_path)
        prompt = analyzer.build_delta_merge_prompt(
            domain_name="test-domain",
            existing_content="existing content",
            changed_repos=[],
            new_repos=[],
            removed_repos=[],
            domain_list=["test-domain"],
        )
        assert _orientation_file_abs(analyzer) in prompt


class TestNewDomainPromptOrientationAndPaths:
    def test_dependency_types_and_methodology_sections_use_absolute_paths(
        self, tmp_path
    ):
        analyzer = _make_analyzer(tmp_path)
        prompt = analyzer.build_new_domain_prompt(
            domain_name="new-domain",
            participating_repos=["repo-1", "repo-2"],
        )
        assert _dep_types_abs(analyzer) in prompt
        assert _analysis_guidelines_abs(analyzer) in prompt

    def test_includes_orientation_reminder(self, tmp_path):
        analyzer = _make_analyzer(tmp_path)
        prompt = analyzer.build_new_domain_prompt(
            domain_name="new-domain",
            participating_repos=["repo-1", "repo-2"],
        )
        assert _orientation_file_abs(analyzer) in prompt


class TestRefinementPromptOrientationAndPaths:
    def test_reference_material_uses_absolute_paths(self, tmp_path):
        analyzer = _make_analyzer(tmp_path)
        prompt = analyzer.build_refinement_prompt(
            domain_name="test-domain",
            existing_body="existing body",
            participating_repos=["repo-1", "repo-2"],
        )
        assert _dep_types_abs(analyzer) in prompt
        assert _analysis_guidelines_abs(analyzer) in prompt

    def test_includes_orientation_reminder(self, tmp_path):
        analyzer = _make_analyzer(tmp_path)
        prompt = analyzer.build_refinement_prompt(
            domain_name="test-domain",
            existing_body="existing body",
            participating_repos=["repo-1", "repo-2"],
        )
        assert _orientation_file_abs(analyzer) in prompt
