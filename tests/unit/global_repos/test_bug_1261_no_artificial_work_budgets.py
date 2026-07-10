"""
Regression tests for Bug #1261: Artificial work-budgets on dependency-map
analysis jobs (search-call ceiling, agent-turn caps, output-length range).

The dependency-map ANALYSIS jobs (Claude-CLI per-domain analysis) previously
carried three artificial numeric budgets that capped the thoroughness of
legitimate analysis work -- the same anti-pattern the project deliberately
removed from the indexing path (see CLAUDE.md "Indexing Path Has No
Job/Subprocess/Per-File Timeouts", Bug #1218):

1. A hardcoded search-call CEILING ("AT MOST 5 calls") on the large-domain
   (>3 repos) prompt, applied INVERSELY to complexity (the small-domain
   prompt mandated an unbounded FLOOR of searches instead).
2. Agent-turn CAPS (`dependency_map_pass2_max_turns=50`,
   `dependency_map_delta_max_turns=30`) that could truncate a legitimately
   long analysis mid-flight.
3. A hardcoded output-length BUDGET ("between 3,000 and 10,000 characters")
   on the small-domain prompt.

This test module asserts all three budgets are gone: searching is mandatory
and unbounded on both large and small domain prompts, and the per-domain /
delta max-turns config defaults are 0 (unlimited, matching pass1's existing
convention -- confirmed that max_turns=0 omits the --max-turns flag entirely
rather than meaning "zero turns", see ClaudeInvoker.invoke_full_agentic).
"""

from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer
from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig
from code_indexer.server.services.config_service import ConfigService


def _make_analyzer(tmp_path):
    return DependencyMapAnalyzer(
        golden_repos_root=tmp_path,
        cidx_meta_path=tmp_path / "cidx-meta",
        pass_timeout=600,
    )


class TestLargeDomainPromptNoSearchCeiling:
    """Instance #1 + #2: large-domain prompt must not cap or downgrade search."""

    def _build_prompt(self, tmp_path):
        analyzer = _make_analyzer(tmp_path)
        domain = {
            "name": "test-domain",
            "description": "Test domain",
            "participating_repos": ["repo1", "repo2", "repo3", "repo4"],
        }
        return analyzer._build_output_first_prompt(domain, [domain], [], None)

    def test_no_at_most_5_ceiling(self, tmp_path):
        prompt = self._build_prompt(tmp_path)
        assert "AT MOST 5" not in prompt

    def test_no_optional_search_framing(self, tmp_path):
        prompt = self._build_prompt(tmp_path)
        assert "OPTIONAL" not in prompt

    def test_no_do_not_explore_extensively(self, tmp_path):
        prompt = self._build_prompt(tmp_path)
        assert "do NOT explore extensively" not in prompt

    def test_no_confirmation_only_framing(self, tmp_path):
        prompt = self._build_prompt(tmp_path)
        assert "CONFIRMING what you wrote, not for discovery" not in prompt

    def test_no_max_5_calls_heading(self, tmp_path):
        prompt = self._build_prompt(tmp_path)
        assert "max 5 calls" not in prompt

    def test_search_is_mandatory_and_unbounded(self, tmp_path):
        """Search must remain available for discovery, not just verification."""
        prompt = self._build_prompt(tmp_path)
        assert "MANDATORY" in prompt
        assert "search_code" in prompt
        # Discovery framing must be present -- large domains need the MOST
        # cross-repo searching, not the least.
        assert "discovery" in prompt.lower()


class TestSmallDomainPromptStillMandatoryUnbounded:
    """Instance #2 sanity check: small-domain floor is unaffected by this fix."""

    def test_small_domain_still_has_unbounded_mandatory_floor(self, tmp_path):
        analyzer = _make_analyzer(tmp_path)
        prompt = analyzer._build_std_mcp_search(
            repos_sorted=["repo1", "repo2"],
            participating_repos=["repo1", "repo2"],
            repo_list=[],
        )
        assert "AT LEAST 3" in prompt
        assert "Do NOT skip MCP searches" in prompt
        # No new ceiling introduced on the small-domain path.
        assert "AT MOST" not in prompt


class TestStandardPromptNoOutputLengthCeiling:
    """Instance #4: small-domain output section must not hardcode a character range."""

    def test_no_hard_character_range(self, tmp_path):
        analyzer = _make_analyzer(tmp_path)
        section = analyzer._build_std_output_section("test-domain")
        assert "3,000" not in section
        assert "10,000" not in section
        assert "3000" not in section
        assert "10000" not in section

    def test_qualitative_conciseness_guidance_retained(self, tmp_path):
        """Removing the numeric cap must not remove all conciseness guidance."""
        analyzer = _make_analyzer(tmp_path)
        section = analyzer._build_std_output_section("test-domain")
        assert "concise" in section.lower() or "CONCISE" in section


class TestMaxTurnsDefaultsUnlimited:
    """Instance #3: pass2/delta max-turns default to 0 (unlimited), matching pass1."""

    def test_pass2_max_turns_default_is_zero(self):
        config = ClaudeIntegrationConfig()
        assert config.dependency_map_pass2_max_turns == 0

    def test_delta_max_turns_default_is_zero(self):
        config = ClaudeIntegrationConfig()
        assert config.dependency_map_delta_max_turns == 0

    def test_pass1_max_turns_still_zero(self):
        """Pass 1 was already correct -- confirm it remains unchanged."""
        config = ClaudeIntegrationConfig()
        assert config.dependency_map_pass1_max_turns == 0


class TestConfigServiceAllowsUnlimitedMaxTurns:
    """Instance #3: ConfigService must not clamp pass2/delta up to a minimum of 5.

    A minimum-5 clamp would make it impossible for an operator to restore the
    unlimited (0) default via the Web UI, defeating the fix.
    """

    def test_pass2_max_turns_accepts_zero(self, tmp_path):
        service = ConfigService(server_dir_path=str(tmp_path))
        service.update_setting("claude_cli", "dependency_map_pass2_max_turns", 0)
        claude_config = service.get_claude_integration_config()
        assert claude_config.dependency_map_pass2_max_turns == 0

    def test_delta_max_turns_accepts_zero(self, tmp_path):
        service = ConfigService(server_dir_path=str(tmp_path))
        service.update_setting("claude_cli", "dependency_map_delta_max_turns", 0)
        claude_config = service.get_claude_integration_config()
        assert claude_config.dependency_map_delta_max_turns == 0

    def test_pass2_max_turns_still_clamps_negative_to_zero(self, tmp_path):
        service = ConfigService(server_dir_path=str(tmp_path))
        service.update_setting("claude_cli", "dependency_map_pass2_max_turns", -1)
        claude_config = service.get_claude_integration_config()
        assert claude_config.dependency_map_pass2_max_turns == 0

    def test_delta_max_turns_still_clamps_negative_to_zero(self, tmp_path):
        service = ConfigService(server_dir_path=str(tmp_path))
        service.update_setting("claude_cli", "dependency_map_delta_max_turns", -1)
        claude_config = service.get_claude_integration_config()
        assert claude_config.dependency_map_delta_max_turns == 0
