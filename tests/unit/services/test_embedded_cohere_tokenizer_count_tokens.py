"""Tests for count_tokens() and count_tokens_single() in embedded_cohere_tokenizer.

TDD tests written BEFORE implementation (RED phase).
Scope: count_tokens() and count_tokens_single() behavior only.
"""

import logging
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

import code_indexer.services.embedded_cohere_tokenizer as mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_tokenizer_cache():
    """Clear the in-memory tokenizer cache before and after every test."""
    mod._tokenizer_cache.clear()
    yield
    mod._tokenizer_cache.clear()


@pytest.fixture()
def mock_tokenizer():
    """Return a mock tokenizer with controllable encode_batch behavior."""
    tok = MagicMock()
    return tok


@pytest.fixture()
def patched_env_with_tokenizer(tmp_path, mock_tokenizer):
    """Patch _get_cache_dir and sys.modules so a working tokenizer is returned.

    Yields the mock tokenizer so tests can configure encode_batch behavior.
    """
    stub = ModuleType("tokenizers")
    stub_class = MagicMock()
    stub_class.from_pretrained.return_value = mock_tokenizer
    stub.Tokenizer = stub_class

    with patch(
        "code_indexer.services.embedded_cohere_tokenizer._get_cache_dir",
        return_value=str(tmp_path),
    ):
        saved = sys.modules.get("tokenizers")
        sys.modules["tokenizers"] = stub
        try:
            yield mock_tokenizer
        finally:
            if saved is None:
                sys.modules.pop("tokenizers", None)
            else:
                sys.modules["tokenizers"] = saved


@pytest.fixture()
def patched_env_no_tokenizer(tmp_path):
    """Patch so _import_tokenizer_class raises (forces char-ratio fallback)."""
    with patch(
        "code_indexer.services.embedded_cohere_tokenizer._get_cache_dir",
        return_value=str(tmp_path),
    ):
        with patch(
            "code_indexer.services.embedded_cohere_tokenizer._import_tokenizer_class",
            side_effect=ImportError("tokenizers not available"),
        ):
            yield


# ---------------------------------------------------------------------------
# Constant tests
# ---------------------------------------------------------------------------


class TestConstants:
    """Module-level constants have correct values."""

    def test_fallback_chars_per_token_is_3(self):
        """FALLBACK_CHARS_PER_TOKEN must be 3 for accurate code token estimation."""
        assert mod.FALLBACK_CHARS_PER_TOKEN == 3

    def test_fallback_warning_threshold_exported(self):
        """FALLBACK_WARNING_TOKEN_THRESHOLD is exported so tests can use it."""
        assert hasattr(mod, "FALLBACK_WARNING_TOKEN_THRESHOLD")
        assert mod.FALLBACK_WARNING_TOKEN_THRESHOLD > 0


# ---------------------------------------------------------------------------
# count_tokens() tests
# ---------------------------------------------------------------------------


class TestCountTokensEmpty:
    """count_tokens() edge cases for empty input."""

    def test_returns_zero_for_empty_list(self):
        """count_tokens([]) returns 0 without touching any tokenizer."""
        assert mod.count_tokens([]) == 0

    def test_returns_zero_for_empty_list_regardless_of_model(self):
        """count_tokens([], model) returns 0 for any model."""
        assert mod.count_tokens([], model="embed-v4.0") == 0


class TestCountTokensWithTokenizer:
    """count_tokens() uses tokenizer.encode_batch() when tokenizer loads."""

    def test_sums_ids_from_all_encodings(self, patched_env_with_tokenizer):
        """Returns sum of len(encoding.ids) across all encode_batch results."""
        tok = patched_env_with_tokenizer
        enc_a = MagicMock()
        enc_a.ids = [1, 2, 3]
        enc_b = MagicMock()
        enc_b.ids = [4, 5]
        tok.encode_batch.return_value = [enc_a, enc_b]

        result = mod.count_tokens(["hello", "world"])

        assert result == 5
        tok.encode_batch.assert_called_once_with(["hello", "world"])

    def test_passes_all_texts_to_encode_batch(self, patched_env_with_tokenizer):
        """All texts in the list are forwarded to encode_batch in one call."""
        tok = patched_env_with_tokenizer
        encs = [MagicMock() for _ in range(4)]
        for e in encs:
            e.ids = [0]
        tok.encode_batch.return_value = encs

        texts = ["a", "b", "c", "d"]
        mod.count_tokens(texts)

        tok.encode_batch.assert_called_once_with(texts)


