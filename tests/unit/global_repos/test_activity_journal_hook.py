"""
Unit tests for Bug #838 — journal silence fix.

Tests that _invoke_claude_cli generates a PostToolUse hook bash script that:
1. Appends narrative **claude-tool** entries to the journal on every tool call.
2. Emits a STATUS NUDGE to stdout every 10th tool call.
3. Does NOT add journal-writing logic (JRNL= variable, claude-tool entries)
   when journal_path is None — the existing turn-counter hook is preserved as-is.

Patching: only _invoke_claude_cli's subprocess.run call is patched (so no real
Claude CLI process runs). The generated hook bash script is executed as a real
bash subprocess to verify its runtime behavior.

Per Bug #838 spec: "This change is purely additive. Existing WARNING/CRITICAL
threshold messages are preserved unchanged." The turn-counter hook is always
generated when post_tool_hook is provided; journal writing is only added on top
of that when journal_path is provided.

These 4 tests are strictly red until _invoke_claude_cli gains the journal_path
kwarg and hook narrative/nudge logic (Bug #838).
"""

import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from unittest.mock import patch

from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer

_SUBPROCESS_PATH = "code_indexer.global_repos.dependency_map_analyzer.subprocess.run"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_analyzer(tmp_path: Path) -> DependencyMapAnalyzer:
    (tmp_path / "golden-repos").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cidx-meta").mkdir(parents=True, exist_ok=True)
    return DependencyMapAnalyzer(
        golden_repos_root=tmp_path / "golden-repos",
        cidx_meta_path=tmp_path / "cidx-meta",
        pass_timeout=60,
    )


def _ok_result() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="done", stderr="")


def _capture_post_tool_hooks(
    tmp_path: Path,
    journal_path: Path,
    max_turns: int = 50,
) -> list:
    """Return the PostToolUse hook list from the --settings JSON, or []."""
    analyzer = _make_analyzer(tmp_path)
    captured: list = []

    with patch(
        _SUBPROCESS_PATH,
        side_effect=lambda *a, **k: (captured.extend(a[0]), _ok_result())[1],
    ):
        analyzer._invoke_claude_cli(
            prompt="test",
            timeout=30,
            max_turns=max_turns,
            post_tool_hook="reminder",
            journal_path=journal_path,
        )

    for i, arg in enumerate(captured):
        if arg == "--settings" and i + 1 < len(captured):
            return json.loads(captured[i + 1]).get("hooks", {}).get("PostToolUse", [])
    return []


def _generate_hook_script(
    tmp_path: Path,
    journal_path: Path,
    max_turns: int = 50,
) -> str:
    """Return the inner bash script from the PostToolUse hook, or empty string."""
    hooks = _capture_post_tool_hooks(tmp_path, journal_path, max_turns)
    if not hooks:
        return ""
    cmd = hooks[0].get("command", "")
    if cmd.startswith("bash -c "):
        return shlex.split(cmd[len("bash -c ") :])[0]
    return ""


def _rewrite_hook_paths(script: str, counter: Path, journal: Path) -> str:
    """Replace embedded counter (F=) and journal (JRNL=) paths.

    Uses re.MULTILINE so ^ anchors to each line, not just the string start.
    """
    script = re.sub(
        r"^F='[^']*'",
        f"F={shlex.quote(str(counter))}",
        script,
        flags=re.MULTILINE,
    )
    return re.sub(r"JRNL='[^']*'", f"JRNL={shlex.quote(str(journal))}", script)


def _run_hook(
    script: str,
    tool_name: str,
    tool_input: str,
    tmp_path: Path,
) -> subprocess.CompletedProcess:
    """Execute hook bash script with given Claude tool env vars.

    Inherits PATH from os.environ so bash can locate utilities on any system.
    """
    env = {
        **os.environ,
        "CLAUDE_TOOL_NAME": tool_name,
        "CLAUDE_TOOL_INPUT": tool_input,
        "HOME": str(tmp_path),
    }
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env
    )


def _prep(tmp_path: Path) -> tuple:
    """Return (script, counter_path, journal_path) ready for execution tests."""
    journal = tmp_path / "activity.md"
    journal.write_text("")
    counter = tmp_path / "hook_counter.cnt"
    counter.write_text("0")
    raw = _generate_hook_script(tmp_path, journal_path=journal)
    script = _rewrite_hook_paths(raw, counter=counter, journal=journal)
    return script, counter, journal


# ---------------------------------------------------------------------------
# Test 1: Hook script generated when journal_path provided
# ---------------------------------------------------------------------------


