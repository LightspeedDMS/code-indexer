"""Credential redaction for anything that leaves the process (Bug #2012).

Stdlib-only and layer-neutral: used by CLI-side utilities
(``utils/subprocess_diagnostics.py``, ``global_repos/git_pull_updater.py``)
and by the server (re-exported from ``server/logging_utils.py``), so no
CLI-path module has to import server code to redact.

Never applied to what a subprocess actually receives -- only to argv and
captured output that are logged, raised or returned.
"""

import base64
import functools
import json
import re
from typing import Any, Dict, FrozenSet, Iterator, List, Sequence, Set, Tuple
from urllib.parse import quote, quote_plus, unquote, unquote_plus

_MASK = "***"

# Userinfo (``user:pass@`` / ``oauth2:TOKEN@`` / ``TOKEN@``) between a URL
# scheme separator and the host: everything up to the LAST '@' before the
# host, never crossing '/', '?', '#' or whitespace (so a password containing
# '@' is masked whole, and an '@' in a path, query or fragment is not
# userinfo).
_URL_USERINFO_RE = re.compile(r"(://)([^/?#\s]+)@")

# ``Authorization: <scheme> <credentials>`` in free text or a header value;
# group 3 is the credential itself. The scheme is bounded (real schemes are
# short) so a failed attempt never rescans an unbounded run: linear time.
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization\s*[:=]\s*)(\S{1,64})\s+(\S+)")

# Option / env-style names whose VALUE is a credential (``--token X``,
# ``--password=X``, ``GIT_ASKPASS_TOKEN=X``, ``http.extraHeader=...``).
# ``credential.helper`` names a helper program, not a secret.
_SECRET_NAME_RE = re.compile(
    r"(?i)(token|passw|secret|api[-_]?key|auth|credential(?!\.helper)|header)"
)

# Echoed secrets shorter than this are not hunted in output text: masking
# e.g. a 3-char value would garble unrelated words.
_MIN_OUTPUT_SECRET_CHARS = 6

# Display masking of a STORED secret (tokens, API keys): reveal at most its
# last _DISPLAY_TAIL_CHARS characters, and only when the secret is at least
# _DISPLAY_TAIL_MIN_CHARS long (a shorter one would leave too little hidden).
DISPLAY_MASK_CHAR = "•"
_DISPLAY_MASK = DISPLAY_MASK_CHAR * 8
_DISPLAY_TAIL_CHARS = 4
_DISPLAY_TAIL_MIN_CHARS = 20
_DISPLAY_SHORT_SECRET = "configured"


def stored_secret_tail(value: Any) -> str:
    """The most of a stored secret that may ever be shown: its last 4
    characters when it is 20+ characters long, otherwise ``""``."""
    secret = str(value) if value else ""
    if len(secret) < _DISPLAY_TAIL_MIN_CHARS:
        return ""
    return secret[-_DISPLAY_TAIL_CHARS:]


def mask_stored_secret(value: Any) -> str:
    """Display form of a stored secret: ``"••••••••abcd"`` (last 4 only) for
    a secret of 20+ characters, ``"configured"`` for a shorter one, and
    ``""`` when nothing is stored. Never reveals a leading character (the
    leading characters identify the provider and part of the secret)."""
    if not value:
        return ""
    tail = stored_secret_tail(value)
    return _DISPLAY_MASK + tail if tail else _DISPLAY_SHORT_SECRET


def is_display_mask(value: Any) -> bool:
    """True only for an EXACT display form produced by mask_stored_secret
    (``"configured"`` or the 8-character mask plus a 4-character tail), so a
    setter can treat a re-submitted display value as "keep the stored
    secret" without mistaking a real key that merely contains ``*``/``•``."""
    if not isinstance(value, str):
        return False
    if value == _DISPLAY_SHORT_SECRET:
        return True
    return len(value) == len(_DISPLAY_MASK) + _DISPLAY_TAIL_CHARS and value.startswith(
        _DISPLAY_MASK
    )


