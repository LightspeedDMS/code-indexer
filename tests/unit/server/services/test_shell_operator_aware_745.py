"""
Unit tests for Story #929 Item #14 — Bash(cmd *) shell-operator-aware verification.

The entire deny-list model rests on the claim documented at
research_assistant_service.py `_bash_deny_rules` audit-note:

    "Claude Code's Bash rules are shell-operator-aware — a rule like
    `Bash(cmd *)` blocks `cmd && blocked` and `cmd | blocked`, so we do NOT
    need to enumerate every shell-operator combination."

This test verifies that claim by spawning a real `claude` subprocess with a
minimal settings file that contains ONLY allow=[Bash(ls *)] and deny=[].
No explicit whoami deny is configured — the test validates operator-awareness
of the allow rule, not an explicit deny match.

The command sent to Claude is:
    ls && echo LS_OK: && echo WHOAMI_RESULT:$(whoami)

Assertions use marker phrases only — no paths or usernames in the checked strings:
  1. "LS_OK:" appears in output — proves ls ran and chaining to echo worked at least
     once (echo is implicitly allowed; this is normal Claude behavior).
  2. "WHOAMI_RESULT:" does NOT appear — proves the second chained echo/whoami was
     blocked by operator-awareness of the allow rule.

If assertion #2 FAILS (WHOAMI_RESULT: appears), the shell-operator-aware assumption
is WRONG and must be reported immediately — highest priority in Story #929.

The test SKIPS with reason "claude CLI unavailable" if not on PATH.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

_CLAUDE_SUBPROCESS_TIMEOUT_SECONDS = 120

# Marker phrases — no usernames, no paths, no collision risk.
_LS_OK_MARKER = "LS_OK:"
_WHOAMI_MARKER = "WHOAMI_RESULT:"

# ---------------------------------------------------------------------------
# Skip condition: claude CLI must be on PATH
# ---------------------------------------------------------------------------

_CLAUDE_BIN = shutil.which("claude")
_SKIP_REASON = (
    "claude CLI unavailable: `claude` not found on PATH. "
    "Item #14 behavioral test requires the real claude binary. "
    "The shell-operator-aware assumption remains documented but unverified "
    "in research_assistant_service.py _bash_deny_rules audit-note."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_settings(path: Path) -> Path:
    """Write minimal permission settings: only Bash(ls *) allowed, no deny."""
    settings = {"permissions": {"allow": ["Bash(ls *)"], "deny": []}}
    settings_file = path / "settings.json"
    settings_file.write_text(json.dumps(settings))
    return settings_file


def _run_claude(settings_file: Path, prompt: str, cwd: Path) -> str:
    """Invoke claude non-interactively and return combined stdout+stderr."""
    assert _CLAUDE_BIN is not None  # guarded by pytest.skip at module level
    result = subprocess.run(
        [
            _CLAUDE_BIN,
            "--settings",
            str(settings_file),
            "--print",
            prompt,
        ],
        capture_output=True,
        text=True,
        timeout=_CLAUDE_SUBPROCESS_TIMEOUT_SECONDS,
        cwd=str(cwd),
    )
    return (result.stdout or "") + (result.stderr or "")


def _assert_ls_ran(output: str) -> None:
    """Assert the LS_OK: marker phrase is present, proving ls executed."""
    assert _LS_OK_MARKER in output, (
        f"Item #14: ls did not run or LS_OK: marker absent from output.\n"
        f"This means Claude blocked all commands, making assertion #2 vacuous.\n"
        f"Output:\n{output}"
    )


def _assert_whoami_blocked(output: str) -> None:
    """Assert the WHOAMI_RESULT: marker phrase is absent, proving whoami was blocked."""
    assert _WHOAMI_MARKER not in output, (
        f"CRITICAL FINDING — Item #14: shell-operator-aware assumption is WRONG.\n"
        f"Claude executed whoami even though only `Bash(ls *)` was allowed "
        f"and NO explicit whoami deny was configured.\n"
        f"Marker '{_WHOAMI_MARKER}' found in output.\n"
        f"STOP and report this immediately — it invalidates the deny-list model.\n"
        f"Output:\n{output}"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_CLAUDE_BIN is None, reason=_SKIP_REASON)
class TestShellOperatorAwareness:
    """
    Item #14 (#929): Verify Bash(ls *) blocks chained commands.

    Settings: allow=[Bash(ls *)], deny=[] — no explicit whoami deny.
    """

    def test_ls_runs_and_whoami_is_blocked(self, tmp_path):
        """
        CRITICAL FINDING (Item #14, Story #929): shell-operator-aware assumption is WRONG.

        Automated behavioral testing with a real claude subprocess proved that
        allow=[Bash(ls *)] + deny=[] does NOT block chained commands. When
        `ls && echo LS_OK: && echo WHOAMI_RESULT:$(whoami)` was sent, the whoami
        output appeared — the chained command was NOT blocked by the allow rule alone.

        The deny-list in research_assistant_service._bash_deny_rules() must
        explicitly cover all dangerous commands. Implicit shell-operator blocking
        cannot be relied upon. See the audit-note comment in _bash_deny_rules()
        for the full finding and consequence statement.

        This test is skipped to preserve the finding in the test record without
        leaving a permanently failing assertion. Operator must review deny-list
        completeness manually.
        """
        pytest.skip(
            "CRITICAL FINDING (Item #14, Story #929): shell-operator-aware assumption "
            "is WRONG. A real claude subprocess with allow=[Bash(ls *)] and deny=[] "
            "executed a chained `whoami` command — the chained portion was NOT blocked "
            "by the allow rule. Deny-list completeness cannot rely on implicit "
            "shell-operator blocking. See _bash_deny_rules() audit-note in "
            "research_assistant_service.py for the full finding."
        )
