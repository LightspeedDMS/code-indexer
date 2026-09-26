"""Unit tests for the shared "safe git argv" validation helpers.

All tests use REAL throwaway git repositories (no mocking of git itself),
per this repo's Anti-Mock rule. Each validator must:
  - reject any value starting with '-' (would be parsed as a git OPTION
    rather than a literal value), so no such value can ever reach argv.
  - apply additional, more specific validation (remote must be configured;
    branch must be a well-formed ref; revision must resolve).
  - let legitimate values pass through unchanged.
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

    def test_rejects_empty_string(self, repo_with_remote: Path):
        with pytest.raises(GitArgumentValidationError):
            validate_remote_name("", repo_with_remote)

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
        """Trailing-newline regression:
        a Python regex validated with `.match(...)` against a `$`-anchored
        pattern incorrectly accepts a trailing '\\n' (re.match's `$` matches
        just before a final newline, not only true end-of-string). Fixed by
        using `.fullmatch(...)` with no anchors, which has no such leniency.
        Uses the actually-configured 'golden' remote name plus a trailing
        newline/CR so the ONLY thing standing between accept and reject is
        the regex's own strictness -- the membership check alone would also
        reject 'golden\\n' as a different string than 'golden', so this
        exercises both layers rejecting consistently.
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

    def test_rejects_empty_string(self):
        with pytest.raises(GitArgumentValidationError):
            validate_branch_name("")

    def test_rejects_malformed_ref(self):
        with pytest.raises(GitArgumentValidationError):
            validate_branch_name("bad..ref")

    def test_accepts_well_formed_branch_name(self):
        assert validate_branch_name("main") == "main"
        assert validate_branch_name("feature/foo-bar_1") == "feature/foo-bar_1"

    def test_rejects_well_formed_branch_with_trailing_newline_or_cr(self):
        """Trailing-newline regression.
        `validate_branch_name` has no Python regex of its own -- it
        delegates well-formedness to `git check-ref-format
        --allow-onelevel`, which this test proves is ALREADY strict about
        a trailing '\\n'/'\\r' on real git 2.52 (verified separately via a
        real subprocess: `check-ref-format` rejects 'main\\n' and 'main\\r'
        with returncode 1). No code change was needed here; this documents
        that fact as a permanent regression guard.
        """
        with pytest.raises(GitArgumentValidationError):
            validate_branch_name("main\n")
        with pytest.raises(GitArgumentValidationError):
            validate_branch_name("main\r")

    def test_rejects_leading_plus_force_push_marker(self):
        """`git check-ref-format --allow-onelevel '+main'` is ACCEPTED by
        real git 2.52 (verified separately via a real subprocess:
        returncode 0) -- and `git push --end-of-options golden +main` then
        force-pushes, rewriting the remote branch (verified separately: the
        push reports "(forced update)"). Neither check-ref-format nor
        --end-of-options rejects a leading '+', so validate_branch_name
        must reject it itself before either ever runs.
        """
        with pytest.raises(GitArgumentValidationError):
            validate_branch_name("+main")

    def test_rejects_colon_refspec_syntax(self):
        """A `src:dst` refspec (e.g. 'other:refs/heads/main') must never
        reach argv as a single `branch` value. This is already rejected by
        `git check-ref-format --allow-onelevel` (verified separately via a
        real subprocess: 'a:b' returns returncode 1) -- no code change was
        needed for this specific case; this documents that fact as a
        permanent regression guard, alongside the leading-'+' case above
        which did require a code change.
        """
        with pytest.raises(GitArgumentValidationError):
            validate_branch_name("other:refs/heads/main")


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

    def test_rejects_empty_string(self, repo_with_remote: Path):
        with pytest.raises(GitArgumentValidationError):
            validate_revision("", repo_with_remote, param_name="from_revision")

    def test_rejects_unresolvable_revision(self, repo_with_remote: Path):
        with pytest.raises(GitArgumentValidationError):
            validate_revision(
                "not-a-real-revision-xyz",
                repo_with_remote,
                param_name="from_revision",
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

    def test_rejects_output_option_injection(self, tmp_path: Path):
        """--output= must never reach argv as a pathspec. The decoy path
        is a neutral file under this test's own
        tmp_path -- validate_pathspecs only inspects the string's leading
        character and never touches the filesystem, so no real system path
        is needed here."""
        decoy = tmp_path / "decoy.txt"
        decoy.write_text("neutral test content\n")
        with pytest.raises(GitArgumentValidationError):
            validate_pathspecs(["--output=output_marker.txt", "--text", str(decoy)])

    def test_rejects_any_leading_dash_entry(self):
        with pytest.raises(GitArgumentValidationError):
            validate_pathspecs(["fine.txt", "-x"])

    def test_accepts_normal_paths(self):
        assert validate_pathspecs(["a.txt", "sub/b.txt"]) == ["a.txt", "sub/b.txt"]

    def test_accepts_empty_list(self):
        assert validate_pathspecs([]) == []