# A query parameter: its name (possibly percent-encoded) and its value. A
# query is split only on '&', and a URL or request target ends at
# whitespace, so the value runs to the next '&' or whitespace -- quotes,
# ';' and ',' are part of it.
_QUERY_PARAM_RE = re.compile(
    r"(?P<prefix>(?:^|[?&])(?P<name>[\w.%~+\-]+)=)(?P<value>[^&\s]*)"
)

# A ``scheme://`` URL in text, to its end at whitespace: the span whose
# query parameters mask_url_credentials masks. A scheme starts only at the
# start of its character run, so no run is rescanned: linear time.
_SCHEME_URL_RE = re.compile(r"(?<![\w+.\-])[A-Za-z][\w+.\-]*://\S*")


def mask_secret_query_values(
    text: str, mask: str = _MASK, also_names: FrozenSet[str] = frozenset()
) -> str:
    """Replace the value of every query parameter in `text` whose DECODED
    name is secret by is_secret_field (a query value is always a string, so
    no flag-like name is exempt), or whose decoded lowercase name is in
    `also_names`. A percent-encoded name (``pass%77ord``) is recognised too.
    Only the value is replaced; the rest of `text` is kept."""

    def mask_param(match: "re.Match[str]") -> str:
        name = unquote(match.group("name"))
        if name.lower() in also_names or is_secret_field(name):
            return match.group("prefix") + mask
        return match.group(0)

    return _QUERY_PARAM_RE.sub(mask_param, text)


def mask_url_credentials(url: Any) -> Any:
    """Strip embedded credentials from a git/HTTP URL for safe exposure.

    The one rule, shared by log redaction and every API, MCP and Web response
    that returns a repository URL: the WHOLE userinfo of a ``scheme://``
    URL is replaced by ``***``, whatever the scheme and whether or not it has
    a ``:password`` part, so neither a secret nor the presence of a username
    is revealed:

    * ``https://user:TOKEN@host/org/repo.git`` -> ``https://***@host/org/repo.git``
    * ``https://TOKEN@host:8443/repo.git`` -> ``https://***@host:8443/repo.git``
    * ``ssh://git@host/org/repo.git`` -> ``ssh://***@host/org/repo.git``

    The value of every secret-named query parameter is replaced by ``***``
    too (mask_secret_query_values):
    ``https://host/repo.git?access_token=X`` ->
    ``https://host/repo.git?access_token=***``.

    Scheme, host, port, path and other query parameters are unchanged.
    scp-style addresses (``git@host:org/repo.git``) have no ``://`` and keep
    their userinfo, as do local paths, ``file:///...`` and scheme-only forms
    such as ``local://alias``. Non-string input (``None``) and ``""`` pass
    through. Idempotent: masking an already-masked URL is a no-op.
    """
    if not isinstance(url, str):
        return url
    masked = _URL_USERINFO_RE.sub(r"\1***@", url)
    return _SCHEME_URL_RE.sub(lambda m: mask_secret_query_values(m.group()), masked)


