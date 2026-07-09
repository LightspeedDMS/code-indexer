"""
Unit tests for Bug #1102 — timeless-snapshot voice enforcement.

Live staging fault-injection showed the refreshed description contained
change-relative phrasing ("Recent code also enforces...").  The root cause
is that the refresh addendum scopes verification via git log --since, which
primes the model to narrate findings as "recent" changes.  Neither prompt
explicitly banned temporal / change-relative language.

Fix: both lifecycle_refresh_addendum.md and lifecycle_unified.md must contain
a TIMELESS SNAPSHOT instruction that:
  - Bans temporal and change-relative phrasing (examples: "recent", "recently",
    "previously", "no longer", etc.)
  - States that git log --since is a verification-BUDGET tool only and must
    never surface in the output voice.

These are content-guard tests (not behavioural LLM tests) — they assert that
the instruction text is present in the prompt files, protecting against prompt
regression.  Same pattern as test_addendum_contains_hallucination_removal_instruction
in test_lifecycle_frontmatter_preserve_1101.py.

Test inventory:
  1. test_addendum_contains_timeless_snapshot_rule
     lifecycle_refresh_addendum.md must contain the timeless-snapshot instruction
     with "timeless" keyword AND at least 3 banned-term examples.
  2. test_unified_prompt_contains_timeless_snapshot_instruction
     lifecycle_unified.md must contain the timeless-snapshot instruction.
  3. test_addendum_change_scoping_paragraph_reinforces_verification_budget_only
     The change-scoping paragraph in the addendum must contain a sentence
     clarifying that the change window is for verification budget only and must
     never appear in the output voice.

No mocks — pure file-content assertions (Messi Rule #1).
"""

from __future__ import annotations

from pathlib import Path

_ADDENDUM_PATH: Path = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "code_indexer"
    / "server"
    / "prompts"
    / "lifecycle_refresh_addendum.md"
)

_UNIFIED_PATH: Path = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "code_indexer"
    / "server"
    / "prompts"
    / "lifecycle_unified.md"
)


# ---------------------------------------------------------------------------
# 1. Addendum: timeless-snapshot rule with banned-term examples
# ---------------------------------------------------------------------------


def test_addendum_contains_timeless_snapshot_rule() -> None:
    """
    lifecycle_refresh_addendum.md must contain an explicit TIMELESS SNAPSHOT
    instruction.

    Required presence (case-insensitive):
      - The word "timeless" (signals the timeless-snapshot concept)
      - At least 3 of the following banned-term examples in an instruction
        context: "recent", "recently", "previously", "no longer", "formerly",
        "newly", "was added", "has been"
    """
    assert _ADDENDUM_PATH.exists(), (
        f"lifecycle_refresh_addendum.md not found at {_ADDENDUM_PATH}"
    )
    text = _ADDENDUM_PATH.read_text(encoding="utf-8")
    lower = text.lower()

    assert "timeless" in lower, (
        "lifecycle_refresh_addendum.md must contain the word 'timeless' as part "
        "of the timeless-snapshot voice instruction (not found)"
    )

    # At least 3 banned-term examples must be present to confirm the rule gives
    # concrete guidance (not just vague "avoid temporal language").
    banned_terms = [
        "recent",
        "recently",
        "previously",
        "no longer",
        "formerly",
        "newly",
        "was added",
        "has been",
    ]
    found = [term for term in banned_terms if term in lower]
    assert len(found) >= 3, (
        "lifecycle_refresh_addendum.md must list at least 3 banned temporal/change-relative "
        f"term examples in the timeless-snapshot instruction. "
        f"Found: {found}. "
        f"Expected at least 3 of: {banned_terms}"
    )


# ---------------------------------------------------------------------------
# 2. Unified prompt: timeless-snapshot instruction
# ---------------------------------------------------------------------------


def test_unified_prompt_contains_timeless_snapshot_instruction() -> None:
    """
    lifecycle_unified.md (create mode) must also contain the timeless-snapshot
    instruction, because create-mode descriptions can drift the same way.

    Required presence (case-insensitive): the word "timeless".
    """
    assert _UNIFIED_PATH.exists(), f"lifecycle_unified.md not found at {_UNIFIED_PATH}"
    text = _UNIFIED_PATH.read_text(encoding="utf-8")
    lower = text.lower()

    assert "timeless" in lower, (
        "lifecycle_unified.md must contain the word 'timeless' as part of the "
        "timeless-snapshot voice instruction for create mode (not found)"
    )


# ---------------------------------------------------------------------------
# 3. Addendum change-scoping paragraph: verification-budget-only reinforcement
# ---------------------------------------------------------------------------


def test_addendum_change_scoping_paragraph_reinforces_verification_budget_only() -> (
    None
):
    """
    The Change-scoping paragraph in lifecycle_refresh_addendum.md must contain
    the load-bearing reinforcement sentence asserting that:
      - The git log --since window is for VERIFICATION BUDGET only
      - It must NEVER surface in the description's output voice.

    Assertions are scoped to the Change-scoping paragraph (between the
    "**Change-scoping" and "**Audience" headings) so that the test fails if
    the reinforcement sentence is deleted from that paragraph, even if similar
    words remain elsewhere in the file.

    Required presence inside the Change-scoping paragraph (case-insensitive):
      - "verification" — the budget-only framing
      - "never surface" — the load-bearing clause added in Bug #1102
      - "voice" or "output" — the output-voice reference
    """
    assert _ADDENDUM_PATH.exists(), (
        f"lifecycle_refresh_addendum.md not found at {_ADDENDUM_PATH}"
    )
    text = _ADDENDUM_PATH.read_text(encoding="utf-8")

    assert "**Change-scoping" in text, (
        "lifecycle_refresh_addendum.md must contain a '**Change-scoping' heading "
        "so the paragraph can be isolated for mutation-resistant assertions"
    )
    assert "**Audience" in text, (
        "lifecycle_refresh_addendum.md must contain a '**Audience' heading "
        "so the Change-scoping paragraph boundary can be determined"
    )

    # Extract only the Change-scoping paragraph text.
    scoping = text.split("**Change-scoping")[1].split("**Audience")[0].lower()

    assert "verification" in scoping, (
        "The Change-scoping paragraph must contain 'verification' to reinforce "
        "that git log --since is a verification-budget tool only. "
        "Not found in the Change-scoping paragraph."
    )
    assert "never surface" in scoping, (
        "The Change-scoping paragraph must contain the clause 'never surface' "
        "(the load-bearing Bug #1102 reinforcement sentence). "
        "Not found in the Change-scoping paragraph."
    )
    assert "voice" in scoping or "output" in scoping, (
        "The Change-scoping paragraph must reference 'voice' or 'output' to "
        "clarify that the change window must not appear in the output voice. "
        "Neither found in the Change-scoping paragraph."
    )
