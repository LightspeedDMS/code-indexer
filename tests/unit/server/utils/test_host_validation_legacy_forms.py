"""
Shared server-host validator: hostname forms that were accepted before the
validator existed keep validating, while the restricted character set stays
enforced.

- A fully-qualified name with a trailing root dot ("host.example.com.") and
  hostnames containing "_" ("node_1.example.internal") are accepted.
- normalize_server_host() is the single normalisation step every layer uses:
  surrounding whitespace is stripped once and the stripped value is what is
  validated and stored. Interior whitespace/line breaks are still rejected.
- Newline, space, quotes, "%", "/", ";", "$", backtick, "\\", "=" and IPv6
  zone ids stay rejected.
"""

import pytest

from code_indexer.server.utils.host_validation import (
    is_valid_server_host,
    normalize_server_host,
    validate_server_host,
)

LEGACY_ACCEPTED_HOSTS = [
    "host.example.com.",
    "node_1.example.internal",
    "_node.example.internal",
    "node_1",
    "localhost.",
    "node_1.localhost.",
    "3com.example.",
]

# A name whose labels are ALL numeric (decimal or 0x/0X hex) must be a
# canonical IPv4 literal. Short, octal, hex and single-integer forms are
# expanded by the resolver ("127.1" and "0x7f.0.0.1" bind 127.0.0.1,
# "1.2.3" binds 1.2.0.3), so the bound address would differ from what the
# text appears to say; with a trailing dot the resolver cannot look them up
# at all.
NUMERIC_NON_CANONICAL_HOSTS = [
    "127.1",
    "0177.0.0.1",
    "0x7f.0.0.1",
    "0X7F.0.0.1",
    "2130706433",
    "1.2.3",
    "1.2.3.4.5",
    "127.0.0.1.",
    "10.0.0.1.",
    "192.0.2.10.",
    "0.0.0.0.",
    "1.2.3.",
    "::1.",
]

# Canonical IP literals, and names with at least one non-numeric label.
NUMERIC_TABLE_ACCEPTED_HOSTS = [
    "3com.example",
    "123.example.com",
    "0x7f.example.com",
    "192.0.2.10",
    "::1",
    "10.0.0.1",
    "host.example.com.",
    "node_1.example.internal",
    "localhost",
]

STILL_REJECTED_HOSTS = [
    "host.example.com\n",
    "host example.com",
    'host".example.com',
    "host'.example.com",
    "host%.example.com",
    "fe80::1%eth0",
    "host/.example.com",
    "host;.example.com",
    "host$.example.com",
    "host`.example.com",
    "host\\.example.com",
    "host=.example.com",
    ".",
    "..",
    "host..example.com",
    ".host.example.com",
    "host.example.com..",
    "-host.example.com",
    "host-.example.com",
]


@pytest.mark.parametrize("host", LEGACY_ACCEPTED_HOSTS)
def test_legacy_hostname_forms_are_accepted(host):
    assert is_valid_server_host(host) is True, f"expected {host!r} to be valid"


@pytest.mark.parametrize("host", STILL_REJECTED_HOSTS)
def test_restricted_character_set_still_enforced(host):
    assert is_valid_server_host(host) is False, f"expected {host!r} to be invalid"


@pytest.mark.parametrize("host", NUMERIC_NON_CANONICAL_HOSTS)
def test_all_numeric_name_that_is_not_a_canonical_ip_is_rejected(host):
    assert is_valid_server_host(host) is False, f"expected {host!r} to be invalid"


@pytest.mark.parametrize("host", NUMERIC_TABLE_ACCEPTED_HOSTS)
def test_canonical_ip_and_names_with_a_non_numeric_label_are_accepted(host):
    assert is_valid_server_host(host) is True, f"expected {host!r} to be valid"


def test_error_message_explains_the_canonical_ipv4_rule():
    with pytest.raises(ValueError) as exc_info:
        validate_server_host("127.1")
    assert "canonical IPv4" in str(exc_info.value)


def test_error_message_describes_the_accepted_hostname_form():
    with pytest.raises(ValueError) as exc_info:
        validate_server_host("bad host")
    message = str(exc_info.value)
    assert "underscores" in message
    assert "trailing dot" in message


class TestNormalizeServerHost:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("  192.0.2.10  ", "192.0.2.10"),
            ("host.example.com.\n", "host.example.com."),
            ("\tnode_1.example.internal ", "node_1.example.internal"),
            ("0.0.0.0", "0.0.0.0"),
        ],
    )
    def test_returns_stripped_value(self, raw, expected):
        assert normalize_server_host(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            " host.example.com\nExecStartPre=+/bin/true ",
            "host .example.com",
            "   ",
            "",
            "fe80::1%eth0",
        ],
    )
    def test_rejects_invalid_after_stripping(self, raw):
        with pytest.raises(ValueError):
            normalize_server_host(raw)

    @pytest.mark.parametrize("raw", [None, 123, ["example.com"]])
    def test_rejects_non_string(self, raw):
        with pytest.raises(ValueError):
            normalize_server_host(raw)