def with_masked_repo_url(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow copy of a repository record whose ``repo_url`` (when present)
    is masked by mask_url_credentials, for returning in a response. The
    record passed in is left unchanged."""
    masked = dict(entry)
    if "repo_url" in masked:
        masked["repo_url"] = mask_url_credentials(masked["repo_url"])
    return masked


# ``Cookie:`` / ``Set-Cookie:`` header: the whole value to end of line.
_COOKIE_HEADER_RE = re.compile(r"(?im)\b((?:set-)?cookie\s*:\s*)[^\r\n]*")

# ``name=value`` / ``name: value`` in free text -- query strings, env
# assignments, headers, JSON-ish and repr fragments. The name may be quoted.
# A value that opens with a quote ends at its matching closing quote; any
# other value runs to the next whitespace, ';' or '&' -- quotes, commas and
# brackets inside it are part of it, so it is masked whole. Only names that
# is_secret_field() classifies have their value masked. The value is matched
# only after the name is classified secret, so a non-secret name never
# consumes the rest of the line and no text is rescanned: linear time.
_ASSIGNMENT_NAME_RE = re.compile(
    r"""(?<![\w.\-])(?P<kq>["']?)(?P<name>[A-Za-z_][\w.\-]*)(?P=kq)\s*[=:]\s*"""
)
_ASSIGNMENT_VALUE_RE = re.compile(r""""[^"\n]*"|'[^'\n]*'|[^\s&;]+""")

# Command-line flags: ``--name=value`` and ``--name value`` (a value never
# starts with '-'). ``--no-*`` flags are boolean and take no value.
_FLAG_RE = re.compile(
    r"(?<![\w-])(?P<flag>--?(?P<name>[A-Za-z][\w.\-]*))(?P<sep>=|\s+)"
    r"(?P<value>[^\s\-]\S*)"
)


def _mask_flag(match: "re.Match[str]") -> str:
    name = match.group("name")
    if name.lower().startswith(("no-", "no_")) or not is_secret_field(name):
        return match.group(0)
    return f"{match.group('flag')}{match.group('sep')}{_MASK}"


def _mask_assignments(text: str) -> str:
    """Mask the value of every secret-named assignment. A non-secret name's
    value is rescanned (``failed: GIT_TOKEN=x`` still masks ``x``); the
    scan position strictly increases, so the loop ends."""
    parts: List[str] = []
    pos = 0
    flushed = 0  # text[:flushed] is already in parts
    while True:
        match = _ASSIGNMENT_NAME_RE.search(text, pos)
        if match is None:
            break
        found = None
        if _is_secret_name(match.group("name")):
            found = _ASSIGNMENT_VALUE_RE.match(text, match.end())
        if found is None:
            pos = match.end("name")
            continue
        value = found.group()
        closed = len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]
        quote = value[0] if closed else ""
        parts.append(text[flushed : found.start()])
        parts.append(f"{quote}{_MASK}{quote}")
        pos = flushed = found.end()
    parts.append(text[flushed:])
    return "".join(parts)


# A PEM private-key block of any label (``PRIVATE KEY``, ``RSA PRIVATE
# KEY``, ...): its body is masked through the matching END line, or to the
# end of the text when the block is truncated (fail closed).
# The label is bounded, so an attempt never rescans an unbounded run.
_PRIVATE_KEY_BLOCK_RE = re.compile(
    r"(-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----)"
    r"(?:.*?(-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----)|.*)",
    re.DOTALL,
)

# A JWT-shaped token: ``eyJ`` (base64url of ``{"``) header, payload and a
# possibly empty signature, each base64url. It starts only at the start of a
# base64url run, so no run is rescanned: linear time.
_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_\-])eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*"
)


def _mask_private_key_block(match: "re.Match[str]") -> str:
    return f"{match.group(1)}{_MASK}{match.group(2) or ''}"


def _mask_text(text: str) -> str:
    text = _PRIVATE_KEY_BLOCK_RE.sub(_mask_private_key_block, text)
    text = _JWT_RE.sub(_MASK, text)
    text = _AUTH_HEADER_RE.sub(r"\1***", mask_url_credentials(text))
    text = _COOKIE_HEADER_RE.sub(rf"\1{_MASK}", text)
    text = _FLAG_RE.sub(_mask_flag, text)
    return _mask_assignments(text)


def _userinfo_secrets(arg: str) -> List[str]:
    """The secret parts of every URL userinfo in `arg`: the password alone
    (it may be echoed on its own) and the whole userinfo; a token-only
    userinfo (no ':') is wholly secret. Each in its percent-encoded AND
    decoded form, since tools may echo either."""
    secrets: List[str] = []
    for _sep, userinfo in _URL_USERINFO_RE.findall(arg):
        _user, colon, password = userinfo.partition(":")
        for part in [password, userinfo] if colon else [userinfo]:
            secrets.extend({part, unquote(part)})
    return secrets


def _header_secrets(text: str) -> List[str]:
    """The credential of every ``Authorization: <scheme> <credential>`` in
    `text` (a tool may echo the bare token)."""
    return [match.group(3) for match in _AUTH_HEADER_RE.finditer(text)]


def _takes_secret_value(arg: str) -> bool:
    """`--token X` style: the NEXT argument is a secret. `--no-auth` style
    boolean flags take no value."""
    return (
        arg.startswith("-")
        and not arg.startswith("--no-")
        and _SECRET_NAME_RE.search(arg) is not None
    )


