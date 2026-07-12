"""Unit tests for CLI subprocess color-forcing env sanitization (Bug #1372).

Bug #1372: ``e2e_cli_env`` (tests/e2e/conftest.py) and its sibling CLI-env
builders (tests/e2e/phase5_resiliency/conftest.py::_build_cli_env,
tests/e2e/phase5_resiliency/test_positive_control.py::_build_test_cli_env)
blindly copy the FULL ambient environment (``dict(os.environ)``) into every
``cidx`` CLI subprocess invocation. When the shell/harness that invokes
pytest has ``FORCE_COLOR`` set (e.g. an interactive Claude Code session),
Rich (the CLI's console library) honors it even on a captured/piped stdout,
wrapping output in real ANSI escape codes and breaking plain-text-output
assertions such as ``test_02_query.py::test_query_limit``'s
``re.findall(r"^\\d+\\.\\s", output, re.MULTILINE)``.

This suite validates the ``sanitize_cli_subprocess_env`` helper in
``tests/e2e/helpers.py`` (pure-function contract) plus a real-subprocess
regression test proving the actual ``cidx`` CLI produces color-free stdout
when spawned with a sanitized environment, even when the parent (pytest)
process has ``FORCE_COLOR`` set ambiently -- the exact reproduction
condition from the bug report.

No mocking -- real dict operations and a real
``python3 -m code_indexer.cli`` child process for the regression test.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.e2e.helpers import sanitize_cli_subprocess_env


# ---------------------------------------------------------------------------
# Pure-function unit tests
# ---------------------------------------------------------------------------


def test_sanitize_removes_force_color_key() -> None:
    """FORCE_COLOR must be removed entirely, not merely reassigned.

    Rich's Console.is_terminal treats ANY non-empty FORCE_COLOR value
    (including "0") as "force terminal on" -- only an absent key or an
    empty-string value falls through to real TTY auto-detection. The fix
    must therefore pop the key, not set it to "0".
    """
    env = {"FORCE_COLOR": "3", "PATH": "/usr/bin"}
    sanitized = sanitize_cli_subprocess_env(env)
    assert "FORCE_COLOR" not in sanitized


def test_sanitize_removes_force_color_when_value_is_zero() -> None:
    """A pre-existing FORCE_COLOR=0 in the source env must also be stripped.

    Guards against a regression where a naive implementation only pops
    non-"0" values and leaves an explicit "0" untouched.
    """
    env = {"FORCE_COLOR": "0"}
    sanitized = sanitize_cli_subprocess_env(env)
    assert "FORCE_COLOR" not in sanitized


def test_sanitize_sets_no_color_flag() -> None:
    """NO_COLOR=1 is set as a defense-in-depth signal for other color-aware
    tooling (e.g. click, colorama-based libraries) invoked transitively by
    the cidx subprocess."""
    env = {"PATH": "/usr/bin"}
    sanitized = sanitize_cli_subprocess_env(env)
    assert sanitized["NO_COLOR"] == "1"


def test_sanitize_does_not_mutate_input() -> None:
    """The source env dict passed in must be left untouched (pure function)."""
    env = {"FORCE_COLOR": "3", "PATH": "/usr/bin"}
    original = dict(env)
    sanitize_cli_subprocess_env(env)
    assert env == original


def test_sanitize_preserves_other_keys() -> None:
    """Unrelated env vars (e.g. PATH, PYTHONPATH) must pass through unchanged."""
    env = {
        "FORCE_COLOR": "3",
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": "/some/src",
        "VOYAGE_API_KEY": "test-key-value",
    }
    sanitized = sanitize_cli_subprocess_env(env)
    assert sanitized["PATH"] == "/usr/bin:/bin"
    assert sanitized["PYTHONPATH"] == "/some/src"
    assert sanitized["VOYAGE_API_KEY"] == "test-key-value"


def test_sanitize_handles_missing_force_color_key_gracefully() -> None:
    """Sanitizing an env with no FORCE_COLOR key at all must not raise."""
    env = {"PATH": "/usr/bin"}
    sanitized = sanitize_cli_subprocess_env(env)
    assert "FORCE_COLOR" not in sanitized
    assert sanitized["NO_COLOR"] == "1"


# ---------------------------------------------------------------------------
# Real-subprocess regression test
# ---------------------------------------------------------------------------


def test_cidx_subprocess_produces_color_free_stdout_despite_ambient_force_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduces the exact Bug #1372 condition end-to-end with a real cidx subprocess.

    Simulates the ambient environment from the bug report (FORCE_COLOR set
    in the parent pytest process, as an interactive Claude Code session
    does), builds the subprocess env the same way the e2e fixtures now do
    (``sanitize_cli_subprocess_env(dict(os.environ))``), and asserts the
    real ``cidx`` CLI child process emits no ANSI escape byte (\\x1b) in
    its captured stdout.

    Uses ``cidx help commands`` (no repo/config/network required) rather
    than an indexed query, to keep this test fast and mock-free while
    exercising the exact same Rich Console color-detection code path that
    produces the ANSI-wrapped ``N. `` result headers in the real bug.
    """
    monkeypatch.setenv("FORCE_COLOR", "3")
    monkeypatch.setenv("COLORTERM", "truecolor")

    src_dir = str(Path(__file__).resolve().parents[2] / "src")
    raw_env = dict(os.environ)
    raw_env["PYTHONPATH"] = src_dir

    sanitized_env = sanitize_cli_subprocess_env(raw_env)

    result = subprocess.run(
        [sys.executable, "-m", "code_indexer.cli", "help", "commands"],
        capture_output=True,
        text=True,
        env=sanitized_env,
        timeout=30,
    )

    assert result.returncode == 0, (
        f"cidx help commands failed (rc={result.returncode})\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "\x1b" not in result.stdout, (
        "Sanitized subprocess env still leaked ANSI escape codes into cidx "
        f"stdout despite ambient FORCE_COLOR:\n{result.stdout!r}"
    )
