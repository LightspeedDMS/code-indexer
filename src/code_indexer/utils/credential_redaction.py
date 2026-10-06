"""Credential redaction for anything that leaves the process (Bug #2012).

Stdlib-only and layer-neutral: used by CLI-side utilities
(``utils/subprocess_diagnostics.py``, ``global_repos/git_pull_updater.py``)
and by the server (re-exported from ``server/logging_utils.py``), so no
CLI-path module has to import server code to redact.

Never applied to what a subprocess actually receives -- only to argv and
captured output that are logged, raised or returned.
"""

import re
from typing import Any, Iterator, List, Tuple
from urllib.parse import unquote

_MASK = "***"

# Userinfo (``user:pass@`` / ``oauth2:TOKEN@`` / ``TOKEN@``) between a URL
# scheme separator and the host.
_URL_USERINFO_RE = re.compile(r"(://)([^/@\s]+)@")

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

    ``https://oauth2:TOKEN@git.example.com/org/repo.git`` ->
    ``https://***@git.example.com/org/repo.git``. Non-string input, credential-free
    URLs, and scheme-only forms (e.g. ``local://alias``) are returned unchanged.
    Idempotent: masking an already-masked URL is a no-op.
    """
    if not isinstance(url, str):
        return url
    return _URL_USERINFO_RE.sub(r"\1***@", url)


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


def redact_command_output(text: Any, args: Any) -> Any:
    """Safe-to-log copy of a subprocess's captured output: URL userinfo and
    Authorization values are masked, and every credential value the argv
    carried (at least _MIN_OUTPUT_SECRET_CHARS long) is masked wherever the
    output echoes it as a whole word. Bytes (subprocess.run's TimeoutExpired
    keeps raw bytes even in text mode) are redacted and returned as bytes;
    any other non-str input is returned unchanged."""
    if isinstance(text, bytes):
        decoded = text.decode("utf-8", errors="surrogateescape")
        redacted = redact_command_output(decoded, args)
        return redacted.encode("utf-8", errors="surrogateescape")
    if not isinstance(text, str):
        return text
    text = _mask_text(text)
    for _display, secrets in _walk_argv(args):
        for secret in secrets:
            if len(secret) >= _MIN_OUTPUT_SECRET_CHARS:
                text = re.sub(rf"(?<![\w-]){re.escape(secret)}(?![\w-])", _MASK, text)
    return text
