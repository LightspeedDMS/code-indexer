"""
Unit tests for Story #359: Truncation guard and no-op in refine_or_create_domain().

Tests cover Component 4:
- Truncation guard rejects short output (< 50% of original body > 500 chars)
- Truncation guard accepts normal output (>= 50%)
- Truncation guard skips short originals (<= 500 chars body)
- No-op when content identical

TDD RED PHASE: Tests written before production code exists.
"""

from pathlib import Path
from unittest.mock import Mock


from .conftest import (
    FULL_DOMAIN_BODY,
    FULL_DOMAIN_CONTENT,
    SAMPLE_DOMAINS_JSON,
    make_config,
    make_dependency_map_dir,
    make_live_dep_map,
    make_service,
)


class TestTruncationGuardRejectsShortOutput:
    """refine_or_create_domain rejects refinement result < 50% of original body."""

    def test_short_refinement_not_written(self, tmp_path: Path):
        """
        Given a domain file with body > 500 chars
        And invoke_refinement returns text < 50% of body length
        When refine_or_create_domain is called
        Then the file is NOT overwritten with the short result.
        """
        dep_map = make_dependency_map_dir(tmp_path)
        live_dep_map = make_live_dep_map(tmp_path)

        (dep_map / "auth-domain.md").write_text(FULL_DOMAIN_CONTENT)
        (live_dep_map / "auth-domain.md").write_text(FULL_DOMAIN_CONTENT)

        short_result = "Summary only."
        body_len = len(FULL_DOMAIN_BODY)
        assert body_len > 500, f"Test setup: body must be >500 chars, got {body_len}"
        assert len(short_result) < body_len * 0.5

        mock_analyzer = Mock()
        mock_analyzer.build_refinement_prompt.return_value = "prompt"
        mock_analyzer.invoke_refinement.return_value = short_result

        config = make_config()
        service = make_service(tmp_path, mock_analyzer, config)

        service.refine_or_create_domain(
            domain_name="auth-domain",
            domain_info=SAMPLE_DOMAINS_JSON[0],
            dependency_map_dir=live_dep_map,
            dependency_map_read_dir=dep_map,
            config=config,
        )

        result = (live_dep_map / "auth-domain.md").read_text()
        assert short_result not in result

    def test_short_refinement_returns_false(self, tmp_path: Path):
        """refine_or_create_domain returns False when truncation guard fires."""
        dep_map = make_dependency_map_dir(tmp_path)
        live_dep_map = make_live_dep_map(tmp_path)

        (dep_map / "auth-domain.md").write_text(FULL_DOMAIN_CONTENT)
        (live_dep_map / "auth-domain.md").write_text(FULL_DOMAIN_CONTENT)

        mock_analyzer = Mock()
        mock_analyzer.build_refinement_prompt.return_value = "prompt"
        mock_analyzer.invoke_refinement.return_value = "Too short."

        config = make_config()
        service = make_service(tmp_path, mock_analyzer, config)

        result = service.refine_or_create_domain(
            domain_name="auth-domain",
            domain_info=SAMPLE_DOMAINS_JSON[0],
            dependency_map_dir=live_dep_map,
            dependency_map_read_dir=dep_map,
            config=config,
        )
        assert result is False


class TestTruncationGuardAcceptsNormalOutput:
    """refine_or_create_domain accepts results >= 50% of original body length."""

    def test_proportional_result_is_written(self, tmp_path: Path):
        """
        Given a domain file with body > 500 chars
        And invoke_refinement returns text >= 50% of body length
        When refine_or_create_domain is called
        Then the file IS updated and method returns True.
        """
        dep_map = make_dependency_map_dir(tmp_path)
        live_dep_map = make_live_dep_map(tmp_path)

        (dep_map / "auth-domain.md").write_text(FULL_DOMAIN_CONTENT)

        body_len = len(FULL_DOMAIN_BODY)
        good_result = "# Domain Analysis: auth-domain\n\n## Overview\n\n" + "x" * int(
            body_len * 0.5
        )

        mock_analyzer = Mock()
        mock_analyzer.build_refinement_prompt.return_value = "prompt"
        mock_analyzer.invoke_refinement.return_value = good_result

        config = make_config()
        service = make_service(tmp_path, mock_analyzer, config)

        result = service.refine_or_create_domain(
            domain_name="auth-domain",
            domain_info=SAMPLE_DOMAINS_JSON[0],
            dependency_map_dir=live_dep_map,
            dependency_map_read_dir=dep_map,
            config=config,
        )
        assert result is True
        written = (live_dep_map / "auth-domain.md").read_text()
        assert good_result in written

    def test_full_response_accepted(self, tmp_path: Path):
        """A full-length response (well above 50%) is accepted without issue."""
        dep_map = make_dependency_map_dir(tmp_path)
        live_dep_map = make_live_dep_map(tmp_path)

        (dep_map / "auth-domain.md").write_text(FULL_DOMAIN_CONTENT)

        # Response that is larger than original body
        larger_result = FULL_DOMAIN_BODY + "\n\nAdditional fact-checked section.\n"

        mock_analyzer = Mock()
        mock_analyzer.build_refinement_prompt.return_value = "prompt"
        mock_analyzer.invoke_refinement.return_value = larger_result

        config = make_config()
        service = make_service(tmp_path, mock_analyzer, config)

        result = service.refine_or_create_domain(
            domain_name="auth-domain",
            domain_info=SAMPLE_DOMAINS_JSON[0],
            dependency_map_dir=live_dep_map,
            dependency_map_read_dir=dep_map,
            config=config,
        )
        assert result is True


