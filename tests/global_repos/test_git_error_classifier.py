"""
Tests for git_error_classifier module.

Tests error classification logic for git fetch failures:
- Corruption category: pack file / object database errors
- Transient category: network / auth errors
- Unknown category: unrecognized errors
- GitFetchError exception attributes
"""

import pytest

from code_indexer.global_repos.git_error_classifier import (
    GitFetchError,
    classify_fetch_error,
)


class TestClassifyFetchError:
    """Test suite for classify_fetch_error() function."""

    def test_classify_corruption_could_not_read(self):
        """'Could not read' in stderr classifies as corruption."""
        result = classify_fetch_error("error: Could not read d670460b4b4aece5915caf5c68d12f560a9fe3e4")
        assert result == "corruption"

    def test_classify_corruption_pack_unresolved_deltas(self):
        """'pack has N unresolved deltas' classifies as corruption."""
        result = classify_fetch_error(
            "fatal: pack has 1234 unresolved deltas\nerror: index-pack failed"
        )
        assert result == "corruption"

    def test_classify_corruption_invalid_index_pack(self):
        """'invalid index-pack output' classifies as corruption."""
        result = classify_fetch_error(
            "error: invalid index-pack output\nfatal: fetch-pack: invalid index-pack output"
        )
        assert result == "corruption"

    def test_classify_corruption_loose_object_corrupt(self):
        """'loose object is corrupt' classifies as corruption."""
        result = classify_fetch_error(
            "error: loose object 1234abc (stored in .git/objects/12/34abc) is corrupt"
        )
        assert result == "corruption"

    def test_classify_corruption_object_file_empty(self):
        """'object file is empty' classifies as corruption."""
        result = classify_fetch_error(
            "error: object file .git/objects/ab/cdef1234 is empty"
        )
        assert result == "corruption"

    def test_classify_corruption_packfile(self):
        """'packfile' in stderr classifies as corruption."""
        result = classify_fetch_error(
            "fatal: packfile .git/objects/pack/pack-abc123.pack does not match index"
        )
        assert result == "corruption"

    def test_classify_corruption_bad_object(self):
        """'bad object' in stderr classifies as corruption."""
        result = classify_fetch_error(
            "fatal: bad object HEAD\nerror: git fetch failed"
        )
        assert result == "corruption"

    def test_classify_transient_connection_refused(self):
        """'Connection refused' classifies as transient."""
        result = classify_fetch_error(
            "fatal: unable to connect to github.com:\ngithub.com[0: 140.82.121.4]: errno=Connection refused"
        )
        assert result == "transient"

    def test_classify_transient_could_not_resolve_host(self):
        """'Could not resolve host' classifies as transient."""
        result = classify_fetch_error(
            "fatal: unable to access 'https://github.com/org/repo.git/': "
            "Could not resolve host: github.com"
        )
        assert result == "transient"

    def test_classify_transient_auth_failed(self):
        """'Authentication failed' classifies as transient."""
        result = classify_fetch_error(
            "remote: HTTP Basic: Access denied\n"
            "fatal: Authentication failed for 'https://gitlab.com/org/repo.git/'"
        )
        assert result == "transient"

    def test_classify_transient_ssl(self):
        """SSL errors classify as transient."""
        result = classify_fetch_error(
            "fatal: unable to access 'https://github.com/org/repo.git/': "
            "SSL certificate problem: certificate has expired"
        )
        assert result == "transient"

    def test_classify_unknown_unrecognized_error(self):
        """Unrecognized error messages classify as unknown."""
        result = classify_fetch_error("something completely unexpected happened")
        assert result == "unknown"

    def test_classify_unknown_empty_stderr(self):
        """Empty stderr classifies as unknown."""
        result = classify_fetch_error("")
        assert result == "unknown"


class TestGitFetchError:
    """Test suite for GitFetchError exception class."""

    def test_git_fetch_error_exception_attributes(self):
        """GitFetchError has category and stderr attributes."""
        stderr_text = "error: Could not read pack index"
        exc = GitFetchError(
            "Git fetch failed for /some/path",
            category="corruption",
            stderr=stderr_text,
        )

        assert exc.category == "corruption"
        assert exc.stderr == stderr_text
        assert "Git fetch failed" in str(exc)

    def test_git_fetch_error_is_exception(self):
        """GitFetchError is an Exception subclass."""
        exc = GitFetchError("msg", category="transient", stderr="network error")
        assert isinstance(exc, Exception)

    def test_git_fetch_error_category_values(self):
        """GitFetchError accepts all valid category values."""
        for category in ("corruption", "transient", "unknown"):
            exc = GitFetchError("test", category=category, stderr="")
            assert exc.category == category
