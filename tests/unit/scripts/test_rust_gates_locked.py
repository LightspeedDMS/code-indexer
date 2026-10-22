"""Both Rust gates (CI rust job, rust-automation.sh) build with --locked.

A manifest change without a matching Cargo.lock update must fail before
merge, not at deploy time (the VM auto-updater builds xray-cli --locked).
"""

import re
from pathlib import Path
from typing import List

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CARGO_GATE = re.compile(r"\bcargo (test|clippy)\b(?!\s+--version)")


def _cargo_gate_commands(lines: List[str]) -> List[str]:
    return [line for line in lines if _CARGO_GATE.search(line)]


def test_ci_rust_job_cargo_commands_are_locked() -> None:
    workflow = (_REPO_ROOT / ".github" / "workflows" / "main.yml").read_text()
    run_lines = [
        line.strip()
        for line in workflow.splitlines()
        if line.strip().startswith("run:")
    ]
    commands = _cargo_gate_commands(run_lines)
    assert len(commands) == 2, commands
    for command in commands:
        assert "--locked" in command.split(), command


def test_rust_automation_cargo_commands_are_locked() -> None:
    script = (_REPO_ROOT / "rust-automation.sh").read_text()
    executed = [
        line.strip()
        for line in script.splitlines()
        if line.strip().startswith("(cd rust &&")
    ]
    commands = _cargo_gate_commands(executed)
    assert len(commands) == 2, commands
    for command in commands:
        assert "--locked" in command.split(), command
