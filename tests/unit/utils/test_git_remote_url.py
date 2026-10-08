"""Epic #2103 item 15: the single git remote URL parser.

``code_indexer.utils.git_remote_url`` is the one implementation every
git-URL consumer uses, in the CLI and the server alike: scheme, user, host,
port, path, the forge's web host (credential host scope), the repository
identity, SSH->HTTPS conversion and owner/repo extraction.
"""

import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

import pytest

import code_indexer
from code_indexer.utils.git_remote_url import git_remote_host, parse_git_remote_url

# url -> (scheme, user, host, port, path, web_host, repo_path)
_Row = Tuple[str, Optional[str], str, Optional[int], str, str, str]

PARSE_TABLE = {
    # The git daemon protocol: no user; its port is never a web port.
    "git://git.example.com/owner/repo.git": (
        "git",
        None,
        "git.example.com",
        None,
        "owner/repo.git",
        "git.example.com",
        "owner/repo",
    ),
    # git+ssh:// is ssh.
    "git+ssh://git@git.example.com:2222/owner/repo.git": (
        "ssh",
        "git",
        "git.example.com",
        2222,
        "owner/repo.git",
        "git.example.com",
        "owner/repo",
    ),
    "https://github.com/owner/repo.git": (
        "https",
        None,
        "github.com",
        None,
        "owner/repo.git",
        "github.com",
        "owner/repo",
    ),
    "http://git.example.com/owner/repo": (
        "http",
        None,
        "git.example.com",
        None,
        "owner/repo",
        "git.example.com",
        "owner/repo",
    ),
    # http(s) userinfo is a credential and is never retained.
    "https://user:secret@git.example.com:8443/group/sub/repo.git": (
        "https",
        None,
        "git.example.com",
        8443,
        "group/sub/repo.git",
        "git.example.com:8443",
        "group/sub/repo",
    ),
    # The userinfo runs up to the LAST '@' of the authority (a password may
    # contain '@').
    "https://oauth2:tok@en@git.example.com/owner/repo.git": (
        "https",
        None,
        "git.example.com",
        None,
        "owner/repo.git",
        "git.example.com",
        "owner/repo",
    ),
    "ssh://git@github.com/owner/repo.git": (
        "ssh",
        "git",
        "github.com",
        None,
        "owner/repo.git",
        "github.com",
        "owner/repo",
    ),
    "ssh://deploy@git.example.com/owner/repo.git": (
        "ssh",
        "deploy",
        "git.example.com",
        None,
        "owner/repo.git",
        "git.example.com",
        "owner/repo",
    ),
    # The SSH port is not part of the forge's web host.
    "ssh://git@git.example.com:2222/owner/repo.git": (
        "ssh",
        "git",
        "git.example.com",
        2222,
        "owner/repo.git",
        "git.example.com",
        "owner/repo",
    ),
    "ssh://git.example.com/owner/repo": (
        "ssh",
        None,
        "git.example.com",
        None,
        "owner/repo",
        "git.example.com",
        "owner/repo",
    ),
    "git@github.com:owner/repo.git": (
        "ssh",
        "git",
        "github.com",
        None,
        "owner/repo.git",
        "github.com",
        "owner/repo",
    ),
    "deploy@git.example.com:group/repo": (
        "ssh",
        "deploy",
        "git.example.com",
        None,
        "group/repo",
        "git.example.com",
        "group/repo",
    ),
    "https://[2001:db8::1]:8443/owner/repo.git": (
        "https",
        None,
        "2001:db8::1",
        8443,
        "owner/repo.git",
        "[2001:db8::1]:8443",
        "owner/repo",
    ),
    "ssh://git@[2001:db8::1]:2222/owner/repo.git": (
        "ssh",
        "git",
        "2001:db8::1",
        2222,
        "owner/repo.git",
        "[2001:db8::1]",
        "owner/repo",
    ),
    "git@[2001:db8::1]:owner/repo.git": (
        "ssh",
        "git",
        "2001:db8::1",
        None,
        "owner/repo.git",
        "[2001:db8::1]",
        "owner/repo",
    ),
    # Scheme is case-insensitive; host and path case are preserved.
    "HTTPS://Git.Example.com/Owner/Repo.git": (
        "https",
        None,
        "Git.Example.com",
        None,
        "Owner/Repo.git",
        "Git.Example.com",
        "Owner/Repo",
    ),
    "https://git.example.com/owner/repo.git/": (
        "https",
        None,
        "git.example.com",
        None,
        "owner/repo.git",
        "git.example.com",
        "owner/repo",
    ),
    "  git@github.com:owner/repo.git  ": (
        "ssh",
        "git",
        "github.com",
        None,
        "owner/repo.git",
        "github.com",
        "owner/repo",
    ),
    # Only a '.git' suffix is removed, never trailing letters of the name.
    "https://github.com/acme/widget": (
        "https",
        None,
        "github.com",
        None,
        "acme/widget",
        "github.com",
        "acme/widget",
    ),
    "git@github.com:acme/cli-git.git": (
        "ssh",
        "git",
        "github.com",
        None,
        "acme/cli-git.git",
        "github.com",
        "acme/cli-git",
    ),
    "https://git.example.com": (
        "https",
        None,
        "git.example.com",
        None,
        "",
        "git.example.com",
        "",
    ),
}

