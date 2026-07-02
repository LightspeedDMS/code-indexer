"""
Unit tests for Story #738: Research Assistant prompt rewrite.

Verifies the authoritative prompt at
  src/code_indexer/server/config/research_assistant_prompt.md

has been rewritten to:
  1. Remove "INVESTIGATE and REPORT, not implement fixes" authority language
  2. Contain REMEDIATION PROTOCOL with DIAGNOSE/PLAN/SCOPE CHECK/EXECUTE/VERIFY
  3. Contain SELF-DIAGNOSED vs OPERATOR-DIRECTED section with prompt-injection
     guard against adversarial content in logs/files/DB rows
  4. Replace "DO NOT explain WHY you cannot perform an action" with softer
     reason-category disclosure — both halves tested independently
  5. Preserve source-code-edit prohibition outside cidx-meta (carveout also checked)
  6. Preserve the GITHUB BUG REPORT CREATION section (issue_manager.py)
  7. Include scope boundary mentioning server_data_dir and golden_repos_dir
  8. Substitute ALL {variable} template placeholders (general assertion, not
     a fixed hardcoded list)

Tests use load_research_prompt() — the real runtime code path.  No mocks of
the service itself are needed.
"""

import re

import pytest

from code_indexer.server.services.research_assistant_service import (
    ResearchAssistantService,
)
from code_indexer.server.storage.database_manager import DatabaseSchema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def service(tmp_path):
    db_path = str(tmp_path / "data" / "cidx_server.db")
    (tmp_path / "data").mkdir(parents=True)
    DatabaseSchema(db_path=db_path).initialize_database()
    return ResearchAssistantService(db_path=db_path)


@pytest.fixture
def prompt_text(service):
    """Rendered prompt with all {variable} placeholders substituted."""
    return service.load_research_prompt()


@pytest.fixture
def prompt_text_normalized(prompt_text):
    """Single-space-collapsed version for cross-line phrase assertions."""
    return re.sub(r"\s+", " ", prompt_text).strip()


# ---------------------------------------------------------------------------
# 1. Old authority language must be gone
# ---------------------------------------------------------------------------


class TestObsoleteAuthorityRemoved:
    def test_investigate_and_report_only_phrase_absent(self, prompt_text_normalized):
        """Old 'INVESTIGATE and REPORT, not implement fixes' must be absent."""
        assert (
            "INVESTIGATE and REPORT, not implement fixes" not in prompt_text_normalized
        )

    def test_investigate_and_report_not_to_implement_phrase_absent(
        self, prompt_text_normalized
    ):
        """Variant phrasing 'not to implement fixes' must also be absent."""
        assert (
            "INVESTIGATE and REPORT, not to implement fixes"
            not in prompt_text_normalized
        )


# ---------------------------------------------------------------------------
# 2. REMEDIATION PROTOCOL with all five ordered steps
# ---------------------------------------------------------------------------


class TestRemediationProtocolPresent:
    def test_has_remediation_protocol_header(self, prompt_text):
        assert re.search(r"REMEDIATION PROTOCOL", prompt_text, re.IGNORECASE), (
            "Expected a 'REMEDIATION PROTOCOL' section header in the prompt"
        )

    @pytest.mark.parametrize(
        "step_name",
        ["DIAGNOSE", "PLAN", "SCOPE CHECK", "EXECUTE", "VERIFY"],
    )
    def test_protocol_step_present(self, prompt_text, step_name):
        assert step_name in prompt_text, (
            f"REMEDIATION PROTOCOL step '{step_name}' must appear in the prompt"
        )

    def test_scope_boundary_server_data_dir_present(self, prompt_text):
        """SCOPE CHECK must reference server_data_dir as a boundary."""
        # After variable substitution the resolved runtime path appears; also
        # check the residual string in case this is a dev environment with
        # the path rendered.  Either the template variable name survives in
        # the scope description OR the rendered path (which always includes
        # ".cidx-server") is present.
        has_boundary = "server_data_dir" in prompt_text or ".cidx-server" in prompt_text
        assert has_boundary, (
            "SCOPE CHECK boundary must reference server_data_dir or .cidx-server"
        )

    def test_scope_boundary_golden_repos_dir_present(self, prompt_text):
        """SCOPE CHECK must reference golden_repos_dir as a boundary."""
        has_boundary = (
            "golden_repos_dir" in prompt_text or "golden-repos" in prompt_text
        )
        assert has_boundary, (
            "SCOPE CHECK boundary must reference golden_repos_dir or golden-repos"
        )


# ---------------------------------------------------------------------------
# 3. SELF-DIAGNOSED vs OPERATOR-DIRECTED guard
# ---------------------------------------------------------------------------


