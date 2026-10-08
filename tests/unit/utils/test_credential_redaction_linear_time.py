"""Credential redaction runs in linear time on adversarial text.

Invariant: every redaction entry point finishes a ~1 MB adversarial input in
well under a second (no regex backtracking or rescanning that grows
quadratically with the input), and the supplied-secret scan still masks the
raw and percent-encoded secret inside a token too long to decode.
"""

from __future__ import annotations

import time
from typing import Callable, List

import pytest

from code_indexer.server.utils.access_log_redaction import (
    redact_sensitive_query_values,
)
from code_indexer.utils.credential_redaction import (
    mask_url_credentials,
    redact_command_output,
    redact_secret_fields,
)

_SIZE = 1_000_000
_LIMIT_SECONDS = 1.0
_SUPPLIED = "Example%Secret"
_ARGS = ["git", "clone", "https://example.com/r.git"]


def _repeat(unit: str) -> str:
    return unit * (_SIZE // len(unit))


_ADVERSARIAL = {
    "percent": _repeat("%"),
    "equals": _repeat("="),
    "assignments": _repeat("a="),
    "double_quotes": _repeat('"'),
    "single_quotes": _repeat("'"),
    "assignment_quotes": 'a="' + _repeat("a="),
    "jwt_dots": _repeat("eyJ."),
    "jwt_run": _repeat("eyJa"),
    "letters": _repeat("a"),
    "auth_headers": _repeat("authorization:"),
    "flags": _repeat("-a "),
    "query": "?" + _repeat("&a"),
    "unterminated_key": "-----BEGIN PRIVATE KEY-----" + _repeat("A"),
    "key_label": "-----BEGIN " + _repeat("A"),
}

_ENTRY_POINTS: List[Callable[[str], object]] = [
    redact_secret_fields,
    mask_url_credentials,
    redact_sensitive_query_values,
    lambda text: redact_command_output(text, _ARGS, [_SUPPLIED]),
]


@pytest.mark.parametrize("name", sorted(_ADVERSARIAL))
@pytest.mark.parametrize("entry", range(len(_ENTRY_POINTS)))
def test_adversarial_input_redacts_in_linear_time(name: str, entry: int) -> None:
    text = _ADVERSARIAL[name]

    started = time.perf_counter()
    _ENTRY_POINTS[entry](text)
    elapsed = time.perf_counter() - started

    assert elapsed < _LIMIT_SECONDS, f"{name}: {elapsed:.2f}s"


@pytest.mark.parametrize(
    "secret, form",
    [
        (_SUPPLIED, _SUPPLIED),
        (_SUPPLIED, "Example%25Secret"),
        (_SUPPLIED, "Example%2525Secret"),
        ("Example/Secret", "Example/Secret"),
        ("Example/Secret", "Example%2FSecret"),
        ("Example/Secret", "Example%2fSecret"),
        ("Example/Secret", "Example%252FSecret"),
    ],
)
def test_long_tokens_still_mask_supplied_secret(secret: str, form: str) -> None:
    padding = "x" * 10_000
    text = f"failed: {padding}{form}{padding} end"

    redacted = redact_command_output(text, _ARGS, [secret])

    assert form not in redacted
    assert redacted.startswith("failed: ")
    assert redacted.endswith(" end")