UNPARSEABLE = [
    "",
    "   ",
    "not-a-url",
    "/srv/repos/repo",
    "file:///srv/repos/repo",
    "ftp://git.example.com/owner/repo.git",
    "https://git.example.com:notaport/owner/repo.git",
    "https://git.example.com:99999/owner/repo.git",
    "https:///owner/repo.git",
    "https://[2001:db8::1/owner/repo.git",
    "https://a:b:c/owner/repo.git",
    # Ports are canonical ASCII decimal numbers in 1-65535.
    "https://git.example.com:²/owner/repo.git",
    "ssh://git@git.example.com:٢٢/owner/repo.git",
    "https://git.example.com:08443/owner/repo.git",
    "https://git.example.com:/owner/repo.git",
    "https://git.example.com:0/owner/repo.git",
    # A bracketed host is an IPv6 literal.
    "https://[a]/owner/repo.git",
    "https://[.]/owner/repo.git",
    "https://[1.2.3.4]/owner/repo.git",
    "git@[a]:owner/repo.git",
    # Hosts hold hostname characters only.
    "https://git.exa\tmple.com/owner/repo.git",
    "https://git example.com/owner/repo.git",
    "git@a@git.example.com:owner/repo.git",
    "git@git.example.com/x:owner/repo.git",
    "ssh://git@git.exa\nmple.com/owner/repo.git",
]


@pytest.mark.parametrize(
    "url,userinfo_parts",
    [
        ("https://single-token@git.example.com/owner/repo.git", ["single-token"]),
        (
            "http://example-user:example-pass@git.example.com:8080/o/r.git",
            ["example-user", "example-pass"],
        ),
        (
            "https://oauth2:tok@en@git.example.com/owner/repo.git",
            ["oauth2", "tok@en"],
        ),
    ],
)
def test_http_userinfo_is_never_retained(url: str, userinfo_parts: list) -> None:
    parsed = parse_git_remote_url(url)

    assert parsed is not None
    assert parsed.user is None
    for part in userinfo_parts:
        assert part not in repr(parsed)
        assert part not in str(parsed)


def _endpoint(url: str) -> str:
    parsed = parse_git_remote_url(url)
    assert parsed is not None
    return parsed.endpoint_identity


def test_default_port_forms_of_one_repository_share_an_endpoint() -> None:
    urls = [
        "https://git.example.com/owner/repo.git",
        "https://git.example.com:443/owner/repo",
        "http://git.example.com/owner/repo.git",
        "http://git.example.com:80/owner/repo.git",
        "https://user:pw@git.example.com/owner/repo.git",
        "git@git.example.com:owner/repo.git",
        "ssh://git@git.example.com/owner/repo.git",
        "ssh://deploy@git.example.com:22/owner/repo.git",
    ]

    assert {_endpoint(url) for url in urls} == {"git.example.com/owner/repo"}


def test_distinct_ssh_ports_are_distinct_endpoints() -> None:
    a = _endpoint("ssh://git@git.example.com:2222/owner/repo.git")
    b = _endpoint("ssh://git@git.example.com:2223/owner/repo.git")

    assert a == "git.example.com:2222/owner/repo"
    assert b == "git.example.com:2223/owner/repo"
    assert a != _endpoint("git@git.example.com:owner/repo.git")


def test_endpoint_keeps_non_default_web_ports_and_ipv6_brackets() -> None:
    assert (
        _endpoint("https://[2001:db8::1]:8443/owner/repo.git")
        == "[2001:db8::1]:8443/owner/repo"
    )
    assert _endpoint("http://git.example.com:8080/o/r") == _endpoint(
        "https://git.example.com:8080/o/r"
    )


def test_trailing_dot_host_shares_the_endpoint() -> None:
    urls = [
        "https://host.example/o/r.git",
        "https://host.example./o/r.git",
        "git@host.example.:o/r.git",
        "ssh://git@host.example.:2222/o/r.git",
    ]

    assert {_endpoint(url) for url in urls[:3]} == {"host.example/o/r"}
    assert _endpoint(urls[3]) == "host.example:2222/o/r"


