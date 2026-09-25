"""
Shared SSH config input validation: hostname / key-name grammar.

Covers the strict hostname / key-name grammar -- the single source of
truth every entry point (SSHKeyGenerator, SSHKeyManager, SSHConfigManager,
SSHKeySyncService, the REST router and the MCP handler) delegates to, so
the grammar is tested exactly once, here.

Discriminating RED: every config-line-breaking case below is a value that
the OLD blocklist-only validators (`SSHKeyGenerator._validate_key_name`,
`SSHConfigManager._format_host_block` with zero filtering) accepted and wrote
straight into ``~/.ssh/config``. Without this module these tests fail
because the module under test does not exist.

Two more discriminating classes, both proven RED against an earlier
implementation via direct interpreter execution:
  - ``re.match(pattern + "$", value)`` accepts a value with a trailing
    ``\\n`` -- Python's ``$`` anchor matches just before a trailing newline,
    not only at the true end of string. ``re.match(r"^[A-Za-z0-9._-]+$",
    "key\\n")`` is truthy. Fixed with ``fullmatch()``.
  - ``ipaddress.ip_address()`` accepts an RFC 4007 SCOPED IPv6 literal
    (a zone id after ``%``), e.g. ``ipaddress.ip_address("fe80::1%h")``
    succeeds -- letting OpenSSH's ``%h``/``%n`` token-expansion sequences
    reach the ``HostName`` line disguised as a "valid" IPv6 address. Fixed
    by rejecting ``%`` (and everything outside the hostname charset) BEFORE
    the ``ipaddress.ip_address()`` fast path.
"""

from __future__ import annotations

import pytest

from code_indexer.server.services.ssh_input_validation import (
    InvalidHostnameError,
    SSHConfigFormatError,
    has_control_characters,
    is_valid_hostname,
    is_valid_key_name,
    validate_hostname,
)


# ---------------------------------------------------------------------------
# Legitimate values must keep working
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostname",
    [
        "github.com",
        "gitlab.example.com",
        "192.0.2.10",  # RFC 5737 TEST-NET-1
        "2001:db8::1",  # RFC 3849 documentation IPv6 range
        "localhost",
        "a",
        # A single trailing DNS-root dot is a legitimate
        # FQDN form that cannot add anything to the HostName line --
        # see tests/unit/server/services/test_ssh_config_grammar_widening_regression.py.
        "example.com.",
    ],
)
def test_is_valid_hostname_accepts_legitimate_values(hostname: str) -> None:
    assert is_valid_hostname(hostname) is True


def test_is_valid_hostname_accepts_unscoped_ipv6() -> None:
    """Regression guard for zone-id rejection: a genuinely unscoped IPv6 literal
    (no '%' zone id) must still validate -- only the scoped form is rejected."""
    assert is_valid_hostname("fe80::1") is True


@pytest.mark.parametrize(
    "key_name",
    [
        "deploy-key_1.v2",
        "GitLab",
        "assign_test_key",
        "a",
        "my.key-name_2",
        # A leading '.' is safe as long as the key file
        # path stays contained (it does -- see the widening regression
        # suite).
        ".hidden",
    ],
)
def test_is_valid_key_name_accepts_legitimate_values(key_name: str) -> None:
    assert is_valid_key_name(key_name) is True


# ---------------------------------------------------------------------------
# Discriminating RED: hostnames that would break their config line must be
# rejected.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostname",
    [
        "example.com\nHost other",
        "example.com\r\nHost other",
        "example.com Host=other",  # embedded space
        "exa mple.com",
        "example.com%h",  # OpenSSH token expansion char
        "example.com;id",
        "example.com,other.com",
        'example.com"',
        "example.com'",
        "*.example.com",  # glob char
        "example.com!",
        "example.com?",
        "",
        "." * 260,  # over-length
        "example.com..",  # more than one trailing dot must still be rejected
    ],
)
def test_is_valid_hostname_rejects_config_line_breaking_characters(
    hostname: str,
) -> None:
    assert is_valid_hostname(hostname) is False


def test_validate_hostname_raises_on_hostname_with_newline() -> None:
    with pytest.raises(InvalidHostnameError):
        validate_hostname("example.com\nHost other")


