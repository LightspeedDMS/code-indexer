"""Credential redaction runs in linear time on adversarial text.

Invariant: every redaction entry point finishes a ~1 MB adversarial input in
well under a second (no regex backtracking or rescanning that grows
quadratically with the input), and the supplied-secret scan decodes every
token whatever its length, so any encoded echo of the secret is masked.
"""

from __future__ import annotations

import random
import time
from typing import Callable, List
from urllib.parse import unquote, unquote_plus

import pytest

from code_indexer.server.utils.access_log_redaction import (
    redact_sensitive_query_values,
)
from code_indexer.utils.credential_redaction import (
    _percent_decode,
    _percent_decode_plus,
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


@pytest.mark.parametrize("length", [4_095, 4_096, 4_097, _SIZE])
def test_supplied_secret_masked_in_partly_encoded_token_of_any_length(
    length: int,
) -> None:
    form = "Example%2f%53ecret"
    token = "x" * (length - len(form)) + form
    text = f"failed: {token} end"

    started = time.perf_counter()
    redacted = redact_command_output(text, _ARGS, ["Example/Secret"])
    elapsed = time.perf_counter() - started

    assert "ecret" not in redacted
    assert redacted.startswith("failed: ")
    assert redacted.endswith(" end")
    assert elapsed < _LIMIT_SECONDS, f"{length}: {elapsed:.2f}s"


def test_percent_decoders_match_stdlib_unquote() -> None:
    crafted = [
        "",
        "%",
        "%%",
        "%4",
        "%4g",
        "%2f%53ecret",
        "%2F%2f",
        "%C3%A9",
        "%c3%a9x",
        "%C3x%A9",
        "%E2%82",
        "%E2%82%",
        "%FF%FE",
        "é%41ü",
        "%C3é",
        "a+b%2B%2b+",
        "%%41%2541",
    ]
    rng = random.Random(1234)
    alphabet = ["%", "2", "f", "F", "C", "3", "a", "9", "+", "x", "é"]
    fuzzed = [
        "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
        for _ in range(2_000)
    ]

    for text in crafted + fuzzed:
        assert _percent_decode(text) == unquote(text), repr(text)
        assert _percent_decode_plus(text) == unquote_plus(text), repr(text)
