"""redact_secret_fields: the one structured-data redaction for anything
that leaves the process (tracing spans, logs).

Invariants: every secret-named value is replaced at any depth; identifier
keys, numeric counts and non-secret values are kept; JSON payload strings
are redacted inside; credentials embedded in URLs or Authorization text are
masked; the input is never modified; excessive nesting fails closed.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from code_indexer.utils.credential_redaction import (
    REDACTED_FIELD,
    is_secret_field,
    redact_secret_fields,
)

_VALUE = "ExampleSecretValue"


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "Password",
        "new_password",
        "passwd",
        "token",
        "confirmation_token",
        "accessToken",
        "secret",
        "client_secret",
        "api_key",
        "apiKey",
        "X-Api-Key",
        "voyage_api_key",
        "private_key",
        "privateKey",
        "Authorization",
        "Cookie",
        "credential",
        "credentials",
    ],
)
def test_secret_named_keys_are_redacted(key: str) -> None:
    assert is_secret_field(key)
    assert redact_secret_fields({key: _VALUE}) == {key: REDACTED_FIELD}


@pytest.mark.parametrize(
    "key", ["credential_id", "key_id", "client_id", "username", "repository_alias"]
)
def test_identifier_and_ordinary_keys_are_kept(key: str) -> None:
    assert not is_secret_field(key)
    assert redact_secret_fields({key: "example"}) == {key: "example"}


@pytest.mark.parametrize(
    "key",
    [
        "otp_enabled",
        "mfa_enabled",
        "totp_enabled",
        "pat_enabled",
        "token_scope",
        "auth_scope",
        "oauth_scopes",
        "onetime_flag",
    ],
)
def test_flag_and_scope_names_keep_their_non_secret_values(key: str) -> None:
    """Names that switch or bound a secret hold none: their values (often
    strings, such as "true" or "read write") stay visible."""
    assert not is_secret_field(key)
    assert redact_secret_fields({key: "read write"}) == {key: "read write"}


@pytest.mark.parametrize(
    "key",
    [
        "otp",
        "totp_code",
        "mfa_code",
        "recovery_codes",
        "pat",
        "access_token",
        "password",
    ],
)
def test_one_time_and_access_secret_names_stay_secret(key: str) -> None:
    assert is_secret_field(key)
    assert redact_secret_fields({key: _VALUE}) == {key: REDACTED_FIELD}


def test_counts_and_flags_under_token_like_names_are_kept() -> None:
    data = {"token_count": 512, "max_tokens": 1.5, "has_token": True, "token": None}

    assert redact_secret_fields(data) == data


def test_nested_containers_are_redacted() -> None:
    data = {
        "providers": [{"name": "example", "api_key": _VALUE}],
        "pair": ("visible", {"secret": _VALUE}),
        "credentials": [{"token": _VALUE}],
    }

    redacted = redact_secret_fields(data)

    assert _VALUE not in json.dumps(redacted)
    assert redacted["providers"][0]["name"] == "example"
    assert redacted["pair"][0] == "visible"
    assert isinstance(redacted["pair"], tuple)
    assert redacted["credentials"] == REDACTED_FIELD


def test_json_payload_strings_are_redacted_inside() -> None:
    payload = {"success": True, "client_id": "example", "client_secret": _VALUE}
    mcp = {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}]}

    redacted = redact_secret_fields(mcp)

    inner = json.loads(redacted["content"][0]["text"])
    assert inner == {
        "success": True,
        "client_id": "example",
        "client_secret": REDACTED_FIELD,
    }


def test_json_string_without_secrets_is_returned_unchanged() -> None:
    text = json.dumps({"results": ["a", "b"]}, indent=2)

    assert redact_secret_fields({"text": text}) == {"text": text}


def test_credentials_in_free_text_are_masked() -> None:
    data = {
        "error": f"failed for https://example-user:{_VALUE}@example.com/r.git",
        "detail": f"Authorization: Bearer {_VALUE}",
    }

    redacted = redact_secret_fields(data)

    assert _VALUE not in json.dumps(redacted)
    assert "example.com/r.git" in redacted["error"]


def test_input_is_not_modified() -> None:
    data: Any = {"password": _VALUE, "nested": [{"token": _VALUE}]}
    original = copy.deepcopy(data)

    redact_secret_fields(data)

    assert data == original


def test_excessive_nesting_fails_closed() -> None:
    data: Any = _VALUE
    for _ in range(200):
        data = [data]

    assert _VALUE not in json.dumps(redact_secret_fields(data))


def test_non_container_values_pass_through() -> None:
    assert redact_secret_fields(None) is None
    assert redact_secret_fields(42) == 42
    assert redact_secret_fields("plain text") == "plain text"


_FREE = "examplevalue123"


@pytest.mark.parametrize(
    "sample",
    [
        f"token={_FREE}",
        f"failed: GIT_TOKEN={_FREE} rejected",
        f"https://example.com/cb?code=1&token={_FREE}&x=2",
        f"Cookie: session={_FREE}; theme=dark",
        f"Set-Cookie: sid={_FREE}; Path=/; HttpOnly",
        f"pass={_FREE}",
        f"pw={_FREE}",
        f"pwd: {_FREE}",
        f"passwd={_FREE}",
        f"export DB_PASSWORD={_FREE}",
        f"secret: {_FREE}",
        f"api_key={_FREE}",
        f"apikey={_FREE}",
        f"X-Api-Key: {_FREE}",
        f"access_key={_FREE}",
        f"private_key={_FREE}",
        f"client_secret={_FREE}",
        f"auth={_FREE}",
        f"Authorization: {_FREE}",
        f'payload {{"token": "{_FREE}", "ok": true',
        f"{{'password': '{_FREE}', 'user': 'example'}}",
        f"clientSecret={_FREE}",
    ],
)
def test_free_text_credential_assignments_are_masked(sample: str) -> None:
    redacted = redact_secret_fields({"text": sample})["text"]

    assert _FREE not in redacted, redacted


@pytest.mark.parametrize(
    "sample",
    [
        "passed=5 tests",
        "tests passed: 12",
        "passthrough=enabled",
        "compass=north",
        "author=example-author",
        "token_count=512",
        "total_tokens=900",
        "max_tokens: 2048",
        "started at time: 12:30",
        "https://example.com/r.git?ref=main&depth=1",
        "bypass=false",
    ],
)
def test_common_false_positives_are_left_intact(sample: str) -> None:
    assert redact_secret_fields({"text": sample}) == {"text": sample}


@pytest.mark.parametrize("key", ["pw", "pass", "pwd", "passwd", "basic_auth"])
def test_short_secret_key_forms_are_secret(key: str) -> None:
    assert is_secret_field(key)


@pytest.mark.parametrize(
    "key",
    [
        "tokens",
        "access_tokens",
        "passwords",
        "password_hash",
        "apiKeys",
        "cookies",
        "secret_value",
        "token_confirm",
        "private_key_pem",
        "clientSecret",
        "pass_hash",
    ],
)
def test_plural_suffixed_and_camelcase_keys_are_secret(key: str) -> None:
    assert is_secret_field(key)
    assert redact_secret_fields({key: _VALUE}) == {key: REDACTED_FIELD}


@pytest.mark.parametrize(
    "key", ["passed", "passthrough", "compass", "bypass", "author"]
)
def test_words_containing_short_forms_are_not_secret(key: str) -> None:
    assert not is_secret_field(key)


def test_string_observability_fields_are_kept() -> None:
    data = {
        "token_type": "bearer",
        "credential_type": "pat",
        "total_tokens": "900",
        "max_tokens": "2048",
        "secret_count": "3",
    }

    assert redact_secret_fields(data) == data


def test_secret_assignment_inside_an_observability_value_is_masked() -> None:
    redacted = redact_secret_fields({"token_type": f"token={_FREE}"})

    assert _FREE not in redacted["token_type"]


def test_bytes_are_decoded_and_masked() -> None:
    redacted = redact_secret_fields({"blob": f"password={_FREE}".encode()})

    assert _FREE not in json.dumps(redacted)


@pytest.mark.parametrize(
    "key",
    [
        "totp_code",
        "recovery_code",
        "recovery_codes",
        "otp",
        "mfa_code",
        "passphrase",
        "pin",
        "2fa_code",
        "one_time_code",
        "totpCode",
        "recoveryCode",
        "GITHUB_PAT",
        "session",
    ],
)
def test_mfa_and_other_credential_keys_are_secret(key: str) -> None:
    assert is_secret_field(key)
    assert redact_secret_fields({key: _VALUE}) == {key: REDACTED_FIELD}


@pytest.mark.parametrize(
    "sample",
    [
        f"git fetch --token={_FREE} origin",
        f"login --password {_FREE} --verbose",
        f"Authorization=Bearer {_FREE}",
        f"GITHUB_PAT={_FREE}",
        f"session={_FREE}",
        f"token_value={_FREE}",
        f"totp_code={_FREE}",
        f"recovery_code: {_FREE}",
        f"passphrase={_FREE}",
    ],
)
def test_more_free_text_credential_forms_are_masked(sample: str) -> None:
    assert _FREE not in redact_secret_fields({"text": sample})["text"]


@pytest.mark.parametrize(
    "value",
    [
        [("Authorization", f"Bearer {_FREE}")],
        (("X-Api-Key", _FREE),),
        {f"token={_FREE}"},
        ValueError(f"login failed password={_FREE}"),
    ],
)
def test_header_pairs_sets_and_objects_are_masked(value: Any) -> None:
    redacted = redact_secret_fields({"payload": value})

    assert _FREE not in json.dumps(redacted, default=str)


@pytest.mark.parametrize(
    "sample",
    [
        "footprint=12",
        "pinned=true",
        "run --verbose output",
        "git push --dry-run origin",
        "path=/tmp/example",
    ],
)
def test_more_false_positives_are_left_intact(sample: str) -> None:
    assert redact_secret_fields({"text": sample}) == {"text": sample}


@pytest.mark.parametrize(
    "sample, expected",
    [
        ("password=a'b; keep=1", "password=***; keep=1"),
        ('password=a"b&keep=1', "password=***&keep=1"),
        ("password=a,b)c keep=1", "password=*** keep=1"),
        ("password='a b;c' keep=1", "password='***' keep=1"),
        ('token: "a b&c"; keep=1', 'token: "***"; keep=1'),
        ("password='unterminated keep=1", "password=*** keep=1"),
    ],
)
def test_free_text_value_is_masked_to_its_real_end(sample: str, expected: str) -> None:
    assert redact_secret_fields({"text": sample}) == {"text": expected}