class TestHookScriptGeneratedWithJournalPath:
    def test_hook_script_generated_when_journal_path_provided(
        self, tmp_path: Path
    ) -> None:
        journal = tmp_path / "fake_activity.md"
        journal.write_text("")
        script = _generate_hook_script(tmp_path, journal_path=journal)

        assert script, "Expected non-empty hook script when journal_path provided"
        assert str(journal) in script, (
            f"Script must reference journal path.\nScript: {script!r}"
        )
        assert "claude-tool" in script, (
            f"Script must write **claude-tool** entries.\nScript: {script!r}"
        )


# ---------------------------------------------------------------------------
# Test 2: No journal-writing additions when journal_path is None
# ---------------------------------------------------------------------------


class TestHookScriptNoJournalLogicWhenPathNone:
    def test_hook_script_omitted_when_journal_path_none(self, tmp_path: Path) -> None:
        """When journal_path=None, the existing turn-counter hook is preserved
        as-is (per Bug #838: purely additive change). The hook command must NOT
        be augmented with journal-writing logic: no JRNL= variable, no claude-tool."""
        analyzer = _make_analyzer(tmp_path)
        captured: list = []

        with patch(
            _SUBPROCESS_PATH,
            side_effect=lambda *a, **k: (captured.extend(a[0]), _ok_result())[1],
        ):
            analyzer._invoke_claude_cli(
                prompt="test",
                timeout=30,
                max_turns=5,
                post_tool_hook="reminder",
                journal_path=None,
            )

        hook_cmd = ""
        for i, arg in enumerate(captured):
            if arg == "--settings" and i + 1 < len(captured):
                hooks = (
                    json.loads(captured[i + 1]).get("hooks", {}).get("PostToolUse", [])
                )
                if hooks:
                    hook_cmd = hooks[0].get("command", "")

        assert "JRNL=" not in hook_cmd, (
            f"Hook must NOT contain JRNL= assignment when journal_path=None.\nCmd: {hook_cmd!r}"
        )
        assert "claude-tool" not in hook_cmd, (
            f"Hook must NOT write **claude-tool** entries when journal_path=None.\nCmd: {hook_cmd!r}"
        )


# ---------------------------------------------------------------------------
# Test 3: Every-10-tool-use nudge via counter file
# ---------------------------------------------------------------------------


class TestHookCounterIncrementsAcrossCalls:
    def test_hook_counter_increments_across_calls(self, tmp_path: Path) -> None:
        script, _counter, journal = _prep(tmp_path)
        assert script, "Expected a hook script"

        outputs = [
            _run_hook(script, "Read", '{"file_path": "/tmp/x.py"}', tmp_path).stdout
            for _ in range(11)
        ]

        assert "STATUS NUDGE" in outputs[9], (
            f"STATUS NUDGE must appear at 10th call.\nGot: {outputs[9]!r}"
        )
        assert "STATUS NUDGE" not in outputs[10], (
            f"STATUS NUDGE must NOT appear at 11th call.\nGot: {outputs[10]!r}"
        )
        assert "claude-tool" in journal.read_text(), (
            "Journal must contain **claude-tool** entries after hook runs"
        )


# ---------------------------------------------------------------------------
# Test 4: Tool-type narrative templates (all cases in one test method)
# ---------------------------------------------------------------------------


class TestHookNarrativeForEachToolType:
    def test_hook_narrative_for_each_tool_type(self, tmp_path: Path) -> None:
        """Loop over tool types so exactly 1 test method is collected (not 7)."""
        tool_cases = [
            ("Read", '{"file_path": "src/foo/bar.py"}', "Claude read file"),
            ("Bash", '{"command": "git log --oneline -20"}', "Claude ran bash"),
            ("Grep", '{"pattern": "def foo", "path": "src/"}', "Claude searched"),
            ("Glob", '{"pattern": "**/*.py"}', "Claude listed files"),
            ("Write", '{"file_path": "/output/domain.md"}', "Claude wrote file"),
            ("Edit", '{"file_path": "/output/domain.md"}', "Claude wrote file"),
            ("mcp__cidx-local__search_code", '{"query_text": "dep"}', "Claude ran"),
        ]
        for tool_name, tool_input, expected_fragment in tool_cases:
            journal = tmp_path / f"activity_{tool_name}.md"
            journal.write_text("")
            counter = tmp_path / f"counter_{tool_name}.cnt"
            counter.write_text("0")

            raw = _generate_hook_script(tmp_path, journal_path=journal)
            assert raw, f"Expected hook script for tool {tool_name}"
            script = _rewrite_hook_paths(raw, counter=counter, journal=journal)

            result = _run_hook(script, tool_name, tool_input, tmp_path)
            content = journal.read_text()

            assert expected_fragment in content, (
                f"Journal must contain '{expected_fragment}' for '{tool_name}'.\n"
                f"Journal: {content!r}\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
            )
            assert "claude-tool" in content, (
                f"Journal must contain **claude-tool** for '{tool_name}'.\nJournal: {content!r}"
            )