def _walk_argv(args: Any) -> Iterator[Tuple[str, List[str]]]:
    """The one argv classifier: yields (safe display, secret values) for
    each argument. Shared by redact_command and redact_command_output."""
    items = [args] if isinstance(args, str) else list(args or ())
    value_is_secret = False
    for arg in (str(a) for a in items):
        if value_is_secret:
            value_is_secret = False
            yield _MASK, [arg, *_header_secrets(arg), *_userinfo_secrets(arg)]
            continue
        name, sep, value = arg.partition("=")
        if sep and _SECRET_NAME_RE.search(name):
            secrets = [value, *_header_secrets(value), *_userinfo_secrets(value)]
            yield f"{name}={_MASK}", secrets
            continue
        value_is_secret = _takes_secret_value(arg)
        yield _mask_text(arg), [*_header_secrets(arg), *_userinfo_secrets(arg)]


def redact_command(args: Any) -> Any:
    """Safe-to-log copy of a subprocess argv (a list for a sequence, a str
    for a str command). Credential-free arguments are unchanged, so a
    normal command keeps its shape."""
    if isinstance(args, str):
        return _mask_text(args)
    return [display for display, _secrets in _walk_argv(args)]


# Characters an encoded echo of a secret is made of: URL-unreserved, '%'
# escapes and '+' (a form-encoded space). A token is a maximal run of these
# plus the secret's own characters, so a partly encoded echo is one token.
_ENCODED_TOKEN_CHARS = r"A-Za-z0-9._~%+\-"
_PERCENT_DECODE_PASSES = 2


def _decoded_forms(token: str) -> Set[str]:
    """``token`` percent-decoded up to twice, each pass both with and
    without '+' read as a space (escapes in any letter case)."""
    forms = {token}
    for _ in range(_PERCENT_DECODE_PASSES):
        forms |= {decode(form) for form in forms for decode in (unquote, unquote_plus)}
    return forms


# A token longer than this (far beyond any credential) is not decoded; the
# raw and percent-encoded forms of a supplied secret are masked in it by
# plain substring search instead, so the scan stays linear.
_MAX_DECODED_TOKEN_CHARS = 4096
_PERCENT_ESCAPE_RE = re.compile(r"%[0-9A-F]{2}")


def _encoded_forms(secret: str) -> List[str]:
    """``secret`` raw and percent-encoded (``%XX`` and ``+``-for-space,
    once and twice, upper and lower-case escapes), longest first."""
    forms = {secret}
    for encode in (quote, quote_plus):
        once = encode(secret, safe="")
        forms |= {once, quote(once, safe="")}
    forms |= {_PERCENT_ESCAPE_RE.sub(lambda m: m.group().lower(), f) for f in forms}
    return sorted(forms, key=len, reverse=True)


def _base64_forms(value: str) -> List[str]:
    """Standard and URL-safe base64 of ``value``, padded and unpadded,
    longest first."""
    forms = set()
    for encode in (base64.b64encode, base64.urlsafe_b64encode):
        encoded = encode(value.encode("utf-8")).decode("ascii")
        forms |= {encoded, encoded.rstrip("=")}
    return sorted(forms, key=len, reverse=True)


def _basic_auth_values(args: Any, secret: str) -> List[str]:
    """What a Basic credential for ``secret`` encodes: the secret itself,
    and ``user:secret`` for every argv URL userinfo whose password it is."""
    values = [secret]
    for arg in [args] if isinstance(args, str) else list(args or ()):
        for _sep, userinfo in _URL_USERINFO_RE.findall(str(arg)):
            user, colon, password = userinfo.partition(":")
            if colon and unquote(password) == secret:
                values.append(f"{unquote(user)}:{secret}")
    return values