class TestTruncationGuardSkipsShortOriginals:
    """Truncation guard is bypassed when original body <= 500 chars."""

    def test_short_original_bypasses_truncation_guard(self, tmp_path: Path):
        """
        Given a domain file with body <= 500 chars
        When invoke_refinement returns any non-empty result
        Then truncation guard does NOT fire (any result is accepted).
        """
        dep_map = make_dependency_map_dir(tmp_path)
        live_dep_map = make_live_dep_map(tmp_path)

        short_body = "# Domain Analysis: tiny\n\n## Overview\n\nSmall domain.\n"
        short_content = (
            "---\ndomain: tiny\nlast_analyzed: 2024-01-01T00:00:00+00:00\n---\n\n"
            + short_body
        )
        assert (
            len(short_body) <= 500
        ), f"Test setup: body must be <=500 chars, got {len(short_body)}"

        (dep_map / "tiny.md").write_text(short_content)

        very_short_result = "# Domain Analysis: tiny\n\n## Overview\n\nUpdated."

        mock_analyzer = Mock()
        mock_analyzer.build_refinement_prompt.return_value = "prompt"
        mock_analyzer.invoke_refinement.return_value = very_short_result

        tiny_domain_info = {
            "name": "tiny",
            "description": "Tiny domain",
            "participating_repos": ["tiny-service"],
        }

        config = make_config()
        service = make_service(tmp_path, mock_analyzer, config)

        result = service.refine_or_create_domain(
            domain_name="tiny",
            domain_info=tiny_domain_info,
            dependency_map_dir=live_dep_map,
            dependency_map_read_dir=dep_map,
            config=config,
        )
        assert result is True
        written = (live_dep_map / "tiny.md").read_text()
        assert very_short_result in written


class TestNoopWhenContentIdentical:
    """refine_or_create_domain skips write when refined content is identical to existing."""

    def test_identical_content_returns_false(self, tmp_path: Path):
        """
        Given invoke_refinement returns the same content as existing body
        When refine_or_create_domain is called
        Then file is not written and method returns False.
        """
        dep_map = make_dependency_map_dir(tmp_path)
        live_dep_map = make_live_dep_map(tmp_path)

        (dep_map / "auth-domain.md").write_text(FULL_DOMAIN_CONTENT)

        mock_analyzer = Mock()
        mock_analyzer.build_refinement_prompt.return_value = "prompt"
        # Return exact same body (stripped of frontmatter)
        mock_analyzer.invoke_refinement.return_value = FULL_DOMAIN_BODY

        config = make_config()
        service = make_service(tmp_path, mock_analyzer, config)

        result = service.refine_or_create_domain(
            domain_name="auth-domain",
            domain_info=SAMPLE_DOMAINS_JSON[0],
            dependency_map_dir=live_dep_map,
            dependency_map_read_dir=dep_map,
            config=config,
        )
        assert result is False

    def test_identical_content_does_not_write_file(self, tmp_path: Path):
        """
        When refined content is identical to existing body
        Then the live domain file is NOT created/overwritten.
        """
        dep_map = make_dependency_map_dir(tmp_path)
        live_dep_map = make_live_dep_map(tmp_path)

        (dep_map / "auth-domain.md").write_text(FULL_DOMAIN_CONTENT)
        # Live file does not exist before the call
        assert not (live_dep_map / "auth-domain.md").exists()

        mock_analyzer = Mock()
        mock_analyzer.build_refinement_prompt.return_value = "prompt"
        mock_analyzer.invoke_refinement.return_value = FULL_DOMAIN_BODY

        config = make_config()
        service = make_service(tmp_path, mock_analyzer, config)

        service.refine_or_create_domain(
            domain_name="auth-domain",
            domain_info=SAMPLE_DOMAINS_JSON[0],
            dependency_map_dir=live_dep_map,
            dependency_map_read_dir=dep_map,
            config=config,
        )

        # File should NOT have been created (no change)
        assert not (live_dep_map / "auth-domain.md").exists()
