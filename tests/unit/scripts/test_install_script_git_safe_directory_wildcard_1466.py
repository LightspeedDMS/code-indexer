"""Tests for Bug #1466: install-cidx-server.sh's ensure_git_safe_directory_wildcard().

Golden-repos/activated-repos on a CoW-mounted storage path may be owned by a
different OS user than the one running cidx-server (e.g. the CoW daemon's
own user vs a dedicated code-indexer service account), which trips git's
"dubious ownership" check on every git subprocess invocation that touches
them. A single blanket `safe.directory=*` grant for the service account is
idempotent and covers all present and future golden/activated repos
regardless of which directory they live in.

Per this project's "Auto-Updater Idempotent Deployment" invariant, the fix
must exist in BOTH places: DeploymentExecutor._ensure_git_safe_directory_
wildcard() (the auto-updater's self-heal twin for already-deployed hosts --
see test_deployment_executor_git_safe_directory.py) AND this installer's
ensure_git_safe_directory_wildcard() (fresh installs).

These tests run the ACTUAL bash function (sourced from the real script,
`main()` never invoked thanks to the script's own BASH_SOURCE guard) against
a REAL isolated git global config (fresh HOME/GIT_CONFIG_GLOBAL) -- no
mocking of git itself, matching this project's Anti-Mock rule and this
codebase's existing precedent for executing real shell scripts under test
(see test_install_script_cow_symlink_migration_1463.py). The function's
`sudo -u <user> git config ...` calls are wrapped by a same-shell `sudo()`
override that drops the `-u <user>` privilege-escalation prefix and runs the
command directly -- the test never needs real root/cross-user sudo, and this
does not weaken the proof: the exact git config command line the production
function issues is still executed for real against a real git binary.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO_ROOT_LEVELS_ABOVE_THIS_FILE = 3  # tests/unit/scripts/<this file> -> repo root
_BASH_INVOCATION_TIMEOUT_SECONDS = 30

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[_REPO_ROOT_LEVELS_ABOVE_THIS_FILE]
    / "scripts"
    / "install-cidx-server.sh"
)

skip_if_no_script = pytest.mark.skipif(
    not _SCRIPT_PATH.exists(), reason="install-cidx-server.sh not found"
)


def _run_ensure_wildcard(
    home_dir: Path, gitconfig: Path, dry_run: bool = False
) -> subprocess.CompletedProcess:
    """Source the real script and invoke ensure_git_safe_directory_wildcard()
    against an isolated git global config, with `sudo -u <user> CMD...`
    rewritten (via a same-shell function override) to just `CMD...` so no
    real privilege escalation is required."""
    # NOTE: install-cidx-server.sh has an unconditional top-level
    # `DRY_RUN=false` assignment, which always resets the variable at
    # `source` time (the real script only ever changes it afterward, via
    # --dry-run CLI flag parsing in parse_args). So DRY_RUN must be set
    # AFTER the `source` line here, not before -- setting it before would
    # be silently clobbered.
    bash_snippet = f"""
set -e
sudo() {{
    if [[ "$1" == "-u" ]]; then
        shift 2
    fi
    "$@"
}}
export HOME={str(home_dir)!r}
export GIT_CONFIG_GLOBAL={str(gitconfig)!r}
source {str(_SCRIPT_PATH)!r}
DRY_RUN={"true" if dry_run else "false"}
ensure_git_safe_directory_wildcard
"""
    return subprocess.run(
        ["bash", "-c", bash_snippet],
        capture_output=True,
        text=True,
        timeout=_BASH_INVOCATION_TIMEOUT_SECONDS,
    )


@skip_if_no_script
class TestEnsureGitSafeDirectoryWildcardAddsWhenMissing:
    def test_adds_wildcard_when_not_configured(self, tmp_path: Path) -> None:
        home_dir = tmp_path / "home"
        home_dir.mkdir()
        gitconfig = home_dir / ".gitconfig"  # deliberately absent

        result = _run_ensure_wildcard(home_dir, gitconfig)

        assert result.returncode == 0, (
            f"function must succeed: stdout={result.stdout!r} stderr={result.stderr!r}"
        )

        check = subprocess.run(
            ["git", "config", "--global", "--get-all", "safe.directory"],
            env={"HOME": str(home_dir), "GIT_CONFIG_GLOBAL": str(gitconfig)},
            capture_output=True,
            text=True,
        )
        assert check.returncode == 0
        assert "*" in check.stdout.split("\n"), (
            f"expected '*' to be added to safe.directory; got {check.stdout!r}"
        )


@skip_if_no_script
class TestEnsureGitSafeDirectoryWildcardIdempotent:
    def test_does_not_duplicate_when_already_configured(self, tmp_path: Path) -> None:
        home_dir = tmp_path / "home"
        home_dir.mkdir()
        gitconfig = home_dir / ".gitconfig"

        # Pre-seed the wildcard grant.
        subprocess.run(
            ["git", "config", "--global", "--add", "safe.directory", "*"],
            env={"HOME": str(home_dir), "GIT_CONFIG_GLOBAL": str(gitconfig)},
            check=True,
            capture_output=True,
        )

        result = _run_ensure_wildcard(home_dir, gitconfig)

        assert result.returncode == 0, (
            f"function must succeed: stdout={result.stdout!r} stderr={result.stderr!r}"
        )

        check = subprocess.run(
            ["git", "config", "--global", "--get-all", "safe.directory"],
            env={"HOME": str(home_dir), "GIT_CONFIG_GLOBAL": str(gitconfig)},
            capture_output=True,
            text=True,
        )
        assert check.returncode == 0
        wildcard_count = check.stdout.split("\n").count("*")
        assert wildcard_count == 1, (
            f"expected exactly one '*' entry (no-op, no duplicate); got "
            f"{wildcard_count} in {check.stdout!r}"
        )


@skip_if_no_script
class TestEnsureGitSafeDirectoryWildcardDryRun:
    def test_dry_run_does_not_touch_git_config(self, tmp_path: Path) -> None:
        home_dir = tmp_path / "home"
        home_dir.mkdir()
        gitconfig = home_dir / ".gitconfig"

        result = _run_ensure_wildcard(home_dir, gitconfig, dry_run=True)

        assert result.returncode == 0, (
            f"function must succeed: stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert not gitconfig.exists(), (
            "DRY_RUN=true must never write the git config file"
        )