def _mask_supplied_secret(text: str, args: Any, secret: str) -> str:
    """Mask ``secret`` in ``text``: every whole token whose decoded forms
    (``_decoded_forms``) contain it, then every remaining raw occurrence,
    then the base64 of each ``_basic_auth_values`` entry."""
    extra = "".join(re.escape(char) for char in sorted(set(secret)))
    token_re = re.compile(f"[{_ENCODED_TOKEN_CHARS}{extra}]+")
    encoded = _encoded_forms(secret)

    def mask_token(match: "re.Match[str]") -> str:
        token = match.group()
        # Decoding never lengthens a token, so a shorter one cannot hold it.
        if len(token) < len(secret):
            return token
        if len(token) > _MAX_DECODED_TOKEN_CHARS:
            for form in encoded:
                token = token.replace(form, _MASK)
            return token
        return _MASK if any(secret in form for form in _decoded_forms(token)) else token

    text = token_re.sub(mask_token, text)
    text = text.replace(secret, _MASK)
    for value in _basic_auth_values(args, secret):
        for form in _base64_forms(value):
            text = text.replace(form, _MASK)
    return text


def redact_command_output(
    text: Any, args: Any, supplied_secrets: Sequence[str] = ()
) -> Any:
    """Safe-to-log copy of a subprocess's captured output. Every value in
    ``supplied_secrets`` -- secrets the caller supplied, so known exactly --
    is masked at EVERY occurrence, whatever its length or neighbouring
    characters: raw, inside any token that percent-decodes to it
    (partial, double, '+'-as-space, any escape case), and as the base64 of
    it or of ``user:secret`` (``_mask_supplied_secret``). Then URL userinfo
    and Authorization values are masked, and every credential value the
    argv carried (at least _MIN_OUTPUT_SECRET_CHARS long) is masked wherever
    the output echoes it as a whole word. Bytes (subprocess.run's
    TimeoutExpired keeps raw bytes even in text mode) are redacted and
    returned as bytes; any other non-str input is returned unchanged."""
    if isinstance(text, bytes):
        decoded = text.decode("utf-8", errors="surrogateescape")
        redacted = redact_command_output(decoded, args, supplied_secrets)
        return redacted.encode("utf-8", errors="surrogateescape")
    if not isinstance(text, str):
        return text
    for secret in sorted(filter(None, set(supplied_secrets)), key=len, reverse=True):
        text = _mask_supplied_secret(text, args, secret)
    text = _mask_text(text)
    for _display, secrets in _walk_argv(args):
        for secret in secrets:
            if len(secret) >= _MIN_OUTPUT_SECRET_CHARS:
                text = re.sub(rf"(?<![\w-]){re.escape(secret)}(?![\w-])", _MASK, text)
    return text


# The ONE credential-name rule, for structured keys and for free-text
# ``name=value`` / ``Name: value`` / ``--name value`` names alike. It FAILS
# CLOSED: any name containing a credential word is secret (``password_hash``,
# ``access_tokens``, ``apiKeys``, ``recoveryCode``), and so is any name with
# a short form as a WHOLE segment (``pass_hash``, ``totp_code``,
# ``GITHUB_PAT``) -- whole segments, so ``passed``, ``compass``, ``pinned``,
# ``footprint`` and ``path`` are not.
_SECRET_KEY_SUBSTRING_RE = re.compile(
    r"(?i)(token|secret|passw|passphrase|pwd|api[-_]?key|apikey"
    r"|private[-_]?key|access[-_]?key|authorization|cookie"
    r"|credential(?!\.helper)|recovery[-_]?codes?|one[-_]?time)"
)
_SECRET_KEY_SEGMENTS = frozenset(
    {"pw", "pwd", "pass", "auth", "pin", "otp", "totp", "mfa", "2fa", "pat", "session"}
)
# Names that only identify, type, count or bound a secret hold none
# (``credential_id``, ``token_type``, ``secret_count``, ``total_tokens``).
# Flag-like names (``mfa_enabled``, ``token_scope``, ``api_key_flag``) are NOT
# exempt by name: they stay secret, and only a real flag value (bool, int,
# None -- see _holds_secret) under them is kept visible.
_NON_SECRET_KEY_RE = re.compile(r"(?i)(?:_ids?|_type|_count)$|^(?:total|max)_")
_CAMEL_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")
_KEY_SEGMENT_SPLIT_RE = re.compile(r"[^a-z0-9]+")
REDACTED_FIELD = "***REDACTED***"
# Bounds recursion through nested containers and JSON-in-JSON strings;
# anything deeper is replaced whole (fail closed).
_MAX_REDACTION_DEPTH = 64


