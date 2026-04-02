"""Tests for _get_tokenizer() in embedded_cohere_tokenizer - Bug #594.

TDD tests written BEFORE implementation (RED phase).
Scope: _get_tokenizer() behavior only.
"""

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
def fake_tokenizers_module():
    """Return a stub tokenizers module and a mock Tokenizer class."""
    mock_class = MagicMock()
    stub = ModuleType("tokenizers")
    stub.Tokenizer = mock_class
    return stub, mock_class


@pytest.fixture()
def patched_env(tmp_path, fake_tokenizers_module):
    """Patch _get_cache_dir and sys.modules['tokenizers'] together.

    Yields (tmp_path, mock_tokenizer_class).
    """
    stub, mock_class = fake_tokenizers_module
    with patch(
        "code_indexer.services.embedded_cohere_tokenizer._get_cache_dir",
        return_value=str(tmp_path),
    ):
        saved = sys.modules.get("tokenizers")
        sys.modules["tokenizers"] = stub
        try:
            yield tmp_path, mock_class
        finally:
            if saved is None:
                sys.modules.pop("tokenizers", None)
            else:
                sys.modules["tokenizers"] = saved


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGetTokenizerHuggingFaceDownload:
    """_get_tokenizer() downloads via from_pretrained when no local file present."""

    def test_calls_from_pretrained_when_no_local_file(self, patched_env):
        """No tokenizer.json in cache dir => from_pretrained('Cohere/embed-v4.0') called."""
        tmp_path, mock_class = patched_env
        mock_tok = MagicMock()
        mock_class.from_pretrained.return_value = mock_tok

        result = mod._get_tokenizer("embed-v4.0")

        mock_class.from_pretrained.assert_called_once_with("Cohere/embed-v4.0")
        assert result is mock_tok

    def test_does_not_call_from_pretrained_when_local_file_exists(self, patched_env):
        """tokenizer.json present in cache => from_file used, from_pretrained skipped."""
        tmp_path, mock_class = patched_env
        (tmp_path / "tokenizer.json").write_text('{"version": "1.0"}')
        mock_tok = MagicMock()
        mock_class.from_file.return_value = mock_tok

        result = mod._get_tokenizer("embed-v4.0")

        mock_class.from_file.assert_called_once_with(str(tmp_path / "tokenizer.json"))
        mock_class.from_pretrained.assert_not_called()
        assert result is mock_tok


class TestGetTokenizerFallbackToNone:
    """_get_tokenizer() returns None on failures."""

    def test_returns_none_when_from_pretrained_raises(self, patched_env):
        """Network/download failure => returns None instead of propagating."""
        tmp_path, mock_class = patched_env
        mock_class.from_pretrained.side_effect = OSError("network error")

        result = mod._get_tokenizer("embed-v4.0")

        assert result is None

    def test_returns_none_when_tokenizers_library_missing(self, tmp_path):
        """Missing tokenizers library => returns None without crashing."""
        with patch(
            "code_indexer.services.embedded_cohere_tokenizer._get_cache_dir",
            return_value=str(tmp_path),
        ):
            saved = sys.modules.pop("tokenizers", None)
            # Ensure the module is importable at test-suite level but blocked here
            stub = ModuleType("tokenizers")
            stub.Tokenizer = property(
                lambda s: (_ for _ in ()).throw(ImportError("no tokenizers"))
            )
            # Instead: block via _import_tokenizer_class
            with patch(
                "code_indexer.services.embedded_cohere_tokenizer._import_tokenizer_class",
                side_effect=ImportError("No module named 'tokenizers'"),
            ):
                result = mod._get_tokenizer("embed-v4.0")
            if saved is not None:
                sys.modules["tokenizers"] = saved

        assert result is None


class TestGetTokenizerCaching:
    """_get_tokenizer() caches results to avoid repeated I/O."""

    def test_caches_tokenizer_after_successful_load(self, patched_env):
        """Loaded tokenizer is stored in _tokenizer_cache."""
        tmp_path, mock_class = patched_env
        mock_tok = MagicMock()
        mock_class.from_pretrained.return_value = mock_tok

        mod._get_tokenizer("embed-v4.0")

        assert mod._tokenizer_cache.get("embed-v4.0") is mock_tok

    def test_second_call_uses_cache_not_from_pretrained(self, patched_env):
        """Second _get_tokenizer() call returns cached instance (from_pretrained once)."""
        tmp_path, mock_class = patched_env
        mock_tok = MagicMock()
        mock_class.from_pretrained.return_value = mock_tok

        r1 = mod._get_tokenizer("embed-v4.0")
        r2 = mod._get_tokenizer("embed-v4.0")

        assert r1 is mock_tok
        assert r2 is mock_tok
        assert mock_class.from_pretrained.call_count == 1

    def test_caches_none_after_failed_load(self, patched_env):
        """None is cached so repeated failures don't retry the network."""
        tmp_path, mock_class = patched_env
        mock_class.from_pretrained.side_effect = OSError("fail")

        mod._get_tokenizer("embed-v4.0")
        mod._get_tokenizer("embed-v4.0")

        assert mock_class.from_pretrained.call_count == 1
        assert "embed-v4.0" in mod._tokenizer_cache
        assert mod._tokenizer_cache["embed-v4.0"] is None

    def test_uses_in_memory_cache_skips_all_io(self):
        """Pre-populated cache returns stored tokenizer with zero I/O."""
        mock_tok = MagicMock()
        mod._tokenizer_cache["embed-v4.0"] = mock_tok

        with patch(
            "code_indexer.services.embedded_cohere_tokenizer._import_tokenizer_class"
        ) as mock_import:
            result = mod._get_tokenizer("embed-v4.0")

        mock_import.assert_not_called()
        assert result is mock_tok