def test_endpoint_never_holds_userinfo() -> None:
    assert "s3cr3t" not in _endpoint("https://u:s3cr3t@git.example.com/o/r.git")


def test_ssh_login_user_is_kept() -> None:
    parsed = parse_git_remote_url("ssh://deploy@git.example.com/owner/repo.git")

    assert parsed is not None
    assert parsed.user == "deploy"


@pytest.mark.parametrize("url", sorted(PARSE_TABLE))
def test_parse_git_remote_url_table(url: str) -> None:
    scheme, user, host, port, path, web_host, repo_path = PARSE_TABLE[url]

    parsed = parse_git_remote_url(url)

    assert parsed is not None
    assert parsed.scheme == scheme
    assert parsed.user == user
    assert parsed.host == host
    assert parsed.port == port
    assert parsed.path == path
    assert parsed.web_host == web_host
    assert parsed.repo_path == repo_path
    assert parsed.identity == f"{web_host}/{repo_path}"
    assert git_remote_host(url) == web_host


@pytest.mark.parametrize("url", UNPARSEABLE)
def test_parse_git_remote_url_rejects_non_remote_values(url: str) -> None:
    assert parse_git_remote_url(url) is None
    assert git_remote_host(url) is None


def test_identity_is_shared_by_every_form_of_one_repository() -> None:
    urls = [
        "https://git.example.com/owner/repo.git",
        "http://git.example.com/owner/repo/",
        "https://user:pw@git.example.com/owner/repo.git",
        "git@git.example.com:owner/repo.git",
        "deploy@git.example.com:owner/repo",
        "ssh://deploy@git.example.com:2222/owner/repo.git",
    ]

    identities = set()
    for url in urls:
        parsed = parse_git_remote_url(url)
        assert parsed is not None
        identities.add(parsed.identity)

    assert identities == {"git.example.com/owner/repo"}


def test_parsed_form_never_holds_the_password() -> None:
    parsed = parse_git_remote_url("https://user:s3cr3t-value@git.example.com/o/r.git")

    assert parsed is not None
    assert "s3cr3t-value" not in repr(parsed)
    assert "s3cr3t-value" not in parsed.to_https()
    assert "s3cr3t-value" not in parsed.identity


@pytest.mark.parametrize(
    "url,expected",
    [
        ("git@github.com:owner/repo.git", "https://github.com/owner/repo.git"),
        (
            "ssh://deploy@git.example.com:2222/group/repo.git",
            "https://git.example.com/group/repo.git",
        ),
        (
            "https://user:pw@git.example.com:8443/group/sub/repo.git",
            "https://git.example.com:8443/group/sub/repo.git",
        ),
        (
            "ssh://git@[2001:db8::1]:2222/owner/repo.git",
            "https://[2001:db8::1]/owner/repo.git",
        ),
    ],
)
def test_to_https_is_credential_free_web_url(url: str, expected: str) -> None:
    parsed = parse_git_remote_url(url)

    assert parsed is not None
    assert parsed.to_https() == expected


def test_to_https_with_userinfo_places_it_before_the_web_host() -> None:
    parsed = parse_git_remote_url("git@github.com:owner/repo.git")

    assert parsed is not None
    assert parsed.to_https(userinfo="tok") == "https://tok@github.com/owner/repo.git"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("git@github.com:owner/repo.git", ("owner", "repo")),
        ("https://gitlab.com/group/sub/repo.git/", ("group/sub", "repo")),
        ("ssh://deploy@git.example.com:2222/team/repo", ("team", "repo")),
    ],
)
def test_owner_repo(url: str, expected: Tuple[str, str]) -> None:
    parsed = parse_git_remote_url(url)

    assert parsed is not None
    assert parsed.owner_repo() == expected


@pytest.mark.parametrize(
    "url", ["https://github.com/owner", "git@github.com:repo.git", "https://h"]
)
def test_owner_repo_rejects_paths_without_owner(url: str) -> None:
    parsed = parse_git_remote_url(url)

    assert parsed is not None
    with pytest.raises(ValueError):
        parsed.owner_repo()


def test_module_imports_nothing_from_the_server_or_heavy_libraries() -> None:
    probe = (
        "import sys\n"
        "import code_indexer.utils.git_remote_url\n"
        "heavy = ('code_indexer.server', 'pydantic', 'fastapi', 'tantivy')\n"
        "print(sorted(m for m in sys.modules if m.startswith(heavy)))\n"
    )
    src_root = Path(code_indexer.__file__).resolve().parents[1]

    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PYTHONPATH": str(src_root), "PATH": "/usr/bin:/bin"},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
