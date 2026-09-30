"""Unit tests for the shared "safe git argv" validation helpers.

All tests use REAL throwaway git repositories (no mocking of git itself),
per this repo's Anti-Mock rule. Each validator must:
  - reject any value starting with '-' (would be parsed as a git OPTION
    rather than a literal value), so no such value can ever reach argv.
  - reject a NUL/CR/LF/other C0 control character (ordinary spaces and
    tabs are not a hazard: argv is never parsed by a shell).
  - apply additional, more specific validation only where the value
    itself carries independent meaning (a remote must be configured). A
    bare branch/ref name, a refspec side, and a revision are all left for
    git itself (or the caller's own pre-existing resolution logic) to
    resolve or reject beyond the two checks above.
  - let legitimate values pass through unchanged.

`validate_pathspecs` is the one exception to the leading-dash/control-
character rule above: every real caller already places a `--`
end-of-options marker before these paths reach argv, so a leading '-'
(including a real filename that is literally "-") can never be misread
as an option there, and ordinary CR/LF are legal filesystem-path
characters. It therefore rejects only a NUL byte.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_indexer.server.services.git_argv_safety import (
    GitArgumentValidationError,
    validate_branch_name,
    validate_pathspecs,
    validate_remote_name,
    validate_revision,
)


def _git(args: list, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), check=True, capture_output=True, text=True
    )


@pytest.fixture
def repo_with_remote(tmp_path: Path) -> Path:
    """Real repo with one commit and a configured local-path 'golden' remote."""
    remote = tmp_path / "golden_remote.git"
    remote.mkdir()
    _git(["init", "-q", "--bare"], cwd=remote)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], cwd=repo)
    _git(["config", "user.email", "test@example.com"], cwd=repo)
    _git(["config", "user.name", "Test User"], cwd=repo)
    (repo / "f.txt").write_text("hello\n")
    _git(["add", "f.txt"], cwd=repo)
    _git(["commit", "-q", "-m", "init"], cwd=repo)
    _git(["remote", "add", "golden", str(remote)], cwd=repo)

    return repo


# ---------------------------------------------------------------------------
# validate_remote_name
# ---------------------------------------------------------------------------


class TestValidateRemoteName:
    def test_rejects_receive_pack_option_injection(self, repo_with_remote: Path):
        """A --receive-pack= value must never reach argv."""
        with pytest.raises(GitArgumentValidationError):
            validate_remote_name("--receive-pack=example-value", repo_with_remote)

    def test_rejects_upload_pack_option_injection(self, repo_with_remote: Path):
        with pytest.raises(GitArgumentValidationError):
            validate_remote_name("--upload-pack=example-value", repo_with_remote)

    def test_rejects_leading_dash_even_without_known_option(
        self, repo_with_remote: Path
    ):
        with pytest.raises(GitArgumentValidationError):
            validate_remote_name("-anything", repo_with_remote)

    def test_accepts_empty_string_deferred_to_git(self, repo_with_remote: Path):
        """An empty string is not one of the two hazards (it can never be
        read as a git OPTION), so it passes through unchanged -- each
        call site's own pre-existing argument handling (e.g. `git
        fetch`/`git push`/`git pull`, which build their argv from
        `remote` unconditionally) decides what an empty remote means."""
        assert validate_remote_name("", repo_with_remote) == ""

    def test_rejects_remote_not_configured_on_repo(self, repo_with_remote: Path):
        """Even a syntactically clean name must be an ACTUALLY configured remote."""
        with pytest.raises(GitArgumentValidationError):
            validate_remote_name("not-a-real-remote", repo_with_remote)

    def test_accepts_configured_remote(self, repo_with_remote: Path):
        assert validate_remote_name("golden", repo_with_remote) == "golden"

    def test_accepts_origin_when_configured(self, tmp_path: Path):
        remote = tmp_path / "origin_remote.git"
        remote.mkdir()
        _git(["init", "-q", "--bare"], cwd=remote)
        repo = tmp_path / "repo2"
        repo.mkdir()
        _git(["init", "-q"], cwd=repo)
        _git(["remote", "add", "origin", str(remote)], cwd=repo)
        assert validate_remote_name("origin", repo) == "origin"

    def test_rejects_configured_remote_with_trailing_newline_or_cr(
        self, repo_with_remote: Path
    ):
        """A trailing LF or CR is a control character, rejected before
        the configured-remote membership check runs (which would itself
        also reject 'golden\\n' as a different string than 'golden').
        Uses the actually-configured 'golden' remote name so the only
        difference from an accepted value is the trailing character.
        """
        with pytest.raises(GitArgumentValidationError):
            validate_remote_name("golden\n", repo_with_remote)
        with pytest.raises(GitArgumentValidationError):
            validate_remote_name("golden\r", repo_with_remote)


# ---------------------------------------------------------------------------
# validate_branch_name
# ---------------------------------------------------------------------------


class TestValidateBranchName:
    def test_none_passes_through(self, repo_with_remote: Path):
        assert validate_branch_name(None) is None

    def test_rejects_leading_dash(self):
        with pytest.raises(GitArgumentValidationError):
            validate_branch_name("--receive-pack=example-value")

    def test_accepts_empty_string_deferred_to_git(self):
        """An empty string is not one of the two hazards (it can never be
        read as a git OPTION), so it passes through unchanged -- each
        call site's own pre-existing truthy check (e.g. `if branch:`) or
        git itself decides what an empty value means."""
        assert validate_branch_name("") == ""

    def test_accepts_range_shaped_value(self):
        """A bare branch/ref name (create/switch/delete) is not checked
        against `check-ref-format`; git itself resolves or rejects a
        value here exactly as it would with no validation at all."""
        assert validate_branch_name("bad..ref") == "bad..ref"

    def test_accepts_well_formed_branch_name(self):
        assert validate_branch_name("main") == "main"
        assert validate_branch_name("feature/foo-bar_1") == "feature/foo-bar_1"

    def test_rejects_well_formed_branch_with_trailing_newline_or_cr(self):
        """A trailing LF or CR is one of the two hazards `validate_branch_name`
        checks directly (see `_reject_control_characters`), independent of
        whether the rest of the value looks like a well-formed ref name."""
        with pytest.raises(GitArgumentValidationError):
            validate_branch_name("main\n")
        with pytest.raises(GitArgumentValidationError):
            validate_branch_name("main\r")

    def test_accepts_leading_plus(self):
        """`+foo` reaching a bare branch/ref name position (create/
        switch/delete) is a literal ref name, not a force-push marker --
        `git branch +foo` and `git checkout +foo` both treat it as such."""
        assert validate_branch_name("+main") == "+main"

    def test_accepts_colon_shaped_value(self):
        """A `src:dst`-shaped string reaching a bare branch/ref name
        position (create/switch/delete) is not treated as a refspec
        there; git itself resolves or rejects it."""
        assert validate_branch_name("other:refs/heads/main") == "other:refs/heads/main"


# ---------------------------------------------------------------------------
# validate_revision
# ---------------------------------------------------------------------------


class TestValidateRevision:
    def test_none_passes_through(self, repo_with_remote: Path):
        assert (
            validate_revision(None, repo_with_remote, param_name="from_revision")
            is None
        )

    def test_rejects_no_index_injection(self, repo_with_remote: Path):
        """--no-index must never reach argv as a revision."""
        with pytest.raises(GitArgumentValidationError):
            validate_revision(
                "--no-index", repo_with_remote, param_name="from_revision"
            )

    def test_accepts_empty_string_deferred_to_git(self, repo_with_remote: Path):
        """An empty string is not one of the two hazards (it can never be
        read as a git OPTION), so it passes through unchanged -- each
        call site's own pre-existing truthy check or resolution logic
        (or git itself) decides what an empty value means."""
        assert validate_revision("", repo_with_remote, param_name="from_revision") == ""

    def test_accepts_unresolvable_revision_deferred_to_git(
        self, repo_with_remote: Path
    ):
        """`validate_revision` no longer resolves the value itself; each
        call site's own pre-existing resolution logic (or `git` itself)
        rejects an unresolvable revision, exactly as it did with no
        validation at all."""
        assert (
            validate_revision(
                "not-a-real-revision-xyz",
                repo_with_remote,
                param_name="from_revision",
            )
            == "not-a-real-revision-xyz"
        )

    def test_accepts_head(self, repo_with_remote: Path):
        assert (
            validate_revision("HEAD", repo_with_remote, param_name="from_revision")
            == "HEAD"
        )

    def test_accepts_relative_ref(self, repo_with_remote: Path):
        # HEAD~0 always resolves (equivalent to HEAD).
        assert (
            validate_revision("HEAD~0", repo_with_remote, param_name="from_revision")
            == "HEAD~0"
        )

    def test_accepts_full_sha(self, repo_with_remote: Path):
        sha = _git(["rev-parse", "HEAD"], cwd=repo_with_remote).stdout.strip()
        assert validate_revision(sha, repo_with_remote, param_name="to_revision") == sha

    def test_rejects_valid_revision_with_trailing_newline_or_cr(
        self, repo_with_remote: Path
    ):
        """Trailing-newline regression.
        `validate_revision` has no Python regex of its own -- it delegates
        resolution to `git rev-parse --verify --end-of-options`, which this
        test proves is ALREADY strict about a trailing '\\n'/'\\r' on real
        git 2.52 (verified separately via a real subprocess: rev-parse
        rejects 'HEAD\\n' and 'HEAD\\r' with "fatal: Needed a single
        revision", returncode 128). No code change was needed here; this
        documents that fact as a permanent regression guard.
        """
        with pytest.raises(GitArgumentValidationError):
            validate_revision("HEAD\n", repo_with_remote, param_name="from_revision")
        with pytest.raises(GitArgumentValidationError):
            validate_revision("HEAD\r", repo_with_remote, param_name="from_revision")


# ---------------------------------------------------------------------------
# validate_pathspecs
# ---------------------------------------------------------------------------


class TestValidatePathspecs:
    def test_none_passes_through(self):
        assert validate_pathspecs(None) is None

    def test_accepts_option_shaped_entry_deferred_to_the_callers_own_dash_dash(
        self,
    ):
        """A pathspec entry starting with '-' -- including one shaped like
        a real git option -- is not a hazard here: every real caller
        already places `--` before these paths reach argv, so git can
        never misread it as an option. This entry reaches the caller's
        own argv construction unchanged."""
        assert validate_pathspecs(["--output=output_marker.txt", "--text"]) == [
            "--output=output_marker.txt",
            "--text",
        ]

    def test_accepts_any_leading_dash_entry(self):
        assert validate_pathspecs(["fine.txt", "-x"]) == ["fine.txt", "-x"]

    def test_accepts_normal_paths(self):
        assert validate_pathspecs(["a.txt", "sub/b.txt"]) == ["a.txt", "sub/b.txt"]

    def test_accepts_empty_list(self):
        assert validate_pathspecs([]) == []

    def test_rejects_nul_byte(self):
        """A NUL byte is the one thing that can never be a legal
        filesystem-path character, and would otherwise reach
        `subprocess.run` directly and raise a bare `ValueError` instead
        of this validator's own clean `GitArgumentValidationError`."""
        with pytest.raises(GitArgumentValidationError):
            validate_pathspecs(["fine.txt", "a\x00b"])
