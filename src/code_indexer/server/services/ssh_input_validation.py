"""
Shared SSH config input validation.

Single source of truth for the grammar every entry point that can influence
``~/.ssh/config`` content must enforce: ``SSHKeyGenerator`` (key names at
creation), ``SSHKeyManager`` (hostnames at assignment, and cluster-sourced
key names before they are resolved to a local path), ``SSHConfigManager``
(a format-time backstop, immediately before a value is written into a Host
block), ``SSHKeySyncService`` (rows read from the shared PostgreSQL/SQLite
backend, which may originate from another node), the REST router, and the
MCP handler.

The invariant this module enforces: a value written into an OpenSSH
``~/.ssh/config`` line must never contain a character that can end or
extend that line (a newline or carriage return), start a new token on the
same line (whitespace, a quote), or trigger OpenSSH's own token/variable
expansion (``%`` tokens, ``${VAR}`` expansion). A value that violates this
is rejected before it is ever written.
"""

from __future__ import annotations

import ipaddress
import re

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class InvalidHostnameError(Exception):
    """Raised when a hostname fails the strict grammar in ``validate_hostname``."""


class SSHConfigFormatError(Exception):
    """Raised by the format-time backstop in ``SSHConfigManager._format_host_block``.

    This should never fire in normal operation: every caller that reaches
    ``_format_host_block`` is expected to have already validated (or
    filtered out) its inputs. Firing here means a value that fails the
    grammar reached formatting despite upstream filtering -- a
    defensive-invariant assertion (Messi Rule #15), not an expected
    user-facing error.
    """


# ---------------------------------------------------------------------------
# Hostname grammar: RFC 1123 labels, or an IPv4/IPv6 literal.
# ---------------------------------------------------------------------------

# entry.host and entry.hostname are always the SAME literal string passed as
# `hostname` to SSHKeyManager.assign_key_to_host() (see
# SSHKeyManager._update_ssh_config: HostEntry(host=hostname,
# hostname=hostname, ...)) and it is written verbatim into OpenSSH's
# `HostName` directive. That directive does not support a `host:port`
# syntax (port belongs to the separate `Port` keyword), no call site or
# test in this codebase ever passes one, so a port suffix is deliberately
# NOT part of this grammar -- admitting `:` would only widen the accepted
# character set (colons are otherwise reserved for IPv6 literals) without
# enabling any real feature.
_HOSTNAME_MAX_LENGTH = 253
# A HostName/Host VALUE is never re-parsed as a command-line flag or a
# config keyword -- it is just a string token inside a keyword=value pair
# (confirmed via `ssh -G -F <config> <target>`: a leading '-' or a
# mid-label '_' resolves cleanly, no line change). So '_' and '-' are
# allowed ANYWHERE in a label; a label is otherwise just a bounded run of
# the allowed charset.
_HOSTNAME_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,63}$")

# The only characters accepted in an unbracketed hostname or IPv4/IPv6
# literal. No '%' is allowed anywhere, so no OpenSSH token-expansion
# sequence can appear in a hostname; quote, glob, whitespace and control
# characters are excluded by the same charset. It is enforced with
# fullmatch() before the ipaddress.ip_address() check below, because that
# function accepts an RFC 4007 scoped IPv6 literal ("fe80::1%eth0") whose
# zone id carries a '%'. '_' is included: it has no meaning on OpenSSH's
# HostName line.
_HOSTNAME_ALLOWED_CHARS_PATTERN = re.compile(r"[A-Za-z0-9._:-]+")


def _has_disallowed_control_character(value: str) -> bool:
    """True iff ``value`` contains a C0 control character (incl. \\r, \\n, \\t,
    NUL), DEL, or a C1 control character (0x80-0x9F).

    An explicit, independent check performed BEFORE any regex/ipaddress
    parsing -- belt-and-suspenders alongside the allowed-character patterns
    below, which already exclude every control character structurally.
    Kept as its own function (rather than reusing ``has_control_characters``)
    because it must also reject the C1 range, which the key_path backstop
    below intentionally does not need to.
    """
    return any(
        ord(ch) < 0x20 or ord(ch) == 0x7F or 0x80 <= ord(ch) <= 0x9F for ch in value
    )


