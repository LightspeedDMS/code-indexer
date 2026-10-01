"""`validate_revision_range`, `validate_branch_name` on push/pull refspecs,
and the widened `VALID_GIT_RESET_MODES` allowlist in git_argv_safety.py.

A revision or range is a single argv element, so the only two hazards
are the whole value starting with '-' (read as a git OPTION) and a
NUL/CR/LF/other C0 control character; beyond that, git itself resolves
or rejects the value -- e.g. `main..-x` is not an option, since it
starts with 'm', and git rejects it with its own "unknown revision"
error. A push/pull refspec is likewise one argv element: only the whole
element is checked, and its leading '+' (git's force-push marker) and
`src:dst` sides reach git unchanged; a reset mode is exactly one of a
fixed literal set.

All tests run against a REAL throwaway git repository (no mocking of git
itself, per this repo's Anti-Mock rule).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_indexer.server.services.git_argv_safety import (
    GitArgumentValidationError,
    VALID_GIT_RESET_MODES,
    validate_branch_name,
    validate_pathspecs,
    validate_reset_mode,
    validate_revision,
    validate_revision_range,
)


def _git(args: list, cwd: Path) -> None:
    subprocess.run(
        ["git"] + args, cwd=str(cwd), check=True, capture_output=True, text=True
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(["init", "-q", "-b", "main"], r)
    _git(["config", "user.email", "test@example.com"], r)
    _git(["config", "user.name", "Test User"], r)
    (r / "f.txt").write_text("one\n")
    _git(["add", "f.txt"], r)
    _git(["commit", "-q", "-m", "c1"], r)
    (r / "f.txt").write_text("one\ntwo\n")
    _git(["add", "f.txt"], r)
    _git(["commit", "-q", "-m", "c2"], r)
    _git(["checkout", "-q", "-b", "feature"], r)
    (r / "f.txt").write_text("one\ntwo\nthree\n")
    _git(["add", "f.txt"], r)
    _git(["commit", "-q", "-m", "c3"], r)
    _git(["checkout", "-q", "main"], r)
    return r


class TestValidateRevisionRangeAcceptsRangeSyntax:
    @pytest.mark.parametrize(
        "expr",
        [
            "main..feature",
            "main...feature",
            "HEAD~1..HEAD",
            "main..",
            "..main",
            "feature^!",
            "HEAD^@",
            "HEAD^-1",
            "HEAD^-",
        ],
    )
    def test_accepts_revision_range_syntax(self, repo, expr):
        assert validate_revision_range(expr, repo, param_name="rev") == expr

    def test_plain_single_revision_still_works(self, repo):
        assert validate_revision_range("HEAD", repo, param_name="rev") == "HEAD"

    def test_none_passes_through(self, repo):
        assert validate_revision_range(None, repo, param_name="rev") is None

    def test_agrees_with_validate_revision_since_both_defer_ranges_to_git(self, repo):
        """`validate_revision` is a pure delegation to
        `validate_revision_range`, so the two apply one identical check:
        both accept a range (neither resolves anything itself) and both
        accept the exact value `-` for every caller, which is never an
        option. Neither takes an opt-in flag for `-`."""
        assert (
            validate_revision_range("main..feature", repo, param_name="rev")
            == "main..feature"
        )
        assert (
            validate_revision("main..feature", repo, param_name="rev")
            == "main..feature"
        )
        assert validate_revision_range("-", repo, param_name="rev") == "-"
        assert validate_revision("-", repo, param_name="rev") == "-"
        with pytest.raises(TypeError):
            validate_revision(  # type: ignore[call-arg]
                "-", repo, param_name="rev", allow_previous_ref_shorthand=True
            )


class TestValidateRevisionRangeStillRejects:
    @pytest.mark.parametrize(
        "expr",
        [
            "-x",
            "--output=example-path",
            "-x..b",
        ],
    )
    def test_rejects_option_shaped_variants(self, repo, expr):
        with pytest.raises(GitArgumentValidationError):
            validate_revision_range(expr, repo, param_name="rev")

    def test_accepts_empty_string_deferred_to_git(self, repo):
        """An empty string is not one of the two hazards (it can never be
        read as a git OPTION), so it passes through unchanged -- each
        call site's own pre-existing truthy check or git itself decides
        what an empty value means."""
        assert validate_revision_range("", repo, param_name="rev") == ""

    @pytest.mark.parametrize("expr", ["HEAD^!\n", "main..feature\n", "HEAD\x00tail"])
    def test_rejects_embedded_control_character(self, repo, expr):
        with pytest.raises(GitArgumentValidationError):
            validate_revision_range(expr, repo, param_name="rev")

    def test_accepts_range_with_option_shaped_side_deferred_to_git(self, repo):
        """`main..-x` is not an option: the whole value starts with 'm',
        not '-'. There is no per-side check any more, so this is accepted
        here and left for git itself to reject when the range is actually
        used (verified empirically: `git diff --end-of-options
        main..-x` fails with "unknown revision", not an option error)."""
        assert validate_revision_range("main..-x", repo, param_name="rev") == "main..-x"


class TestValidateRevisionRangeAcceptsSpacedSyntax:
    """git's own reflog/date and commit-message-search syntax contains
    ordinary spaces; a value is passed to `git` as one literal argv
    element (argv is never parsed by a shell), so a space is not a
    hazard and must not be rejected."""

    @pytest.fixture()
    def repo_with_searchable_commit(self, tmp_path: Path) -> Path:
        r = tmp_path / "searchable_repo"
        r.mkdir()
        _git(["init", "-q", "-b", "main"], r)
        _git(["config", "user.email", "test@example.com"], r)
        _git(["config", "user.name", "Test User"], r)
        (r / "f.txt").write_text("one\n")
        _git(["add", "f.txt"], r)
        _git(["commit", "-q", "-m", "fix bug in parser"], r)
        return r

    def test_accepts_reflog_date_expression(self, repo_with_searchable_commit):
        r = repo_with_searchable_commit
        assert (
            validate_revision_range("HEAD@{1 day ago}", r, param_name="rev")
            == "HEAD@{1 day ago}"
        )

    def test_accepts_named_branch_reflog_date_expression(
        self, repo_with_searchable_commit
    ):
        r = repo_with_searchable_commit
        assert (
            validate_revision_range("main@{1 minute ago}", r, param_name="rev")
            == "main@{1 minute ago}"
        )

    def test_accepts_commit_message_search_syntax(self, repo_with_searchable_commit):
        r = repo_with_searchable_commit
        assert validate_revision_range(":/fix bug", r, param_name="rev") == ":/fix bug"

    def test_accepts_reflog_date_expression_in_a_range(
        self, repo_with_searchable_commit
    ):
        r = repo_with_searchable_commit
        assert (
            validate_revision_range("HEAD@{1 day ago}..HEAD", r, param_name="rev")
            == "HEAD@{1 day ago}..HEAD"
        )

    def test_accepts_reflog_date_expression_as_refspec_side(self):
        assert (
            validate_branch_name(
                "HEAD@{1 minute ago}:refs/heads/dated", param_name="branch"
            )
            == "HEAD@{1 minute ago}:refs/heads/dated"
        )


class TestValidateRevisionRangeAcceptsEmbeddedDotDot:
    """A single revision may itself contain '..' without being a range,
    e.g. git's commit-message-search syntax (`:/<pattern>`) or a
    commit-message search from a base (`<rev>^{/<pattern>}`). Since there
    is no splitting here any more, neither is misread as a range."""

    @pytest.fixture()
    def repo_with_dotted_message(self, tmp_path: Path) -> Path:
        r = tmp_path / "dotted_message_repo"
        r.mkdir()
        _git(["init", "-q", "-b", "main"], r)
        _git(["config", "user.email", "test@example.com"], r)
        _git(["config", "user.name", "Test User"], r)
        (r / "f.txt").write_text("one\n")
        _git(["add", "f.txt"], r)
        _git(["commit", "-q", "-m", "fix foo..bar thing"], r)
        return r

    def test_accepts_commit_message_search_containing_dotdot(
        self, repo_with_dotted_message
    ):
        assert (
            validate_revision_range(
                ":/fix foo..bar", repo_with_dotted_message, param_name="rev"
            )
            == ":/fix foo..bar"
        )

    def test_accepts_commit_message_search_from_base_containing_dotdot(
        self, repo_with_dotted_message
    ):
        assert (
            validate_revision_range(
                "HEAD^{/fix foo..bar}", repo_with_dotted_message, param_name="rev"
            )
            == "HEAD^{/fix foo..bar}"
        )


class TestValidateBranchNameRefspecContentsDeferToGit:
    """A push/pull refspec is ONE argv element: only a leading '-' on the
    whole element (or a control character) is a hazard. The `+` force
    marker and each `src:dst` side are ref syntax that git itself
    resolves or rejects -- e.g. `git push <remote> main:-` creates a
    remote branch literally named `-`."""

    @pytest.mark.parametrize(
        "expr",
        [
            "+main",
            "HEAD:refs/heads/x",
            "feature:main",
            ":refs/heads/old",
            "HEAD~1:refs/heads/older",
            "refs/heads/*:refs/heads/*",
            "+-x",
            ":-x",
            ":--x",
            "a:b:-x",
            "main:-",
            "+main:-x",
            "refs/heads/*:-x",
        ],
    )
    def test_accepts_refspec_contents(self, repo, expr):
        assert validate_branch_name(expr, param_name="branch") == expr

    def test_none_passes_through(self, repo):
        assert validate_branch_name(None, param_name="branch") is None

    @pytest.mark.parametrize("expr", ["-x", "--x:main", "main:main\n", "+main\r"])
    def test_rejects_leading_dash_or_control_character(self, repo, expr):
        with pytest.raises(GitArgumentValidationError):
            validate_branch_name(expr, param_name="branch")

    def test_has_no_allow_refspec_flag(self, repo):
        with pytest.raises(TypeError):
            validate_branch_name(  # type: ignore[call-arg]
                "+main", param_name="branch", allow_refspec=True
            )


class TestValidateBranchNameBareRefDefersToGit:
    """Branch create/switch/delete: only a leading '-' (other than
    exactly `-`) and a control character are rejected; everything
    else is left for git itself to resolve or reject, matching how these
    positions behaved with no validation at all -- `git branch +foo`,
    `git checkout @{-1}` (the previously checked out branch), and
    `git checkout HEAD~1` (a commit-ish switch target, detached HEAD) all
    work."""

    def test_accepts_plus_prefixed_value(self, repo):
        assert validate_branch_name("+foo", param_name="branch_name") == "+foo"

    def test_accepts_previous_branch_shorthand(self, repo):
        assert validate_branch_name("@{-1}", param_name="branch_name") == "@{-1}"

    def test_accepts_commit_ish_switch_target(self, repo):
        assert validate_branch_name("HEAD~1", param_name="branch_name") == "HEAD~1"

    def test_accepts_plain_branch_name(self, repo):
        assert (
            validate_branch_name("feature/x", param_name="branch_name") == "feature/x"
        )

    def test_rejects_leading_dash(self, repo):
        with pytest.raises(GitArgumentValidationError):
            validate_branch_name("-x", param_name="branch_name")

    def test_rejects_control_character(self, repo):
        with pytest.raises(GitArgumentValidationError):
            validate_branch_name("main\n", param_name="branch_name")


class TestValidateBranchNameAcceptsBareDash:
    """The exact value `-` is a single character, not `-<something>`, so
    git never reads it as an option: in every argv `validate_branch_name`
    guards it is a positional (`git checkout -` is git's own
    previous-branch shorthand; `git push <remote> -`, `git pull <remote>
    -`, `git branch -` and `git branch -d -` each let git itself resolve
    or reject it). It is accepted for every caller, bare-ref and refspec
    alike, with no opt-in flag, and so is a refspec whose side is `-`
    (the whole element does not start with '-'); any OTHER value starting
    with '-' is still rejected."""

    def test_accepts_bare_dash(self, repo):
        assert validate_branch_name("-", param_name="branch_name") == "-"

    @pytest.mark.parametrize("value", ["+-", "x:-", "+x:-"])
    def test_accepts_dash_refspec_sides(self, repo, value):
        assert validate_branch_name(value, param_name="branch") == value

    @pytest.mark.parametrize("value", ["-x", "--", "--force", "-\n", "-:x"])
    def test_still_rejects_other_dash_prefixed_values(self, repo, value):
        with pytest.raises(GitArgumentValidationError):
            validate_branch_name(value, param_name="branch")

    def test_has_no_previous_ref_shorthand_flag(self, repo):
        with pytest.raises(TypeError):
            validate_branch_name(  # type: ignore[call-arg]
                "-", param_name="branch_name", allow_previous_ref_shorthand=True
            )


class TestValidateResetModeAllowlist:
    def test_allowlist_is_exactly_five_literal_modes(self):
        """Mutation guard: locks the exact set so a future edit cannot
        silently drop or widen it without this test catching it."""
        assert VALID_GIT_RESET_MODES == frozenset(
            {"soft", "mixed", "hard", "keep", "merge"}
        )

    @pytest.mark.parametrize("mode", ["soft", "mixed", "hard", "keep", "merge"])
    def test_accepts_each_valid_mode(self, mode):
        assert validate_reset_mode(mode) == mode

    def test_rejects_unambiguous_abbreviation(self):
        with pytest.raises(GitArgumentValidationError):
            validate_reset_mode("har")

    def test_rejects_unknown_mode(self):
        with pytest.raises(GitArgumentValidationError):
            validate_reset_mode("bogus")

    def test_accepts_empty_string_unchanged(self):
        """An empty mode is not an option: the call site's own
        `f"--{mode}"` construction turns "" into a bare `--` separator,
        giving `git reset -- <target>` (the target is read as a pathspec,
        not a commit) -- the same argv as with no validation at all."""
        assert validate_reset_mode("") == ""


class TestValidateRevisionAcceptsBareDash:
    """`git merge -` (the previously checked out branch) is git's own
    shorthand, not an option, verified working with real git.
    `validate_revision` is a pure delegation to `validate_revision_range`,
    which accepts the exact value `-` for every caller, with no opt-in
    flag."""

    def test_accepts_bare_dash(self, repo):
        assert validate_revision("-", repo, param_name="source_branch") == "-"

    def test_still_rejects_dash_prefixed_value(self, repo):
        with pytest.raises(GitArgumentValidationError):
            validate_revision("-x", repo, param_name="source_branch")


class TestValidatePathspecsNulOnly:
    """A NUL byte in a pathspec entry must be rejected with
    GitArgumentValidationError before any git subprocess runs -- it would
    otherwise reach `subprocess.run` directly and raise a bare
    `ValueError` ("embedded null byte"). CR/LF and a leading '-' are NOT
    hazards here: every real caller (`git_stage`'s `git add -- ...`,
    `git_unstage`'s `git reset HEAD -- ...`, `git_diff`'s legacy
    file_paths branch) already places `--` before these paths reach
    argv, and CR/LF are legal filesystem-path characters."""

    def test_none_passes_through(self):
        assert validate_pathspecs(None) is None

    def test_accepts_plain_paths(self):
        assert validate_pathspecs(["src/a.py", "src/b.py"]) == [
            "src/a.py",
            "src/b.py",
        ]

    def test_rejects_nul_byte(self):
        with pytest.raises(GitArgumentValidationError):
            validate_pathspecs(["a\x00b"])

    @pytest.mark.parametrize("entry", ["a\nb", "a\rb"])
    def test_accepts_cr_lf_in_a_real_filename(self, entry):
        assert validate_pathspecs([entry]) == [entry]

    def test_accepts_leading_dash(self):
        assert validate_pathspecs(["-x"]) == ["-x"]