class TestCountTokensFallback:
    """count_tokens() char-ratio fallback when tokenizer unavailable."""

    def test_fallback_uses_chars_divided_by_3(self, patched_env_no_tokenizer):
        """Fallback: total_chars // 3 + 1."""
        texts = ["abc", "defg"]  # 3 + 4 = 7 chars
        expected = 7 // mod.FALLBACK_CHARS_PER_TOKEN + 1

        result = mod.count_tokens(texts)

        assert result == expected

    def test_fallback_single_char(self, patched_env_no_tokenizer):
        """Single character text: 1 // 3 + 1 = 1."""
        result = mod.count_tokens(["x"])
        assert result == 1 // mod.FALLBACK_CHARS_PER_TOKEN + 1

    def test_fallback_on_encode_batch_exception(self, patched_env_with_tokenizer):
        """Falls back to char ratio when encode_batch raises at runtime."""
        tok = patched_env_with_tokenizer
        tok.encode_batch.side_effect = RuntimeError("encode failed")

        texts = ["abc", "defg"]  # 7 chars
        expected = 7 // mod.FALLBACK_CHARS_PER_TOKEN + 1

        result = mod.count_tokens(texts)

        assert result == expected


class TestCountTokensFallbackWarning:
    """count_tokens() emits WARNING when large batch uses char-ratio fallback."""

    def test_warns_when_estimated_tokens_exceed_threshold(
        self, patched_env_no_tokenizer, caplog
    ):
        """WARNING logged when estimated token count >= FALLBACK_WARNING_TOKEN_THRESHOLD."""
        threshold = mod.FALLBACK_WARNING_TOKEN_THRESHOLD
        # Produce enough chars to exceed threshold after //3 division
        chars_needed = threshold * mod.FALLBACK_CHARS_PER_TOKEN + 1
        big_text = "x" * chars_needed

        with caplog.at_level(
            logging.WARNING,
            logger="code_indexer.services.embedded_cohere_tokenizer",
        ):
            mod.count_tokens([big_text])

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) >= 1
        # Warning message must mention fallback or char-ratio
        combined = " ".join(r.message.lower() for r in warnings)
        assert "fallback" in combined or "char" in combined

    def test_no_warning_for_small_batch(self, patched_env_no_tokenizer, caplog):
        """No WARNING logged when estimated tokens are below the threshold."""
        with caplog.at_level(
            logging.WARNING,
            logger="code_indexer.services.embedded_cohere_tokenizer",
        ):
            mod.count_tokens(["hello world"])

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 0

    def test_no_warning_when_tokenizer_available(
        self, patched_env_with_tokenizer, caplog
    ):
        """No WARNING when real tokenizer is used (even for large input)."""
        tok = patched_env_with_tokenizer
        threshold = mod.FALLBACK_WARNING_TOKEN_THRESHOLD
        chars_needed = threshold * mod.FALLBACK_CHARS_PER_TOKEN + 1
        big_text = "x" * chars_needed

        # Make encode_batch return a plausible result
        enc = MagicMock()
        enc.ids = list(range(threshold + 10))
        tok.encode_batch.return_value = [enc]

        with caplog.at_level(
            logging.WARNING,
            logger="code_indexer.services.embedded_cohere_tokenizer",
        ):
            mod.count_tokens([big_text])

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 0


# ---------------------------------------------------------------------------
# count_tokens_single() tests
# ---------------------------------------------------------------------------


class TestCountTokensSingle:
    """count_tokens_single() is a single-text convenience wrapper."""

    def test_delegates_to_count_tokens(self, patched_env_with_tokenizer):
        """count_tokens_single(text, model) == count_tokens([text], model)."""
        tok = patched_env_with_tokenizer
        enc = MagicMock()
        enc.ids = [1, 2, 3, 4]
        tok.encode_batch.return_value = [enc]

        result = mod.count_tokens_single("hello world", "embed-v4.0")

        assert result == 4

    def test_passes_default_model(self, patched_env_with_tokenizer):
        """Default model 'embed-v4.0' is used when not specified."""
        tok = patched_env_with_tokenizer
        enc = MagicMock()
        enc.ids = [1, 2]
        tok.encode_batch.return_value = [enc]

        result = mod.count_tokens_single("hi")

        assert result == 2
