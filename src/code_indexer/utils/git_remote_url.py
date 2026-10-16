"""The single parser of git remote URLs, shared by the CLI and the server.

Supported forms:
  - https://[userinfo@]host[:port]/owner/repo[.git]
  - http://[userinfo@]host[:port]/owner/repo[.git]
  - ssh://[user@]host[:port]/owner/repo[.git]
  - user@host:owner/repo[.git] (SSH scp-style, any user)

Every consumer (credential host scoping, SSH->HTTPS conversion, owner/repo
extraction, repository identity) goes through ``parse_git_remote_url``.
Standard library only: importing this module loads nothing from the server.
"""

import ipaddress
import re
from dataclasses import dataclass
from typing import NamedTuple, Optional, Tuple

_DEFAULT_PORTS = {"https": 443, "http": 80, "ssh": 22, "git": 9418}


@dataclass(frozen=True)
class GitRemoteUrl:
    """A parsed git remote URL. Never holds http(s) userinfo or a password;
    ``user`` is only an ssh login name.

    ``scheme`` is "https", "http", "ssh" (ssh://, git+ssh:// and scp-style
    alike) or "git" (the git daemon protocol); ``host`` keeps its case, an
    IPv6 literal without brackets; ``path`` has no leading or trailing '/'
    and keeps any ``.git`` suffix.
    """

    scheme: str
    user: Optional[str]
    host: str
    port: Optional[int]
    path: str

    @property
    def web_host(self) -> str:
        """The forge's web host, the scope of stored credentials:
        ``host[:port]`` for http(s); the bare host for ssh and git, whose
        port is never the web port."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        if self.scheme in ("http", "https") and self.port is not None:
            return f"{host}:{self.port}"
        return host

    @property
    def repo_path(self) -> str:
        """``path`` without its ``.git`` suffix."""
        return self.path[: -len(".git")] if self.path.endswith(".git") else self.path

    @property
    def identity(self) -> str:
        """``<web_host>/<repo_path>``: the same for every transport, user,
        port and credential that reaches one repository."""
        return f"{self.web_host}/{self.repo_path}"

    @property
    def endpoint_identity(self) -> str:
        """``<host>[:<port>]/<repo_path>``: the transport endpoint and
        repository, the identity of one repository on one server.

        https, http and ssh on their default ports reach the same server,
        so they share an identity. An explicit non-default port selects a
        different service on that host (e.g. two SSH daemons on 2222 and
        2223) and is kept: keeping two forms of one repository apart only
        forgoes a dedup, while merging two repositories would refuse a
        legitimate operation.
        """
        if ":" in self.host:
            host = f"[{self.host}]"
        else:
            # A fully-qualified name with its root dot names the same host.
            host = self.host[:-1] if self.host.endswith(".") else self.host
        if self.port is not None and self.port != _DEFAULT_PORTS[self.scheme]:
            host = f"{host}:{self.port}"
        return f"{host}/{self.repo_path}"

    def owner_repo(self) -> Tuple[str, str]:
        """(owner, repo); owner may hold subgroups ("group/sub").

        Raises:
            ValueError: The path has no owner segment.
        """
        owner, _, repo = self.repo_path.rpartition("/")
        if not owner or not repo:
            raise ValueError("Git remote URL path has no owner/repo")
        return owner, repo

    def to_https(self, userinfo: Optional[str] = None) -> str:
        """The https URL of this repository on its web host; credential-free
        unless ``userinfo`` is given."""
        prefix = f"{userinfo}@" if userinfo else ""
        return f"https://{prefix}{self.web_host}/{self.path}"


# Accepted URL schemes and the scheme they parse to.
_REMOTE_URL_SCHEMES = {
    "https": "https",
    "http": "http",
    "ssh": "ssh",
    "git+ssh": "ssh",
    "ssh+git": "ssh",
    "git": "git",
}
_AUTHORITY_URL = re.compile(
    r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?P<authority>[^/?#]*)(?P<rest>.*)$",
    re.S,
)


class UrlAuthority(NamedTuple):
    """A ``<scheme>://<authority><rest>`` URL split at its authority.

    ``scheme`` is as written; ``userinfo`` is everything before the
    authority's LAST '@' (as git and ``urlsplit`` read it), None without
    one; ``rest`` is the path, query and fragment."""

    scheme: str
    userinfo: Optional[str]
    hostport: str
    rest: str


def split_url_authority(url: str) -> Optional[UrlAuthority]:
    """Split ``url`` (surrounding whitespace removed) at its authority; None
    for a value without a ``<scheme>://`` authority (scp-like, local path).
    Never raises."""
    match = _AUTHORITY_URL.match(url.strip())
    if match is None:
        return None
    userinfo, at, hostport = match.group("authority").rpartition("@")
    return UrlAuthority(
        scheme=match.group("scheme"),
        userinfo=userinfo if at else None,
        hostport=hostport,
        rest=match.group("rest"),
    )


_SCP_LIKE_URL = re.compile(
    r"^(?P<user>[^@/:\s\[\]?#]+)@(?P<host>\[[^\]/]+\]|[^:/\[\]\s]+):(?P<path>.*)$"
)
# Matched with fullmatch(): '$' would also accept a trailing newline.
_HOSTNAME = re.compile(r"[A-Za-z0-9._~%-]+")
# An IPv6 literal always holds a ':'.
_IPV6_LITERAL = re.compile(r"[0-9A-Fa-f.]*:[0-9A-Fa-f:.]*(?:%[A-Za-z0-9._~-]+)?")
# A canonical ASCII decimal port: no sign, no leading zero, never empty.
_PORT = re.compile(r"[1-9][0-9]{0,4}")
_MAX_PORT = 65535


def _split_host_port(hostport: str) -> Optional[Tuple[str, Optional[int]]]:
    """(host, port) of an authority's host part; None when malformed. The
    host holds hostname characters only (or an IPv6 literal in brackets);
    a port, when given, is a canonical ASCII decimal number in 1-65535."""
    if hostport.startswith("["):
        host, bracket, rest = hostport[1:].partition("]")
        if not bracket or (rest and not rest.startswith(":")):
            return None
        if not _IPV6_LITERAL.fullmatch(host):
            return None
        try:
            ipaddress.IPv6Address(host.partition("%")[0])
        except ValueError:
            return None
        has_port, port_text = bool(rest), rest[1:]
    else:
        host, colon, port_text = hostport.partition(":")
        if not _HOSTNAME.fullmatch(host):
            return None
        has_port = bool(colon)
    if not has_port:
        return host, None
    if not _PORT.fullmatch(port_text) or int(port_text) > _MAX_PORT:
        return None
    return host, int(port_text)


def parse_git_remote_url(url: str) -> Optional[GitRemoteUrl]:
    """Parse an http(s), ssh:// or scp-style (``user@host:path``) git remote
    URL; None for anything else (local paths, file://, other schemes,
    malformed authorities). Never raises. The userinfo of a URL runs up to
    the LAST '@' of its authority. Only an ssh login name is kept: http(s)
    userinfo is a credential and is never retained."""
    text = url.strip()
    split = split_url_authority(text)
    if split is not None:
        mapped = _REMOTE_URL_SCHEMES.get(split.scheme.lower())
        if mapped is None:
            return None
        scheme = mapped
        userinfo, hostport = split.userinfo or "", split.hostport
        # '[' and ']' are valid only around an IPv6 host literal.
        if "[" in userinfo or "]" in userinfo:
            return None
        user: Optional[str] = None
        if split.userinfo is not None and scheme == "ssh":
            user = userinfo.partition(":")[0]
        path = re.split(r"[?#]", split.rest, maxsplit=1)[0]
    else:
        scp_match = _SCP_LIKE_URL.match(text)
        if scp_match is None:
            return None
        scheme, user = "ssh", scp_match.group("user")
        hostport, path = scp_match.group("host"), scp_match.group("path")
    host_port = _split_host_port(hostport)
    if host_port is None:
        return None
    return GitRemoteUrl(
        scheme=scheme,
        user=user or None,
        host=host_port[0],
        port=host_port[1],
        path=path.strip("/"),
    )


def git_remote_host(url: str) -> Optional[str]:
    """The forge web host of a git remote URL (``GitRemoteUrl.web_host``),
    the key that scopes stored credentials; None when ``url`` is not a git
    remote URL."""
    parsed = parse_git_remote_url(url) if url else None
    return parsed.web_host if parsed is not None else None


# The literal forms whose host may scope a stored credential.
_CREDENTIAL_SCOPED_PREFIXES = ("https://", "http://", "ssh://git@", "git@")
_CREDENTIAL_SSH_USER = "git"


def credential_scope_host(url: str) -> Optional[str]:
    """The host a stored credential may be used for, or None.

    Invariant: only the literal forms ``https://``, ``http://``,
    ``ssh://git@`` and ``git@host:`` (case-sensitive) are scoped; an SSH
    form only with login user ``git`` and a non-IPv6 host. The result is
    the parsed ``web_host``: ``host[:port]`` for http(s), the bare host for
    SSH. Any other value scopes no credential.
    """
    text = url.strip() if url else ""
    if not text.startswith(_CREDENTIAL_SCOPED_PREFIXES):
        return None
    parsed = parse_git_remote_url(text)
    if parsed is None:
        return None
    if parsed.scheme == "ssh" and (
        parsed.user != _CREDENTIAL_SSH_USER or ":" in parsed.host
    ):
        return None
    return parsed.web_host
