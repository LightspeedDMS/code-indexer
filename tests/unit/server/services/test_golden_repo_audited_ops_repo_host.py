"""Epic #2103 item 15: the audit host of a golden-repo clone URL comes from
the single git URL parser: the lower-cased bare host, never a port, the URL
or its userinfo."""

from typing import Optional

import pytest

from code_indexer.server.services.golden_repo_audited_ops import repo_host
from code_indexer.utils.git_remote_url import parse_git_remote_url
from tests.unit.utils.test_git_remote_url import PARSE_TABLE


@pytest.mark.parametrize(
    "url,host",
    [
        ("https://user:s3cr3t@Git.Example.com:8443/o/r.git", "git.example.com"),
        ("http://git.example.com/o/r", "git.example.com"),
        ("git@Git.Example.com:o/r.git", "git.example.com"),
        ("deploy@git.example.com:o/r.git", "git.example.com"),
        ("ssh://deploy@git.example.com:2222/o/r.git", "git.example.com"),
        ("git://git.example.com/o/r.git", "git.example.com"),
        ("git+ssh://git@git.example.com/o/r.git", "git.example.com"),
        ("https://[2001:DB8::1]:8443/o/r.git", "2001:db8::1"),
        ("file:///srv/repos/r", None),
        ("/srv/repos/r", None),
        ("ftp://git.example.com/o/r.git", None),
        ("", None),
        (None, None),
        (42, None),
    ],
)
def test_repo_host(url: object, host: Optional[str]) -> None:
    assert repo_host(url) == host


@pytest.mark.parametrize("url", sorted(PARSE_TABLE))
def test_repo_host_is_the_parsed_host(url: str) -> None:
    parsed = parse_git_remote_url(url)
    assert parsed is not None

    assert repo_host(url) == parsed.host.lower()
