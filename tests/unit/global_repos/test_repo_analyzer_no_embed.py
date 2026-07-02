"""
Bug #840 Site #5: repo_analyzer._get_refresh_prompt must not embed existing description.

Design principle: the refresh prompt must reference a temp file path, not inline the
existing repo description. Claude is instructed to Read the file and Edit it in place.

Tests:
1. test_refresh_prompt_does_not_embed_existing_description
   — with a large distinctive description, assert it does NOT appear inline in the prompt
2. test_refresh_prompt_references_file_workflow
   — assert the prompt instructs Read+Edit with the exact temp file path and a specific
     phrase prohibiting stdout output of the full document
3. test_refresh_flow_writes_existing_description_to_temp_file
   — scheduler._get_refresh_prompt stages description to a temp file; verify through
     observable outputs only: canary not inline, prompt contains known tmp_path prefix
     (platform-neutral), file at that path contains the canary
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.global_repos.repo_analyzer import RepoAnalyzer

# ---------------------------------------------------------------------------
# Canary marker — must be distinctive and unlikely to appear in any prompt
# ---------------------------------------------------------------------------
_DESCRIPTION_CANARY = (
    "UNIQUE_CANARY_EXISTING_DESCRIPTION_MUST_NOT_APPEAR_IN_PROMPT_SITE5_XYZ999"
)

_LAST_ANALYZED = "2025-01-01T00:00:00Z"

# Large description: canary + padding to exceed typical context budgets (~4 KB)
_LARGE_DESCRIPTION = (
    "---\nname: test-repo\nlast_analyzed: 2025-01-01T00:00:00Z\n---\n"
    + f"{_DESCRIPTION_CANARY}\n"
    + "Some existing analysis content.\n" * 200
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def analyzer_refresh_prompt(tmp_path):
    """Return (prompt, temp_file) from RepoAnalyzer for a large description."""
    temp_file = tmp_path / "existing_desc.md"
    temp_file.write_text(_LARGE_DESCRIPTION)
    analyzer = RepoAnalyzer(str(tmp_path))
    prompt = analyzer.get_prompt(
        mode="refresh",
        last_analyzed=_LAST_ANALYZED,
        temp_file_path=temp_file,
    )
    return prompt, temp_file


@pytest.fixture()
def scheduler_prompt(tmp_path):
    """Return (prompt, meta_dir, tmp_path) from scheduler._get_refresh_prompt."""
    from code_indexer.server.services.description_refresh_scheduler import (
        DescriptionRefreshScheduler,
    )

    meta_dir = tmp_path / "cidx-meta"
    meta_dir.mkdir()
    (meta_dir / "my-repo.md").write_text(
        f"---\nname: my-repo\nlast_analyzed: {_LAST_ANALYZED}\n---\n"
        f"{_DESCRIPTION_CANARY}\nExisting body text.\n"
    )

    config_manager = MagicMock()
    config_manager.load_config.return_value = None
    tracking_backend = MagicMock()
    tracking_backend.close = MagicMock()
    golden_backend = MagicMock()
    golden_backend.close = MagicMock()

    scheduler = DescriptionRefreshScheduler(
        config_manager=config_manager,
        tracking_backend=tracking_backend,
        golden_backend=golden_backend,
    )
    scheduler._meta_dir = meta_dir
    prompt = scheduler._get_refresh_prompt("my-repo", str(tmp_path))
    return prompt, tmp_path


# ---------------------------------------------------------------------------
# Tests — single class, three focused assertions
# ---------------------------------------------------------------------------


class TestRefreshPromptNoEmbed:
    """Verify that the refresh workflow does not embed existing description inline."""

    def test_refresh_prompt_does_not_embed_existing_description(
        self, analyzer_refresh_prompt
    ):
        """Site #5: Canary content must NOT appear inline in the refresh prompt."""
        prompt, _ = analyzer_refresh_prompt
        assert isinstance(prompt, str)
        assert _DESCRIPTION_CANARY not in prompt, (
            "Existing description canary found inline in refresh prompt — "
            "content must be referenced via file path, not embedded."
        )

    def test_refresh_prompt_references_file_workflow(self, analyzer_refresh_prompt):
        """Site #5: Prompt must reference exact file path and instruct Read+Edit."""
        prompt, temp_file = analyzer_refresh_prompt
        assert str(temp_file) in prompt, (
            f"Exact temp file path '{temp_file}' must appear in the refresh prompt."
        )
        assert "read" in prompt.lower(), "Prompt must instruct Claude to Read the file."
        assert "edit" in prompt.lower(), "Prompt must instruct Claude to Edit in place."
        # Assert a specific prohibition phrase against stdout output
        assert "do not output" in prompt.lower() or "not output" in prompt.lower(), (
            "Prompt must contain a specific phrase prohibiting stdout output of the "
            "full document (e.g. 'Do NOT output the full document to stdout')."
        )

    def test_refresh_flow_writes_existing_description_to_temp_file(
        self, scheduler_prompt
    ):
        """
        Scheduler stages description to a temp file referenced in the prompt.

        Verified through observable outputs only (no internal spying):
        - The returned prompt must not contain the canary inline.
        - The prompt must contain the known tmp_path prefix (platform-neutral path check).
        - At least one file under tmp_path referenced by the prompt must contain canary.
        """
        prompt, tmp_path = scheduler_prompt

        assert prompt is not None, "_get_refresh_prompt must return a prompt string."
        assert _DESCRIPTION_CANARY not in prompt, (
            "Existing description canary found inline in scheduler refresh prompt."
        )

        # Platform-neutral: the prompt must reference something under tmp_path
        tmp_str = str(tmp_path)
        assert tmp_str in prompt, (
            f"Prompt must reference a path under '{tmp_str}' (the staged temp file). "
            f"Prompt snippet: {prompt[:300]}"
        )

        # Find the staged file: any Path under tmp_path mentioned in the prompt
        # Extract by splitting the prompt and checking each token
        staged_path = None
        for token in prompt.split():
            candidate = Path(token.strip("\"'"))
            if str(candidate).startswith(tmp_str) and candidate.exists():
                staged_path = candidate
                break

        assert staged_path is not None, (
            f"Could not find an existing staged file under '{tmp_str}' in the prompt. "
            f"Prompt snippet: {prompt[:300]}"
        )
        assert _DESCRIPTION_CANARY in staged_path.read_text(), (
            f"Staged temp file '{staged_path}' must contain the original description "
            "canary."
        )
