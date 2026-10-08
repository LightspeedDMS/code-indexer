"""Credential host scoping is never wider than the previous helper.

``credential_scope_host`` (code_indexer.utils.git_remote_url) decides which
host a stored credential may be used for. For every input it returns the
previous helper's host or None, never a host the previous helper did not.
The previous helper is kept below, verbatim, as the reference oracle.
"""

import re
from typing import Optional
from urllib.parse import urlsplit

import pytest

from code_indexer.server.services.git_credential_helper import GitCredentialHelper
from code_indexer.utils.git_remote_url import credential_scope_host, git_remote_host

_RAISED = "<raised>"


def _previous_extract_host(remote_url: str) -> Optional[str]:
    """The previous GitCredentialHelper.extract_host_from_remote_url."""
    if not remote_url:
        return None
    url = remote_url.strip()
    match = re.match(r"^git@([^:]+):", url)
    if match:
        return match.group(1)
    if re.match(r"^https?://", url):
        host = urlsplit(url).netloc.rpartition("@")[2]
        return host or None
    match = re.match(r"^ssh://git@([^:/]+)", url)
    if match:
        return match.group(1)
    return None


def _previous_convert_ssh_to_https(remote_url: str) -> str:
    """The previous GitCredentialHelper.convert_ssh_to_https."""
    url = remote_url.strip()
    match = re.match(r"^git@([^:]+):(.+)$", url)
    if match:
        return f"https://{match.group(1)}/{match.group(2)}"
    match = re.match(r"^ssh://git@([^:/]+)(?::\d+)?/(.+)$", url)
    if match:
        return f"https://{match.group(1)}/{match.group(2)}"
    return url


def _previous_host_or_raised(url: str) -> Optional[str]:
    try:
        return _previous_extract_host(url)
    except ValueError:
        return _RAISED


ADVERSARIAL = [
    "git@example.com:owner/repo.git",
    "https://user:pw@example.com:8443/owner/repo.git",
    "ssh://git@example.com:2222/owner/repo.git",
    "ssh://deploy@example.com:2222/owner/repo.git",
    "https://user:pw@[2001:db8::1]:8443/owner/repo.git",
    "https://User:pw@EXAMPLE.com/owner/repo.git",
    "https://user:pw@example.com:bad/owner/repo.git",
    "https://user:pw@example.com:/owner/repo.git",
    "https://example.com:08443/owner/repo.git",
    "https://example.com:443/owner/repo.git",
    "https://example.com:²/owner/repo.git",
    "http://example.com",
    "https://example.com",
    "HTTPS://example.com/owner/repo.git",
    "SSH://git@example.com/owner/repo.git",
    "ssh://git:pw@example.com/owner/repo.git",
    "ssh://git@evil.example@example.com/owner/repo.git",
    "ssh://example.com/owner/repo.git",
    "ssh://deploy@example.com/owner/repo.git",
    "ssh://git@[2001:db8::1]:2222/owner/repo.git",
    "ssh://git@example.com:bad/owner/repo.git",
    "ssh://git@example.com",
    "git@[2001:db8::1]:owner/repo.git",
    "deploy@example.com:owner/repo.git",
    "git@example.com/x:owner/repo.git",
    "git@a@example.com:owner/repo.git",
    "git@example.com:",
    "  git@example.com:owner/repo.git  ",
    "https://example.com\\@evil.example/owner/repo.git",
    "https://evil.example#@example.com/owner/repo.git",
    "https://evil.example?@example.com/owner/repo.git",
    "https://user@evil.example@example.com/owner/repo.git",
    "https://ex\tample.com/owner/repo.git",
    "https://[2001:db8::1/owner/repo.git",
    # '[' or ']' outside a well-formed bracketed IPv6 host literal.
    "https://bad[.example@forge.example/o/r",
    "https://bad].example@forge.example/o/r",
    "https://user:p[w@forge.example/o/r",
    "https://[2001:db8::1]x@forge.example/o/r",
    "ssh://git[@forge.example/o/r",
    "git[@forge.example:o/r",
    "https://[:]/o/r",
    "http://[:8443]/o/r",
    "git://example.com/owner/repo.git",
    "file:///srv/repos/repo",
    "/srv/repos/repo",
    "not-a-url",
    "",
]


@pytest.mark.parametrize("url", ADVERSARIAL)
def test_credential_scope_is_never_wider_than_before(url: str) -> None:
    previous = _previous_host_or_raised(url)
    current = credential_scope_host(url)

    assert current is None or current == previous, (url, previous, current)


@pytest.mark.parametrize(
    "url,host",
    [
        ("git@example.com:owner/repo.git", "example.com"),
        ("https://user:pw@example.com:8443/owner/repo.git", "example.com:8443"),
        ("ssh://git@example.com:2222/owner/repo.git", "example.com"),
        ("https://user:pw@[2001:db8::1]:8443/owner/repo.git", "[2001:db8::1]:8443"),
        ("https://User:pw@EXAMPLE.com/owner/repo.git", "EXAMPLE.com"),
        ("  git@example.com:owner/repo.git  ", "example.com"),
        # Previously scoped to a host that is not the URL's host; now none.
        ("https://user:pw@example.com:bad/owner/repo.git", None),
        ("https://user:pw@example.com:/owner/repo.git", None),
        # A non-git SSH user never selects a credential.
        ("ssh://deploy@example.com:2222/owner/repo.git", None),
        ("deploy@example.com:owner/repo.git", None),
        ("ssh://git:pw@example.com/owner/repo.git", None),
        ("SSH://git@example.com/owner/repo.git", None),
        ("HTTPS://example.com/owner/repo.git", None),
        # Bracket characters outside an IPv6 host literal select nothing.
        ("https://bad[.example@forge.example/o/r", None),
        ("https://user:p[w@forge.example/o/r", None),
        ("git[@forge.example:o/r", None),
    ],
)
def test_credential_scope_examples(url: str, host: Optional[str]) -> None:
    assert credential_scope_host(url) == host


@pytest.mark.parametrize("url", ADVERSARIAL)
def test_credential_helper_scopes_through_the_shared_function(url: str) -> None:
    assert GitCredentialHelper.extract_host_from_remote_url(
        url
    ) == credential_scope_host(url)


@pytest.mark.parametrize("url", ADVERSARIAL)
def test_ssh_to_https_converts_only_what_was_converted_before(url: str) -> None:
    previous = _previous_convert_ssh_to_https(url)
    current = GitCredentialHelper.convert_ssh_to_https(url)

    if current != url.strip():
        assert previous != url.strip(), (url, current)
        assert git_remote_host(current) == git_remote_host(previous), (url, current)
