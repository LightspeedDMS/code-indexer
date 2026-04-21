"""
Regression tests for the default_branch=None coercion bug.

Root cause: line 360 in golden_repo_manager.py used `default_branch or ""`
which silently coerced None to "".  The empty string then slipped through
the `is not None` guard in _clone_remote_repository (line 1217), causing
`git clone --branch ""` which fails with
"fatal: Remote branch  not found in upstream origin".

Three surgical fixes (see commit message):
  1. Remove `or ""` coercion at call site (line 360)
  2. Widen _clone_repository.branch to Optional[str] = None (line 1082)
  3. Change guard from `is not None` to truthy check (line 1217)

Test strategy:
  - Use _make_manager() with object.__new__() for lightweight construction
    (matches pattern in test_golden_repo_manager_ssh_noninteractive.py)
  - Patch subprocess.run to capture the exact git clone invocation
  - Inspect the captured command list for presence/absence of --branch
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers — mirror the pattern from test_golden_repo_manager_ssh_noninteractive.py
# ---------------------------------------------------------------------------


def _make_manager():
    """Construct a minimal GoldenRepoManager for direct method-level testing."""
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

    manager = object.__new__(GoldenRepoManager)
    resource_config = MagicMock()
    resource_config.git_pull_timeout = 60
    resource_config.git_clone_timeout = 120
    manager.resource_config = resource_config
    return manager


def _make_successful_subprocess_result():
    result = MagicMock()
    result.returncode = 0
    result.stdout = ""
    result.stderr = ""
    return result


def _run_clone_and_get_cmd(branch, tmp_path):
    """
    Run _clone_remote_repository with the given branch value against a patched
    subprocess.run, then return the captured git clone command list.

    Consolidates the identical setup shared by the three branch-flag tests.
    """
    manager = _make_manager()
    clone_path = str(tmp_path / "clone")

    with patch(
        "subprocess.run", return_value=_make_successful_subprocess_result()
    ) as mock_run:
        manager._clone_remote_repository(
            repo_url="git@github.com:example/repo.git",
            clone_path=clone_path,
            branch=branch,
        )

    for call in mock_run.call_args_list:
        cmd = call[0][0] if call[0] else call[1].get("args", [])
        if isinstance(cmd, list) and len(cmd) >= 2 and cmd[0] == "git" and cmd[1] == "clone":
            return cmd
    raise AssertionError(
        f"No 'git clone' call found in: {mock_run.call_args_list}"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_clone_with_none_default_branch_omits_branch_flag(tmp_path):
    """
    When branch=None is passed to _clone_remote_repository, the subprocess
    git clone command must NOT include --branch at all.

    This is the primary regression test for the bug: default_branch=None from
    add_golden_repo was coerced to "" which then passed the `is not None` guard
    and produced `git clone --branch ""`.
    """
    cmd = _run_clone_and_get_cmd(branch=None, tmp_path=tmp_path)
    assert "--branch" not in cmd, (
        f"Expected no --branch flag when branch=None, but got: {cmd}"
    )


def test_clone_with_empty_string_default_branch_omits_branch_flag(tmp_path):
    """
    Defensive test: when branch="" is passed to _clone_remote_repository,
    the subprocess git clone command must NOT include --branch "".

    After the fix, the truthy guard (`if branch:`) rejects both None and "",
    preventing `git clone --branch ""` from being emitted in either case.
    """
    cmd = _run_clone_and_get_cmd(branch="", tmp_path=tmp_path)
    assert "--branch" not in cmd, (
        f"Expected no --branch flag when branch='', but got: {cmd}"
    )


def test_clone_with_valid_default_branch_includes_branch_flag(tmp_path):
    """
    When a non-empty branch is specified, git clone must include --branch <name>.
    This verifies the fix does not accidentally suppress valid branch selections.
    """
    cmd = _run_clone_and_get_cmd(branch="main", tmp_path=tmp_path)
    assert "--branch" in cmd, (
        f"Expected --branch flag when branch='main', but got: {cmd}"
    )
    branch_idx = cmd.index("--branch")
    assert cmd[branch_idx + 1] == "main", (
        f"Expected --branch main, but got: {cmd[branch_idx + 1]!r}"
    )


def test_add_golden_repo_with_none_default_branch_does_not_emit_branch_flag(tmp_path):
    """End-to-end regression: _clone_repository with default_branch=None must not
    emit `git clone --branch ""` (or `--branch None`).

    This is the test that would have caught the original bug.  The prior tests
    exercised the leaf `_clone_remote_repository` directly, which had a correct
    guard — but the bug lived one layer up, where `default_branch or ""`
    silently coerced None into an empty string that slipped through the leaf.

    Drives the call through _clone_repository (the middle layer) with
    branch=None and patches subprocess.run at the module boundary
    (code_indexer.server.repositories.golden_repo_manager.subprocess.run)
    so the full chain is exercised without spawning a real git process.
    """
    manager = _make_manager()
    manager.golden_repos_dir = str(tmp_path)
    clone_path = str(tmp_path / "clone")

    with patch(
        "code_indexer.server.repositories.golden_repo_manager.subprocess.run",
        return_value=_make_successful_subprocess_result(),
    ) as mock_run:
        manager._clone_repository(
            repo_url="git@github.com:example/repo.git",
            alias="test-repo",
            branch=None,
        )

    # Flatten all subprocess.run invocation args into a single list so we can
    # assert that --branch never appears in any emitted command.
    all_tokens = []
    for call in mock_run.call_args_list:
        cmd = call[0][0] if call[0] else call[1].get("args", [])
        if isinstance(cmd, list):
            all_tokens.extend(cmd)

    assert "--branch" not in all_tokens, (
        f"Expected no --branch token anywhere when default_branch=None, "
        f"but subprocess received: {[c[0][0] if c[0] else c[1].get('args', []) for c in mock_run.call_args_list]}"
    )
