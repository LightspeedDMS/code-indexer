"""
Unit tests for GitUrlNormalizer filesystem-path URL support (Bug #842).

Covers three filesystem-path forms valid for ``git clone``:
  1. /absolute/path
  2. file:///absolute/path  (must produce same canonical_form as /absolute/path)
  3. ~/relative/path        (expands to same canonical_form as its absolute equivalent)

Also guards that existing HTTPS/SSH normalization is unaffected.

All paths use ``Path.home()`` so tests are portable across machines.
``normalize()`` accepts ``object`` in the production signature, so the None
rejection test requires no type suppressions.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from code_indexer.server.services.git_url_normalizer import (
    GitUrlNormalizer,
    GitUrlNormalizationError,
    NormalizedGitUrl,
)

_HOME = str(Path.home())
_ABS = f"{_HOME}/.tmp/repos/myrepo"
_FILE_URI = f"file://{_ABS}"
_TILDE = "~/repos/myrepo"


@pytest.fixture
def n() -> GitUrlNormalizer:
    return GitUrlNormalizer()


# ---------------------------------------------------------------------------
# Acceptance: all three forms return a NormalizedGitUrl with correct fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected_repo",
    [
        (_ABS, "myrepo"),
        (_FILE_URI, "myrepo"),
        (_TILDE, "myrepo"),
    ],
    ids=["abs", "file-uri", "tilde"],
)
def test_filesystem_path_accepted_with_correct_fields(
    n: GitUrlNormalizer, url: str, expected_repo: str
) -> None:
    """Accepted URL returns NormalizedGitUrl with domain='local' and correct repo name."""
    result = n.normalize(url)
    assert isinstance(result, NormalizedGitUrl)
    assert result.domain == "local"
    assert result.repo == expected_repo
    assert result.original_url == url


# ---------------------------------------------------------------------------
# Round-trip stability: same URL normalizes to same canonical_form twice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [_ABS, _FILE_URI, _TILDE],
    ids=["abs", "file-uri", "tilde"],
)
def test_canonical_form_is_stable(n: GitUrlNormalizer, url: str) -> None:
    """Normalizing the same URL twice yields the same canonical_form."""
    assert n.normalize(url).canonical_form == n.normalize(url).canonical_form


# ---------------------------------------------------------------------------
# Cross-form equality: equivalent URL styles share a canonical_form
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "left, right",
    [
        (_FILE_URI, _ABS),
        (_TILDE, os.path.expanduser(_TILDE)),
    ],
    ids=["file-uri-vs-abs", "tilde-vs-expanded"],
)
def test_equivalent_forms_share_canonical(
    n: GitUrlNormalizer, left: str, right: str
) -> None:
    """Equivalent filesystem-path representations produce identical canonical forms."""
    assert n.normalize(left).canonical_form == n.normalize(right).canonical_form


# ---------------------------------------------------------------------------
# Rejection: invalid / out-of-scope inputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        r"C:\windows\path\repo",
        "relative/path/repo",
        "/",
        "/onlyone",
        "~",
    ],
    ids=[
        "empty",
        "whitespace",
        "windows-path",
        "relative",
        "bare-slash",
        "single-component",
        "bare-tilde",
    ],
)
def test_invalid_string_inputs_rejected(n: GitUrlNormalizer, bad: str) -> None:
    """Invalid or out-of-scope string inputs raise GitUrlNormalizationError."""
    with pytest.raises(GitUrlNormalizationError):
        n.normalize(bad)


def test_none_rejected(n: GitUrlNormalizer) -> None:
    """None raises GitUrlNormalizationError. normalize() accepts object so no suppression needed."""
    with pytest.raises(GitUrlNormalizationError):
        n.normalize(None)


# ---------------------------------------------------------------------------
# Regression guard: HTTPS and SSH still normalize correctly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected_domain, expected_repo",
    [
        ("https://github.com/user/repo", "github.com", "repo"),
        ("https://github.com/user/repo.git", "github.com", "repo"),
        ("git@github.com:user/repo", "github.com", "repo"),
    ],
    ids=["https", "https-dot-git", "ssh"],
)
def test_remote_urls_unaffected(
    n: GitUrlNormalizer, url: str, expected_domain: str, expected_repo: str
) -> None:
    """Existing HTTPS/SSH URL normalization is unaffected by the filesystem-path addition."""
    result = n.normalize(url)
    assert result.domain == expected_domain
    assert result.repo == expected_repo
