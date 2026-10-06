"""Display masking of stored secrets reveals at most the last 4 characters."""

from __future__ import annotations

import pytest

from code_indexer.utils.credential_redaction import (
    DISPLAY_MASK_CHAR,
    is_display_mask,
    mask_stored_secret,
)

MASK = DISPLAY_MASK_CHAR * 8


@pytest.mark.parametrize(
    "secret", ["short", "operator-key-0019ch", "abcdefghijklmnop" + "WXYZ"]
)
def test_is_display_mask_recognises_every_display_form(secret) -> None:
    assert is_display_mask(mask_stored_secret(secret))


@pytest.mark.parametrize(
    "value",
    [
        "",
        None,
        42,
        "***",
        "dummy-***",
        "Configured",
        MASK + "abc",
        MASK + "abcde",
        DISPLAY_MASK_CHAR * 7 + "abcde",
        "x" + MASK + "abc",
        "sk-real-key-" + MASK + "tail",
    ],
)
def test_is_display_mask_rejects_everything_else(value) -> None:
    assert not is_display_mask(value)


@pytest.mark.parametrize("value", [None, ""])
def test_nothing_stored_shows_nothing(value) -> None:
    assert mask_stored_secret(value) == ""


@pytest.mark.parametrize(
    "value", ["a", "short-key", "abcdefgh" + "WXYZ", "operator-key-0019ch"]
)
def test_short_secret_shows_only_configured(value) -> None:
    # Operator-chosen short keys (under 20 chars) reveal nothing at all.
    assert len(value) < 20
    assert mask_stored_secret(value) == "configured"


def test_twenty_char_secret_shows_last_four_only() -> None:
    secret = "abcdefghijklmnop" + "WXYZ"
    assert len(secret) == 20
    assert mask_stored_secret(secret) == MASK + "WXYZ"


def test_long_token_never_reveals_its_prefix() -> None:
    token = "ghp_" + "Ex4mpleSampleValue0000000000000000" + "k9Q2"
    masked = mask_stored_secret(token)
    assert masked == MASK + "k9Q2"
    assert "ghp_" not in masked
    assert token[:-4] not in masked
    # Fixed width: the mask does not leak the secret's length.
    assert len(masked) == len(mask_stored_secret("x" * 200))


def test_config_page_token_view_carries_no_raw_token() -> None:
    from code_indexer.server.services.ci_token_manager import TokenData
    from code_indexer.server.web.routes import _ci_token_display

    token = "glpat-" + "ExampleSampleValue0000" + "r7T1"
    view = _ci_token_display(
        TokenData(platform="gitlab", token=token, base_url="https://gitlab.example.com")
    )
    assert view == {
        "masked_token": MASK + "r7T1",
        "base_url": "https://gitlab.example.com",
    }
    assert token not in repr(view)
    assert _ci_token_display(None) is None
