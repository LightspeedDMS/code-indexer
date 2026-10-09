"""The single URL userinfo redaction rule (mask_url_credentials) and the
repository-record helper built on it (with_masked_repo_url).

Rule: the whole userinfo of a ``scheme://`` URL becomes ``***``, whatever
the scheme and whether or not a password part is present; scheme, host,
port and path are unchanged. scp-style addresses, local paths and
non-strings are returned unchanged.

Hosts and secrets are neutral placeholders.
"""

from __future__ import annotations

from typing import Optional

import pytest

from code_indexer.utils.credential_redaction import (
    mask_url_credentials,
    with_masked_repo_url,
)


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://example-user:example-token-123@git.example.com/org/repo.git",
            "https://***@git.example.com/org/repo.git",
        ),
        # A token alone as the userinfo (common for personal access tokens).
        (
            "https://example-token-123@git.example.com/org/repo.git",
            "https://***@git.example.com/org/repo.git",
        ),
        (
            "http://example-user:example-token-123@git.example.com/org/repo.git",
            "http://***@git.example.com/org/repo.git",
        ),
        (
            "https://oauth2:example-token-123@git.example.com:8443/org/repo.git",
            "https://***@git.example.com:8443/org/repo.git",
        ),
        (
            "https://example-user:example-token-123@[2001:db8::1]:8443/org/repo.git",
            "https://***@[2001:db8::1]:8443/org/repo.git",
        ),
        # Percent-encoded characters inside the secret are masked with it.
        (
            "https://example-user:p%40ss%3Aword@git.example.com/repo.git",
            "https://***@git.example.com/repo.git",
        ),
        # ssh:// URLs follow the same rule: any userinfo is masked.
        (
            "ssh://git@git.example.com/org/repo.git",
            "ssh://***@git.example.com/org/repo.git",
        ),
        (
            "ssh://example-user:example-token-123@git.example.com:2222/repo.git",
            "ssh://***@git.example.com:2222/repo.git",
        ),
    ],
)
def test_userinfo_is_replaced_by_mask(url: str, expected: str) -> None:
    masked = mask_url_credentials(url)
    assert masked == expected
    assert "example-token-123" not in masked
    assert "example-user" not in masked


@pytest.mark.parametrize(
    "value",
    [
        "https://git.example.com/org/repo.git",
        "https://git.example.com:8443/org/repo.git",
        "https://[2001:db8::1]:8443/org/repo.git",
        # scp-style address: a username, no secret, no scheme separator.
        "git@git.example.com:org/repo.git",
        "/srv/repos/example-repo",
        "/srv/repos/user@host/example-repo",
        "file:///srv/repos/example-repo",
        "local://example-repo",
        "not a url",
        "",
    ],
)
def test_values_without_scheme_userinfo_are_unchanged(value: str) -> None:
    assert mask_url_credentials(value) == value


@pytest.mark.parametrize("value", [None, 123])
def test_non_strings_pass_through(value: Optional[int]) -> None:
    assert mask_url_credentials(value) is value


@pytest.mark.parametrize(
    "text,expected",
    [
        # An unencoded '@' inside the password: masked through the LAST '@'
        # before the host.
        (
            "https://example-user:pa@ss@git.example.com/r.git",
            "https://***@git.example.com/r.git",
        ),
        (
            "https://a@b@c@git.example.com:8443/r.git",
            "https://***@git.example.com:8443/r.git",
        ),
        # '@' after the host (query, fragment, path) is not userinfo.
        (
            "https://git.example.com?owner=a@b",
            "https://git.example.com?owner=a@b",
        ),
        (
            "https://git.example.com#a@b",
            "https://git.example.com#a@b",
        ),
        (
            "https://git.example.com/users/a@b/r.git",
            "https://git.example.com/users/a@b/r.git",
        ),
        # Several URLs in one text are masked independently.
        (
            "fatal: https://u:t1@h1.example.com/x and https://v:t2@h2.example.com/y",
            "fatal: https://***@h1.example.com/x and https://***@h2.example.com/y",
        ),
    ],
)
def test_userinfo_is_masked_through_the_last_at_before_the_host(
    text: str, expected: str
) -> None:
    assert mask_url_credentials(text) == expected
    assert mask_url_credentials(expected) == expected


def test_masking_is_idempotent() -> None:
    once = mask_url_credentials("https://example-token-123@git.example.com/r.git")
    assert mask_url_credentials(once) == once


def test_with_masked_repo_url_masks_a_copy() -> None:
    record = {
        "alias": "example-repo",
        "repo_url": "https://example-user:example-token-123@git.example.com/r.git",
    }
    masked = with_masked_repo_url(record)
    assert masked == {
        "alias": "example-repo",
        "repo_url": "https://***@git.example.com/r.git",
    }
    assert record["repo_url"].startswith("https://example-user:")


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://git.example.com/repo.git?access_token=example-token-123",
            "https://git.example.com/repo.git?access_token=***",
        ),
        (
            "https://git.example.com/repo.git?ref=main&private_token=example-token-123",
            "https://git.example.com/repo.git?ref=main&private_token=***",
        ),
        (
            "https://git.example.com/r.git?access%5Ftoken=example-token-123&ref=main",
            "https://git.example.com/r.git?access%5Ftoken=***&ref=main",
        ),
        (
            "https://u:p@git.example.com/r.git?pass%77ord=example-token-123",
            "https://***@git.example.com/r.git?pass%77ord=***",
        ),
        (
            "https://git.example.com/r.git?ref=main&depth=1",
            "https://git.example.com/r.git?ref=main&depth=1",
        ),
    ],
)
def test_secret_named_query_parameters_are_masked(url: str, expected: str) -> None:
    assert mask_url_credentials(url) == expected
    assert mask_url_credentials(expected) == expected
    assert with_masked_repo_url({"repo_url": url}) == {"repo_url": expected}


def test_with_masked_repo_url_keeps_records_without_a_url() -> None:
    assert with_masked_repo_url({"alias": "example-repo"}) == {"alias": "example-repo"}
    assert with_masked_repo_url({"repo_url": None}) == {"repo_url": None}
