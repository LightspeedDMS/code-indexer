"""Epic #2103 item 15: ``GitUrlNormalizer`` is built on the shared git URL
parser (``code_indexer.utils.git_remote_url``); its canonical form for a
remote URL is the parsed URL's identity.

The normalizer's canonical form previously diverged on ssh:// URLs: a
non-git user was rejected and an SSH port was read as a path segment.
"""

import pytest

from code_indexer.server.services.git_url_normalizer import (
    GitUrlNormalizationError,
    GitUrlNormalizer,
)
from code_indexer.utils.git_remote_url import parse_git_remote_url
from tests.unit.utils.test_git_remote_url import PARSE_TABLE

OWNED_URLS = sorted(url for url, row in PARSE_TABLE.items() if "/" in row[6])


@pytest.mark.parametrize("url", OWNED_URLS)
def test_canonical_form_is_the_shared_identity(url: str) -> None:
    parsed = parse_git_remote_url(url)
    assert parsed is not None

    result = GitUrlNormalizer().normalize(url)

    assert result.canonical_form == parsed.identity
    assert result.domain == parsed.web_host
    assert (result.user, result.repo) == parsed.owner_repo()


def test_ssh_non_git_user_previously_rejected_now_normalized() -> None:
    result = GitUrlNormalizer().normalize("ssh://deploy@git.example.com/owner/repo.git")

    assert result.canonical_form == "git.example.com/owner/repo"


def test_ssh_port_previously_read_as_path_segment() -> None:
    result = GitUrlNormalizer().normalize(
        "ssh://git@git.example.com:2222/owner/repo.git"
    )

    assert result.canonical_form == "git.example.com/owner/repo"
    assert result.user == "owner"


def test_all_remote_forms_share_one_canonical_form() -> None:
    urls = [
        "https://git.example.com/owner/repo.git",
        "http://git.example.com/owner/repo",
        "https://user:pw@git.example.com/owner/repo.git",
        "git@git.example.com:owner/repo.git",
        "deploy@git.example.com:owner/repo.git",
        "ssh://git@git.example.com/owner/repo.git",
        "ssh://deploy@git.example.com:2222/owner/repo.git",
    ]

    forms = {GitUrlNormalizer().get_canonical_form(url) for url in urls}

    assert forms == {"git.example.com/owner/repo"}


@pytest.mark.parametrize(
    "url",
    [
        "ftp://git.example.com/owner/repo.git",
        "https://git.example.com",
        "https://git.example.com/owner",
    ],
)
def test_values_without_owner_and_repo_still_raise(url: str) -> None:
    with pytest.raises(GitUrlNormalizationError):
        GitUrlNormalizer().normalize(url)
