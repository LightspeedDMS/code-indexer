"""Credential redaction for anything that leaves the process (Bug #2012).

Stdlib-only and layer-neutral: used by CLI-side utilities
(``utils/subprocess_diagnostics.py``, ``global_repos/git_pull_updater.py``)
and by the server (re-exported from ``server/logging_utils.py``), so no
CLI-path module has to import server code to redact.

Never applied to what a subprocess actually receives -- only to argv and
captured output that are logged, raised or returned.
"""

import base64
import re
from typing import Any, Dict, Iterator, List, Sequence, Set, Tuple
from urllib.parse import unquote, unquote_plus

_MASK = "***"

# Userinfo (``user:pass@`` / ``oauth2:TOKEN@`` / ``TOKEN@``) between a URL
# scheme separator and the host: everything up to the LAST '@' before the
# host, never crossing '/', '?', '#' or whitespace (so a password containing
# '@' is masked whole, and an '@' in a path, query or fragment is not
# userinfo).
_URL_USERINFO_RE = re.compile(r"(://)([^/?#\s]+)@")

# ``Authorization: <scheme> <credentials>`` in free text or a header value;
# group 3 is the credential itself.
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization:\s*)(\S+)\s+(\S+)")

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

    Scheme, host, port and path are unchanged. scp-style addresses
    (``git@host:org/repo.git``) have no ``://`` and are returned unchanged,
    as are local paths, ``file:///...`` and scheme-only forms such as
    ``local://alias``. Non-string input (``None``) and ``""`` pass through.
    Idempotent: masking an already-masked URL is a no-op.
    """
    if not isinstance(url, str):
        return url
    return _URL_USERINFO_RE.sub(r"\1***@", url)


def with_masked_repo_url(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow copy of a repository record whose ``repo_url`` (when present)
    is masked by mask_url_credentials, for returning in a response. The
    record passed in is left unchanged."""
    masked = dict(entry)
    if "repo_url" in masked:
        masked["repo_url"] = mask_url_credentials(masked["repo_url"])
    return masked


def _mask_text(text: str) -> str:
    return _AUTH_HEADER_RE.sub(r"\1***", mask_url_credentials(text))


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

    def mask_token(match: "re.Match[str]") -> str:
        token = match.group()
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