def _key_segments(name: str) -> List[str]:
    """Lowercase segments of a name, splitting separators and camelCase."""
    snake = _CAMEL_BOUNDARY_RE.sub(r"\1_\2", name).lower()
    return [s for s in _KEY_SEGMENT_SPLIT_RE.split(snake) if s]


def is_secret_field(name: Any) -> bool:
    """True when a key or assignment name may hold a credential. Fails
    closed: over-redaction is acceptable, under-redaction is not. A bytes
    name (a bytes dictionary key) is checked by its decoded text."""
    if isinstance(name, (bytes, bytearray)):
        name = bytes(name).decode("utf-8", errors="replace")
    if not isinstance(name, str):
        return False
    return _is_secret_name(name)


@functools.lru_cache(maxsize=4096)
def _is_secret_name(name: str) -> bool:
    """is_secret_field for a text name. Cached: text repeating the same
    names (``a=a=a=...``) classifies each distinct name once."""
    if _NON_SECRET_KEY_RE.search(name):
        return False
    if _SECRET_KEY_SUBSTRING_RE.search(name):
        return True
    return any(s in _SECRET_KEY_SEGMENTS for s in _key_segments(name))


def _holds_secret(value: Any) -> bool:
    """Numbers, booleans and None under a secret-named key are counts or
    flags, never credentials."""
    return not (value is None or isinstance(value, (bool, int, float)))


def redact_secret_fields(data: Any) -> Any:
    """Safe-to-export copy of structured data, for anything that leaves the
    process (tracing spans, logs).

    At any depth of dicts, lists and tuples, the value of every secret-named
    key (see is_secret_field) is replaced by ``REDACTED_FIELD`` when it is a
    string or a container. A string holding a JSON object or array (how MCP
    responses carry their payload) is redacted inside and re-serialized; any
    other string (and any bytes value, decoded to text) has URL userinfo,
    Authorization and Cookie values and ``name=value`` credential
    assignments masked. The input is never modified.
    """
    return _redact_value(data, 0)


_BARE_NAME_RE = re.compile(r"[A-Za-z][\w.\-]*")


def _is_header_pair(data: Any) -> bool:
    """A (name, value) pair whose bare name says the value is secret."""
    return (
        len(data) == 2
        and isinstance(data[0], str)
        and _BARE_NAME_RE.fullmatch(data[0]) is not None
        and is_secret_field(data[0])
        and _holds_secret(data[1])
    )


def _redact_value(data: Any, depth: int) -> Any:
    if depth > _MAX_REDACTION_DEPTH:
        return REDACTED_FIELD
    if isinstance(data, dict):
        return {
            key: (
                REDACTED_FIELD
                if is_secret_field(key) and _holds_secret(value)
                else _redact_value(value, depth + 1)
            )
            for key, value in data.items()
        }
    if isinstance(data, (list, tuple)):
        items = [_redact_value(item, depth + 1) for item in data]
        if _is_header_pair(data):
            items[1] = REDACTED_FIELD
        return items if isinstance(data, list) else tuple(items)
    if isinstance(data, (set, frozenset)):
        return type(data)(_redact_value(item, depth + 1) for item in data)
    if isinstance(data, str):
        return _redact_text(data, depth)
    if isinstance(data, (bytes, bytearray)):
        # Raw bytes never leave unredacted: decode, mask, export as text.
        return _redact_text(bytes(data).decode("utf-8", errors="replace"), depth)
    if not _holds_secret(data):
        return data
    # Any other object (an exception, a model) is exported by its text, so
    # its text is masked; it is kept as-is only when that text holds none.
    text = str(data)
    masked = _mask_text(text)
    return data if masked == text else masked


def _redact_text(text: str, depth: int) -> str:
    if text.lstrip()[:1] in ("{", "["):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            redacted = _redact_value(parsed, depth + 1)
            return text if redacted == parsed else json.dumps(redacted)
    return _mask_text(text)