def test_validate_hostname_error_message_never_echoes_raw_newline() -> None:
    """The rejection message must not itself contain a literal newline --
    it must use a representation (repr()) that escapes control characters,
    so an unsafe value can never add a line to a log via the message."""
    hostname_with_newline = "example.com\nHost other"
    with pytest.raises(InvalidHostnameError) as exc_info:
        validate_hostname(hostname_with_newline)
    assert "\n" not in str(exc_info.value)


def test_validate_hostname_accepts_legitimate_value_without_raising() -> None:
    validate_hostname("github.com")  # must not raise


# ---------------------------------------------------------------------------
# Trailing-newline rejection, hostname side
# ---------------------------------------------------------------------------


def test_is_valid_hostname_rejects_trailing_newline() -> None:
    """re.match(pattern + '$', ...) accepts a trailing '\\n' -- this is the
    exact discriminating probe. Proven RED against the
    match()-based implementation via direct interpreter execution:
    re.match(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$", "com\\n") was
    truthy."""
    assert is_valid_hostname("example.com\n") is False


def test_is_valid_hostname_rejects_trailing_carriage_return() -> None:
    assert is_valid_hostname("example.com\r") is False


def test_is_valid_hostname_rejects_trailing_newline_after_ip_literal() -> None:
    """The IP-literal fast path must not be fooled by a trailing newline
    either -- ipaddress.ip_address("192.0.2.10\\n") already raises ValueError
    on its own, but this proves the overall function still rejects it end
    to end."""
    assert is_valid_hostname("192.0.2.10\n") is False


# ---------------------------------------------------------------------------
# Scoped-IPv6 zone-id rejection ('%h'/'%n'/'%eth0')
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostname",
    [
        "fe80::1%h",  # OpenSSH %h (remote hostname) token
        "fe80::1%n",  # OpenSSH %n (original remote hostname) token
        "fe80::1%eth0",  # ordinary RFC 4007 zone id -- also must be rejected
        "fe80::1%25eth0",  # percent-encoded zone id form
    ],
)
def test_is_valid_hostname_rejects_scoped_ipv6(hostname: str) -> None:
    """Proven RED via direct interpreter execution:
    ipaddress.ip_address("fe80::1%h") returned a VALID IPv6Address object,
    so an is_valid_hostname() relying on it alone would report this
    hostname as safe, and the %h token would reach the HostName line."""
    assert is_valid_hostname(hostname) is False


# ---------------------------------------------------------------------------
# Discriminating RED: key names that would break their config line must be
# rejected.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key_name",
    [
        "deploy key\nHost other",  # newline
        "deploy key",  # bare space
        "deploy%key",
        "deploy;key",
        "../other",
        "..",
        "-flag-like",
        "a/b",
        "",
        "a" * 256,
    ],
)
def test_is_valid_key_name_rejects_config_line_breaking_characters(
    key_name: str,
) -> None:
    assert is_valid_key_name(key_name) is False


# ---------------------------------------------------------------------------
# Trailing-newline rejection, key-name side
# ---------------------------------------------------------------------------


def test_is_valid_key_name_rejects_trailing_newline() -> None:
    """Proven RED against the match()-based implementation via direct
    interpreter execution: re.match(r"^[A-Za-z0-9._-]+$", "key\\n") was
    truthy."""
    assert is_valid_key_name("key\n") is False


def test_is_valid_key_name_rejects_trailing_carriage_return() -> None:
    assert is_valid_key_name("key\r") is False


# ---------------------------------------------------------------------------
# has_control_characters: used as the format-time backstop for key_path
# ---------------------------------------------------------------------------


def test_has_control_characters_detects_embedded_newline() -> None:
    assert has_control_characters("/home/svc/.ssh/deploy\nHost other") is True


def test_has_control_characters_false_for_normal_path() -> None:
    assert has_control_characters("/home/svc/.ssh/deploy-key_1.v2") is False


def test_ssh_config_format_error_is_a_distinct_exception_type() -> None:
    # Only asserts the type exists and is a real Exception subclass -- the
    # format-time backstop that raises it is exercised in
    # test_ssh_config_manager_host_validation.py.
    assert issubclass(SSHConfigFormatError, Exception)