def is_valid_hostname(value: object) -> bool:
    """True iff ``value`` is a safe RFC 1123 hostname or an IPv4/IPv6 literal.

    Rejects whitespace, newlines, control characters, quotes, ``%`` (OpenSSH
    token expansion, including via an RFC 4007 scoped IPv6 zone id), glob
    characters (``*?!``), commas, semicolons, and any other character
    outside the strict grammar -- these are exactly the characters that let
    a hostname break out of the ``HostName`` config line it is interpolated
    into.
    """
    if not isinstance(value, str) or not value:
        return False
    if len(value) > _HOSTNAME_MAX_LENGTH:
        return False

    # Explicit control-character rejection FIRST, independent of the regex
    # engine's own exclusion of these characters below.
    if _has_disallowed_control_character(value):
        return False

    # Reject anything outside the safe charset BEFORE the ipaddress fast
    # path -- this is what stops a scoped IPv6 literal ("fe80::1%h") from
    # ever reaching ipaddress.ip_address(), which accepts the zone id and
    # would otherwise report it "valid". fullmatch() (not match()) so a
    # trailing newline can never sneak past the '$' anchor's "just before a
    # trailing newline" quirk.
    if not _HOSTNAME_ALLOWED_CHARS_PATTERN.fullmatch(value):
        return False

    # An IP literal (v4 or unscoped v6) is accepted verbatim -- the charset
    # filter above has already excluded '%', so no scoped/zone-id literal
    # can reach this point.
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        pass

    # Exactly ONE trailing dot (the DNS root separator, e.g. "github.com.")
    # is accepted -- it cannot end or extend the HostName line. A bare "."
    # or a second trailing dot ("example.com..") stays rejected.
    label_source = value
    if label_source.endswith("."):
        label_source = label_source[:-1]
        if not label_source or label_source.endswith("."):
            return False

    labels = label_source.split(".")
    return all(_HOSTNAME_LABEL_PATTERN.fullmatch(label) for label in labels)


def validate_hostname(value: object) -> None:
    """Raise ``InvalidHostnameError`` unless ``value`` passes ``is_valid_hostname``.

    The rejected value is never echoed raw into the error message -- ``repr()``
    escapes control characters (a literal newline becomes the two characters
    ``\\n``), so a rejected value can never add a line to a log or error
    message through this exception.
    """
    if not is_valid_hostname(value):
        raise InvalidHostnameError(f"Invalid hostname: {value!r}")


# ---------------------------------------------------------------------------
# Key name grammar: denylist of config-line-breaking characters, not an
# ASCII-only allow-list.
# ---------------------------------------------------------------------------

_KEY_NAME_MAX_LENGTH = 255

