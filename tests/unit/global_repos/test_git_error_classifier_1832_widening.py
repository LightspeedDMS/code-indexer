"""
Unit tests for widening GitFetchError with stdout/returncode/cmd (Bug #1832
follow-up, coordinator-directed consolidation).

Context: refresh_scheduler.py has two sites (~1036, ~1124) that interpolate
`error.stderr` where `error` is a `GitFetchError`. Those sites cannot be
improved to show a fuller diagnostic because `GitFetchError` structurally
carries ONLY `.stderr` -- no `.stdout`, `.returncode`, or `.cmd`. This test
file proves the widening: the class must accept and expose these three new
OPTIONAL fields without breaking any existing (positional message, keyword
category/stderr) construction call.
"""

import pytest

from code_indexer.global_repos.git_error_classifier import GitFetchError


class TestGitFetchErrorWidenedConstructor:
    """The widened constructor must accept stdout/returncode/cmd as optional
    keyword args, on top of the pre-existing message/category/stderr."""

    def test_accepts_new_optional_fields(self):
        """
        Discriminating: on unmodified code, GitFetchError.__init__ does not
        accept stdout/returncode/cmd at all, so this raises TypeError.
        """
        error = GitFetchError(
            "Git fetch failed",
            category="transient",
            stderr="ssh: connect to host example.com port 22: Connection timed out",
            stdout="remote: some diagnostic that landed on stdout",
            returncode=128,
            cmd=["git", "fetch", "origin"],
        )

        assert error.stdout == "remote: some diagnostic that landed on stdout"
        assert error.returncode == 128
        assert error.cmd == ["git", "fetch", "origin"]

    def test_new_fields_default_to_none_when_omitted(self):
        """
        Backward compatibility: every existing call site in this codebase
        (git_pull_updater.py's pre-widening raise site, and
        test_refresh_scheduler_backoff.py's test-double constructors) passes
        only message/category/stderr. Those calls must keep working
        unchanged, with the new fields defaulting to None.
        """
        error = GitFetchError("Git fetch failed", category="permanent", stderr="x")

        assert error.stdout is None
        assert error.returncode is None
        assert error.cmd is None

    def test_stderr_semantics_unchanged(self):
        """
        AC (coordinator directive): .stderr keeps its exact prior meaning --
        the raw stderr string, untouched by the new fields.
        """
        error = GitFetchError(
            "Git fetch failed",
            category="transient",
            stderr="raw stderr text",
            stdout="unrelated stdout text",
            returncode=1,
            cmd=["git", "fetch", "origin"],
        )

        assert error.stderr == "raw stderr text"

    def test_positional_message_still_works(self):
        """
        message stays the first positional argument -- exactly how every
        existing call site (production and test) constructs it.
        """
        error = GitFetchError("some message", category="unknown", stderr="")

        assert str(error) == "some message"

    def test_isinstance_exception(self):
        with pytest.raises(GitFetchError):
            raise GitFetchError("boom", category="unknown", stderr="")