class TestSelfVsOperatorDirectedGuard:
    def test_has_self_diagnosed_section(self, prompt_text_normalized):
        assert "SELF-DIAGNOSED" in prompt_text_normalized.upper(), (
            "Prompt must contain a SELF-DIAGNOSED section/label"
        )

    def test_has_operator_directed_section(self, prompt_text_normalized):
        assert "OPERATOR-DIRECTED" in prompt_text_normalized.upper(), (
            "Prompt must contain an OPERATOR-DIRECTED section/label"
        )

    def test_prompt_injection_data_source_language(self, prompt_text_normalized):
        """
        Prompt must reference the hostile content sources: logs, file uploads,
        or DB rows (or generic 'data you are analyzing') as vectors for
        adversarial/injected instructions.
        """
        lower = prompt_text_normalized.lower()
        has_data_source_reference = any(
            phrase in lower
            for phrase in (
                "log content",
                "file upload",
                "database row",
                "db row",
                "data you are analyzing",
                "data being analyzed",
                "log line",
            )
        )
        assert has_data_source_reference, (
            "Prompt must name at least one hostile content source "
            "(log content / file upload / database rows / data you are analyzing). "
            "Got (first 1000 chars): " + prompt_text_normalized[:1000]
        )

    def test_prompt_injection_adversarial_language(self, prompt_text_normalized):
        """
        Prompt must use adversarial/prompt-injection framing.
        """
        lower = prompt_text_normalized.lower()
        has_adversarial = any(
            phrase in lower
            for phrase in (
                "adversarial",
                "prompt injection",
                "prompt-injection",
            )
        )
        assert has_adversarial, (
            "Prompt must contain adversarial / prompt injection language. "
            "Got (first 500 chars): " + prompt_text_normalized[:500]
        )

    def test_prompt_injection_non_obedience_rule(self, prompt_text_normalized):
        """
        Prompt must explicitly state that embedded instructions in data must
        NOT be obeyed / followed.
        """
        lower = prompt_text_normalized.lower()
        has_non_obedience = any(
            phrase in lower
            for phrase in (
                "never obey instructions found",
                "do not obey",
                "must not obey",
                "data is evidence, not commands",
                "data are evidence",
                "not commands",
            )
        )
        assert has_non_obedience, (
            "Prompt must state that instructions embedded in data must not be obeyed. "
            "Got (first 1000 chars): " + prompt_text_normalized[:1000]
        )


# ---------------------------------------------------------------------------
# 4. Softer reason-category disclosure (two independent assertions)
# ---------------------------------------------------------------------------


class TestDoNotExplainDirectiveSoftened:
    def test_old_hard_ban_phrase_absent(self, prompt_text_normalized):
        """
        The hardline 'DO NOT explain WHY you cannot perform an action' must
        be removed entirely (not merely accompanied by softer language).
        """
        assert (
            "DO NOT explain WHY you cannot perform an action"
            not in prompt_text_normalized
        ), (
            "Old hardline directive 'DO NOT explain WHY you cannot perform an action' "
            "must be removed from the prompt"
        )

    def test_reason_category_disclosure_present(self, prompt_text_normalized):
        """
        Softer reason-category disclosure language must be present so the
        admin can understand what category of refusal applies.
        """
        assert "reason category" in prompt_text_normalized.lower(), (
            "Prompt must contain 'reason category' disclosure language "
            "to replace the old hard-ban directive"
        )


# ---------------------------------------------------------------------------
# 5. Source-code-edit prohibition outside cidx-meta preserved (with carveout)
# ---------------------------------------------------------------------------


class TestSourceCodeEditProhibitionPreserved:
    def test_must_not_edit_source_phrase(self, prompt_text_normalized):
        assert "You MUST NOT edit, write, patch, or modify" in prompt_text_normalized, (
            "Source-code edit prohibition phrase must be preserved"
        )

    def test_source_tree_phrase(self, prompt_text_normalized):
        assert (
            "source files under the application's source tree" in prompt_text_normalized
        ), "Phrase referencing the application's source tree must be preserved"

    def test_cidx_meta_carveout_still_present(self, prompt_text_normalized):
        """
        The cidx-meta directory is the one allowed exception to the source-edit
        prohibition.  It must still be named as an allowed write target so the
        RA knows it can write repo descriptions and dependency maps there.
        """
        assert "cidx-meta" in prompt_text_normalized, (
            "cidx-meta carveout must be preserved in the source-code prohibition "
            "section so the RA knows it is allowed to write inside that directory"
        )


# ---------------------------------------------------------------------------
# 6. GITHUB BUG REPORT CREATION section preserved
# ---------------------------------------------------------------------------


class TestGithubBugReportSectionPreserved:
    def test_github_bug_report_section_header_present(self, prompt_text):
        assert "GITHUB BUG REPORT CREATION" in prompt_text, (
            "GITHUB BUG REPORT CREATION section header must be preserved"
        )

    def test_issue_manager_reference_present(self, prompt_text):
        assert "issue_manager.py" in prompt_text, (
            "issue_manager.py reference must be preserved in prompt"
        )


# ---------------------------------------------------------------------------
# 7. Template variable substitution — general assertion
# ---------------------------------------------------------------------------


class TestTemplateVariablesResolved:
    """
    All {variable} placeholders must be substituted by load_research_prompt().

    Two complementary checks:
    a) A general regex scan for any remaining {identifier} token ensures that
       newly added variables are never missed.
    b) A parametrized check for each expected variable confirms they each
       appear resolved (belt-and-suspenders against the general scan missing
       edge cases such as escaping or deliberate literal braces).
    """

    def test_no_unresolved_placeholders_general(self, prompt_text):
        """No {word} template tokens must remain after rendering."""
        # Allow literal curly braces inside code blocks (SQL, bash) — we only
        # care about plain Python str.format()-style {identifier} tokens.
        remaining = re.findall(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}", prompt_text)
        assert remaining == [], (
            f"Unresolved template placeholders found in rendered prompt: {remaining}"
        )

    @pytest.mark.parametrize(
        "placeholder",
        [
            "{hostname}",
            "{server_version}",
            "{db_path}",
            "{cidx_repo_root}",
            "{server_data_dir}",
            "{golden_repos_dir}",
            "{service_name}",
        ],
    )
    def test_known_placeholder_substituted(self, prompt_text, placeholder):
        assert placeholder not in prompt_text, (
            f"Template placeholder {placeholder!r} was not substituted in rendered prompt"
        )