# The key-name grammar is a denylist: it accepts '@' (e.g. "user@host"),
# '+' (e.g. "work+github"), non-ASCII letters (e.g. "clé"), and a leading
# '.' (e.g. ".hidden") -- none of those can break the ``IdentityFile``
# line: the key name is joined onto ``ssh_dir`` as a bare filename, and
# every character that matters for that (path separator, control chars,
# OpenSSH's own '%'/quote metacharacters) is rejected explicitly.
#
# Containment: a name containing no '/' and no '..' substring always
# resolves to a direct, contained child of whatever directory it is joined
# onto -- ``Path(name).name == name`` for any string without a separator,
# and a name containing '..' anywhere is rejected outright below. This
# holds regardless of what Unicode letters or the other allowed
# punctuation the name also contains.
#
#   - '#' is NOT a config-line hazard for an IdentityFile value: OpenSSH's
#     '#' only starts a comment at the START of a token, and IdentityFile is
#     always an absolute path here -- verified via `ssh -G -F <config>
#     <target>` resolving "IdentityFile /.../hash#1" unchanged. It is
#     therefore accepted (not in the banned set below).
#   - '${' IS a genuine hazard: ssh_config(5) documents that IdentityFile
#     (among other keywords) supports environment-variable expansion via
#     "${VAR}" -- the same class of runtime substitution as OpenSSH's '%'
#     tokens. Banned as a substring (not a full charset ban on '$'/'{'/'}'
#     individually, since only the "${" opening sequence starts expansion).
#   - '\' is NOT a config-line hazard for an IdentityFile value: OpenSSH
#     keeps a single backslash verbatim in an unquoted value -- verified via
#     `ssh -G -F <config> <target>` resolving "IdentityFile /.../a\b"
#     unchanged -- and ssh-keygen receives the key path as its own argv
#     element with no shell involved. It is therefore accepted.
_KEY_NAME_BANNED_CHARS = frozenset("%'\"/;")
_KEY_NAME_BANNED_SUBSTRINGS = ("${", "..")

# A key name is a bare filename joined onto the ssh directory, so it must
# not be any of the filenames OpenSSH itself reads or writes there. None of
# these eight names ends in ".pub", so the plain membership check below
# also covers the public-key twin (ssh_dir/<name>.pub).
RESERVED_SSH_FILE_NAMES = frozenset(
    {
        "config",
        "authorized_keys",
        "authorized_keys2",
        "known_hosts",
        "known_hosts.old",
        "known_hosts2",
        "environment",
        "rc",
    }
)


def is_valid_key_name(value: object) -> bool:
    """True iff ``value`` is a safe SSH key name (used as a bare filename
    under ``ssh_dir`` and as the ``IdentityFile`` line's trailing component).

    Rejects whitespace, control characters (incl. NUL/newline/CR), ``%``
    (OpenSSH token expansion), quotes, ``/`` (path separator),
    ``;``, ``${`` (OpenSSH environment-variable expansion), any ``..``
    substring (path traversal), a leading ``-`` (could be mistaken for a
    CLI flag), and a name matching ``RESERVED_SSH_FILE_NAMES`` (key names
    never coincide with OpenSSH-managed files in the ssh directory).
    Everything else -- including ``@``, ``+``, ``#``, ``\\``, non-ASCII letters,
    and a leading ``.`` -- is accepted; none of it can break out of the
    ``IdentityFile`` config line or escape ``ssh_dir`` (see module comment
    above).
    """
    if not isinstance(value, str) or not value:
        return False
    if len(value) > _KEY_NAME_MAX_LENGTH:
        return False

    # Explicit control-character rejection FIRST, independent of the
    # per-character checks below -- an independent, belt-and-suspenders
    # check (incl. NUL, newline, CR, DEL, and the C1 range).
    if _has_disallowed_control_character(value):
        return False

    if any(ch.isspace() for ch in value):
        return False

    if any(ch in _KEY_NAME_BANNED_CHARS for ch in value):
        return False

    if any(substring in value for substring in _KEY_NAME_BANNED_SUBSTRINGS):
        return False

    if value[0] == "-":
        return False

    if value in RESERVED_SSH_FILE_NAMES:
        return False

    return True


# ---------------------------------------------------------------------------
# Format-time backstop helper for key_path (IdentityFile line).
# ---------------------------------------------------------------------------


def has_control_characters(value: object) -> bool:
    """True iff ``value`` contains a C0 control character (incl. newline/CR) or DEL.

    Used as the format-time backstop for ``entry.key_path``: unlike a
    hostname or key name, a filesystem path legitimately contains ``/``, so
    the strict key-name grammar does not apply to it directly -- but a
    path can never legitimately contain a newline, and that is the actual
    mechanism that lets a value break out of the ``IdentityFile`` config
    line.
    """
    if not isinstance(value, str):
        return True
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)
