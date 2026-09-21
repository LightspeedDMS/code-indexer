"""
Unit tests for scripts/check_disclosure_tree.py (Bug #1916).

Bug #1916: the mandatory per-diff disclosure review scan structurally cannot
find a literal that was already committed before the gate existed, and can
also miss a literal added to a change AFTER reviewers already scanned it.
This script closes that gap with a TREE-wide scan (git-tracked files, not a
diff), wired into both lint.sh (review/CI time) and pre-commit (commit time).

Tests use REAL temporary git repositories under tmp_path (real `git init`,
`git add`, `git commit`, real `git ls-files`) -- zero mocking of git or the
filesystem, per this project's Anti-Mock rule. The scanner's own pattern
list is never used directly in these tests; each test supplies its own
synthetic banned-pattern/allowlist so this test file itself never needs to
embed a real leaked literal.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).parents[3]
_SCRIPT_PATH = _PROJECT_ROOT / "scripts" / "check_disclosure_tree.py"


def _load_module() -> Any:
    assert _SCRIPT_PATH.exists(), f"Script not found: {_SCRIPT_PATH}"
    spec = importlib.util.spec_from_file_location("check_disclosure_tree", _SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _init_git_repo(root: Path) -> None:
    """Create a real git repo at root with a real commit (no mocking)."""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=root, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)


def _commit_all(root: Path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "test commit"],
        cwd=root,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "PATH": "/usr/bin:/bin",
        },
    )


# ---------------------------------------------------------------------------
# scan() against real git repos
# ---------------------------------------------------------------------------


def test_scan_detects_banned_literal_with_correct_file_and_line(tmp_path):
    mod = _load_module()
    _init_git_repo(tmp_path)
    target = tmp_path / "leaky.py"
    target.write_text("line one\nsecretmarker9000 lives here\nline three\n")
    _commit_all(tmp_path)

    violations = mod.scan(
        root=tmp_path,
        banned_patterns=[("test-marker", "secretmarker9000", False)],
        allowlist={},
    )

    assert len(violations) == 1
    assert "leaky.py:2" in violations[0]
    assert "test-marker" in violations[0]


def test_scan_is_case_insensitive_when_configured(tmp_path):
    mod = _load_module()
    _init_git_repo(tmp_path)
    target = tmp_path / "leaky.py"
    target.write_text("SecretMarker9000\n")
    _commit_all(tmp_path)

    violations = mod.scan(
        root=tmp_path,
        banned_patterns=[("test-marker", "secretmarker9000", False)],
        allowlist={},
    )

    assert len(violations) == 1


def test_scan_case_sensitive_pattern_does_not_match_different_case(tmp_path):
    mod = _load_module()
    _init_git_repo(tmp_path)
    target = tmp_path / "leaky.py"
    target.write_text("SecretMarker9000\n")
    _commit_all(tmp_path)

    violations = mod.scan(
        root=tmp_path,
        banned_patterns=[("test-marker", "secretmarker9000", True)],
        allowlist={},
    )

    assert violations == []


def test_scan_returns_empty_on_clean_tree(tmp_path):
    mod = _load_module()
    _init_git_repo(tmp_path)
    (tmp_path / "clean.py").write_text("nothing to see here\n")
    _commit_all(tmp_path)

    violations = mod.scan(
        root=tmp_path,
        banned_patterns=[("test-marker", "secretmarker9000", False)],
        allowlist={},
    )

    assert violations == []


def test_scan_suppresses_allowlisted_line(tmp_path):
    mod = _load_module()
    _init_git_repo(tmp_path)
    (tmp_path / "leaky.py").write_text("secretmarker9000\n")
    _commit_all(tmp_path)

    violations = mod.scan(
        root=tmp_path,
        banned_patterns=[("test-marker", "secretmarker9000", False)],
        allowlist={"test-marker": {"leaky.py:1"}},
    )

    assert violations == []


def test_scan_allowlist_line_number_is_specific_not_whole_file(tmp_path):
    """An allowlist entry for one line must not suppress a DIFFERENT line
    with the same banned literal in the same file."""
    mod = _load_module()
    _init_git_repo(tmp_path)
    (tmp_path / "leaky.py").write_text("secretmarker9000\nsecretmarker9000\n")
    _commit_all(tmp_path)

    violations = mod.scan(
        root=tmp_path,
        banned_patterns=[("test-marker", "secretmarker9000", False)],
        allowlist={"test-marker": {"leaky.py:1"}},
    )

    assert len(violations) == 1
    assert "leaky.py:2" in violations[0]


def test_scan_suppresses_whole_file_allowlist_entry(tmp_path):
    """A bare path (no ':line') in the allowlist exempts the WHOLE file --
    used only for a file that must legitimately discuss the literal itself
    (e.g. the scanner script naming its own pattern)."""
    mod = _load_module()
    _init_git_repo(tmp_path)
    (tmp_path / "defines_pattern.py").write_text(
        "BANNED = 'secretmarker9000'\nsecretmarker9000\n"
    )
    _commit_all(tmp_path)

    violations = mod.scan(
        root=tmp_path,
        banned_patterns=[("test-marker", "secretmarker9000", False)],
        allowlist={"test-marker": {"defines_pattern.py"}},
    )

    assert violations == []


def test_scan_skips_binary_files_without_crashing(tmp_path):
    mod = _load_module()
    _init_git_repo(tmp_path)
    binary = tmp_path / "image.bin"
    binary.write_bytes(bytes(range(256)))
    (tmp_path / "clean.py").write_text("fine\n")
    _commit_all(tmp_path)

    violations = mod.scan(
        root=tmp_path,
        banned_patterns=[("test-marker", "secretmarker9000", False)],
        allowlist={},
    )

    assert violations == []


def test_scan_only_examines_git_tracked_files(tmp_path):
    """An untracked file containing the banned literal must NOT be flagged --
    the scan targets the tracked tree (what actually ships), not the
    working directory."""
    mod = _load_module()
    _init_git_repo(tmp_path)
    (tmp_path / "tracked.py").write_text("clean\n")
    _commit_all(tmp_path)
    # Untracked file added AFTER the commit, never `git add`-ed.
    (tmp_path / "untracked.py").write_text("secretmarker9000\n")

    violations = mod.scan(
        root=tmp_path,
        banned_patterns=[("test-marker", "secretmarker9000", False)],
        allowlist={},
    )

    assert violations == []


# ---------------------------------------------------------------------------
# main() CLI exit codes
# ---------------------------------------------------------------------------


def test_main_exits_nonzero_when_violations_found(tmp_path, monkeypatch, capsys):
    mod = _load_module()
    _init_git_repo(tmp_path)
    (tmp_path / "leaky.py").write_text("secretmarker9000\n")
    _commit_all(tmp_path)

    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(
        mod, "BANNED_PATTERNS", [("test-marker", "secretmarker9000", False)]
    )
    monkeypatch.setattr(mod, "ALLOWLIST", {})

    exit_code = mod.main()

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "leaky.py:1" in captured.err


def test_main_exits_zero_when_clean(tmp_path, monkeypatch, capsys):
    mod = _load_module()
    _init_git_repo(tmp_path)
    (tmp_path / "clean.py").write_text("fine\n")
    _commit_all(tmp_path)

    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(
        mod, "BANNED_PATTERNS", [("test-marker", "secretmarker9000", False)]
    )
    monkeypatch.setattr(mod, "ALLOWLIST", {})

    exit_code = mod.main()

    assert exit_code == 0


# ---------------------------------------------------------------------------
# Real invocation against the actual project tree (the thing lint.sh runs)
# ---------------------------------------------------------------------------


def test_real_project_tree_passes_with_the_actual_banned_patterns():
    """The scanner, run with its OWN real BANNED_PATTERNS/ALLOWLIST against
    the ACTUAL project tree, must currently report zero violations -- this
    is the exact invocation lint.sh performs."""
    mod = _load_module()

    violations = mod.scan(
        root=_PROJECT_ROOT,
        banned_patterns=mod.BANNED_PATTERNS,
        allowlist=mod.ALLOWLIST,
    )

    assert violations == [], f"Unexpected disclosure violations: {violations}"
