"""Tests for cache-first HuggingFace tokenizer loading in VoyageTokenizer.

TDD test file written BEFORE implementation (RED phase).
Tests the new _resolve_hf_cache_path() method and the modified _get_tokenizer()
cache-first behavior added to VoyageTokenizer.
"""

import os
import time
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.services.embedded_voyage_tokenizer import VoyageTokenizer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HF_MODEL_DIR_TEMPLATE = "models--voyageai--{model}"


def _make_hf_snapshot(
    tmp_path: Path,
    model: str = "voyage-3",
    snapshot_name: str = "abc123",
    include_tokenizer_json: bool = True,
    hf_home: Optional[Path] = None,
) -> Path:
    """Build a minimal HuggingFace cache directory structure for a model.

    Returns the path to the tokenizer.json file (or snapshot dir if no
    tokenizer.json is created).
    """
    cache_root = hf_home if hf_home is not None else tmp_path / ".cache" / "huggingface"
    snapshots_dir = (
        cache_root / "hub" / HF_MODEL_DIR_TEMPLATE.format(model=model) / "snapshots"
    )
    snapshot_dir = snapshots_dir / snapshot_name
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    if include_tokenizer_json:
        tokenizer_json = snapshot_dir / "tokenizer.json"
        tokenizer_json.write_text('{"version": "1.0"}')
        return tokenizer_json
    return snapshot_dir


def _make_import_patcher(mock_tokenizer_class):
    """Return an __import__ side-effect that substitutes tokenizers.Tokenizer.

    Required because _get_tokenizer() uses a lazy 'from tokenizers import Tokenizer'
    inside the function body, which cannot be patched at module level.
    """
    import builtins

    real_import = builtins.__import__

    def _patched_import(name, *args, **kwargs):
        if name == "tokenizers":
            mock_module = MagicMock()
            mock_module.Tokenizer = mock_tokenizer_class
            return mock_module
        return real_import(name, *args, **kwargs)

    return _patched_import


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_tokenizer_cache():
    """Clear the in-memory tokenizer cache before and after every test."""
    VoyageTokenizer._tokenizer_cache.clear()
    yield
    VoyageTokenizer._tokenizer_cache.clear()


# ---------------------------------------------------------------------------
# Tests for _resolve_hf_cache_path()
# ---------------------------------------------------------------------------


class TestResolveHfCachePath:
    """Pure filesystem tests for _resolve_hf_cache_path() - no mocking needed."""

    def test_resolve_hf_cache_path_returns_path_when_cache_exists(self, tmp_path):
        """Returns the tokenizer.json path when a valid HF cache structure exists."""
        tokenizer_json = _make_hf_snapshot(tmp_path, model="voyage-3")

        hf_home = str(tmp_path / ".cache" / "huggingface")
        with patch.dict(os.environ, {"HF_HOME": hf_home}):
            result = VoyageTokenizer._resolve_hf_cache_path("voyage-3")

        assert result is not None
        assert result == tokenizer_json
        assert result.name == "tokenizer.json"
        assert result.exists()

    def test_resolve_hf_cache_path_returns_none_when_no_snapshots_dir(self, tmp_path):
        """Returns None when the model directory exists but has no snapshots subdir."""
        # Create the model directory but no snapshots subdir inside it
        model_dir = (
            tmp_path
            / ".cache"
            / "huggingface"
            / "hub"
            / HF_MODEL_DIR_TEMPLATE.format(model="voyage-3")
        )
        model_dir.mkdir(parents=True)

        hf_home = str(tmp_path / ".cache" / "huggingface")
        with patch.dict(os.environ, {"HF_HOME": hf_home}):
            result = VoyageTokenizer._resolve_hf_cache_path("voyage-3")

        assert result is None

    def test_resolve_hf_cache_path_returns_none_when_empty_snapshots(self, tmp_path):
        """Returns None when the snapshots directory exists but is empty."""
        snapshots_dir = (
            tmp_path
            / ".cache"
            / "huggingface"
            / "hub"
            / HF_MODEL_DIR_TEMPLATE.format(model="voyage-3")
            / "snapshots"
        )
        snapshots_dir.mkdir(parents=True)

        hf_home = str(tmp_path / ".cache" / "huggingface")
        with patch.dict(os.environ, {"HF_HOME": hf_home}):
            result = VoyageTokenizer._resolve_hf_cache_path("voyage-3")

        assert result is None

    def test_resolve_hf_cache_path_returns_none_when_no_tokenizer_json(self, tmp_path):
        """Returns None when snapshot dir exists but contains no tokenizer.json."""
        _make_hf_snapshot(
            tmp_path, model="voyage-3", include_tokenizer_json=False
        )

        hf_home = str(tmp_path / ".cache" / "huggingface")
        with patch.dict(os.environ, {"HF_HOME": hf_home}):
            result = VoyageTokenizer._resolve_hf_cache_path("voyage-3")

        assert result is None

    def test_resolve_hf_cache_path_selects_newest_snapshot(self, tmp_path):
        """Selects the most recently modified snapshot directory."""
        cache_root = tmp_path / ".cache" / "huggingface"
        snapshots_dir = (
            cache_root
            / "hub"
            / HF_MODEL_DIR_TEMPLATE.format(model="voyage-3")
            / "snapshots"
        )

        # Create older snapshot
        old_dir = snapshots_dir / "old_snapshot"
        old_dir.mkdir(parents=True)
        old_tokenizer = old_dir / "tokenizer.json"
        old_tokenizer.write_text('{"version": "old"}')

        # Ensure old snapshot has an older mtime (1 hour ago)
        old_time = time.time() - 3600
        os.utime(old_dir, (old_time, old_time))

        # Create newer snapshot
        new_dir = snapshots_dir / "new_snapshot"
        new_dir.mkdir(parents=True)
        new_tokenizer = new_dir / "tokenizer.json"
        new_tokenizer.write_text('{"version": "new"}')

        # Ensure new snapshot has a more recent mtime (1 minute ago)
        new_time = time.time() - 60
        os.utime(new_dir, (new_time, new_time))

        hf_home = str(cache_root)
        with patch.dict(os.environ, {"HF_HOME": hf_home}):
            result = VoyageTokenizer._resolve_hf_cache_path("voyage-3")

        assert result is not None
        assert result == new_tokenizer
        assert "new_snapshot" in str(result)

    def test_resolve_hf_cache_path_respects_hf_home_env(self, tmp_path):
        """Uses HF_HOME environment variable as the cache root instead of default."""
        custom_hf_home = tmp_path / "custom_hf_cache"
        tokenizer_json = _make_hf_snapshot(
            tmp_path, model="voyage-3", hf_home=custom_hf_home
        )

        with patch.dict(os.environ, {"HF_HOME": str(custom_hf_home)}):
            result = VoyageTokenizer._resolve_hf_cache_path("voyage-3")

        assert result is not None
        assert result == tokenizer_json
        # Confirm it came from the custom path
        assert str(custom_hf_home) in str(result)

    def test_resolve_hf_cache_path_handles_symlinked_tokenizer_json(self, tmp_path):
        """Returns correct path when tokenizer.json is a symlink (real HF layout)."""
        # Create blob target (HF content-addressed store pattern)
        blob = tmp_path / "blobs" / "abc123def"
        blob.parent.mkdir(parents=True)
        blob.write_text('{"version": "1.0"}')

        snap_dir = (
            tmp_path
            / ".cache"
            / "huggingface"
            / "hub"
            / "models--voyageai--voyage-3"
            / "snapshots"
            / "abc123"
        )
        snap_dir.mkdir(parents=True)
        (snap_dir / "tokenizer.json").symlink_to(blob)

        with patch.dict(
            os.environ, {"HF_HOME": str(tmp_path / ".cache" / "huggingface")}
        ):
            result = VoyageTokenizer._resolve_hf_cache_path("voyage-3")

        assert result is not None
        assert result.is_file()


# ---------------------------------------------------------------------------
# Tests for _get_tokenizer() cache-first behavior
# ---------------------------------------------------------------------------


class TestGetTokenizerCacheFirst:
    """Tests for the modified _get_tokenizer() with cache-first loading.

    Mocking is required here because from_pretrained() and from_file() would
    make real network calls or require real tokenizer files on disk.
    """

    def _make_mock_tokenizer(self):
        """Create a mock tokenizer instance with required methods."""
        mock_tok = MagicMock()
        mock_tok.no_truncation = MagicMock()
        return mock_tok

    def test_get_tokenizer_uses_from_file_when_cache_exists(self, tmp_path):
        """Calls Tokenizer.from_file() when a valid HF cache path is found."""
        tokenizer_json = _make_hf_snapshot(tmp_path, model="voyage-3")
        mock_tok = self._make_mock_tokenizer()

        MockTokenizerClass = MagicMock()
        MockTokenizerClass.from_file = MagicMock(return_value=mock_tok)
        MockTokenizerClass.from_pretrained = MagicMock(return_value=mock_tok)

        with patch.object(
            VoyageTokenizer,
            "_resolve_hf_cache_path",
            return_value=tokenizer_json,
        ) as mock_resolve:
            with patch("builtins.__import__", side_effect=_make_import_patcher(MockTokenizerClass)):
                result = VoyageTokenizer._get_tokenizer("voyage-3")

            mock_resolve.assert_called_once_with("voyage-3")
            MockTokenizerClass.from_file.assert_called_once_with(str(tokenizer_json))
            MockTokenizerClass.from_pretrained.assert_not_called()
            assert result is mock_tok

    def test_get_tokenizer_falls_back_to_from_pretrained_when_no_cache(self, tmp_path):
        """Falls back to from_pretrained() when no local HF cache is available."""
        mock_tok = self._make_mock_tokenizer()

        MockTokenizerClass = MagicMock()
        MockTokenizerClass.from_file = MagicMock()
        MockTokenizerClass.from_pretrained = MagicMock(return_value=mock_tok)

        with patch.object(
            VoyageTokenizer,
            "_resolve_hf_cache_path",
            return_value=None,
        ) as mock_resolve:
            with patch("builtins.__import__", side_effect=_make_import_patcher(MockTokenizerClass)):
                result = VoyageTokenizer._get_tokenizer("voyage-3")

            mock_resolve.assert_called_once_with("voyage-3")
            MockTokenizerClass.from_file.assert_not_called()
            MockTokenizerClass.from_pretrained.assert_called_once_with("voyageai/voyage-3")
            assert result is mock_tok

    def test_get_tokenizer_falls_back_on_corrupted_cache(self, tmp_path):
        """Falls back to from_pretrained() silently when from_file() raises an exception."""
        tokenizer_json = _make_hf_snapshot(tmp_path, model="voyage-3")
        mock_tok = self._make_mock_tokenizer()

        MockTokenizerClass = MagicMock()
        # from_file raises to simulate a corrupted cache file
        MockTokenizerClass.from_file = MagicMock(
            side_effect=Exception("Corrupted tokenizer file")
        )
        MockTokenizerClass.from_pretrained = MagicMock(return_value=mock_tok)

        with patch.object(
            VoyageTokenizer,
            "_resolve_hf_cache_path",
            return_value=tokenizer_json,
        ):
            with patch("builtins.__import__", side_effect=_make_import_patcher(MockTokenizerClass)):
                result = VoyageTokenizer._get_tokenizer("voyage-3")

            # Should have tried from_file, failed silently, then used from_pretrained
            MockTokenizerClass.from_file.assert_called_once()
            MockTokenizerClass.from_pretrained.assert_called_once_with("voyageai/voyage-3")
            assert result is mock_tok

    def test_in_memory_cache_still_works(self):
        """Second call to _get_tokenizer returns cached instance without any loading."""
        mock_tok = MagicMock()

        # Pre-populate the in-memory cache to simulate a prior call
        VoyageTokenizer._tokenizer_cache["voyage-3"] = mock_tok

        MockTokenizerClass = MagicMock()
        MockTokenizerClass.from_file = MagicMock()
        MockTokenizerClass.from_pretrained = MagicMock()

        with patch.object(VoyageTokenizer, "_resolve_hf_cache_path") as mock_resolve:
            with patch("builtins.__import__", side_effect=_make_import_patcher(MockTokenizerClass)):
                result = VoyageTokenizer._get_tokenizer("voyage-3")

            # Cache hit: no I/O, no network, no resolve call needed
            mock_resolve.assert_not_called()
            MockTokenizerClass.from_file.assert_not_called()
            MockTokenizerClass.from_pretrained.assert_not_called()
            assert result is mock_tok


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


class TestGetTokenizerErrorHandling:
    """Tests verifying unchanged error-handling behavior."""

    def test_import_error_still_raised_when_tokenizers_not_installed(self):
        """ImportError is raised when the tokenizers package is not available."""
        import builtins

        real_import = builtins.__import__

        def import_blocker(name, *args, **kwargs):
            if name == "tokenizers":
                raise ImportError("No module named 'tokenizers'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_blocker):
            with pytest.raises(ImportError, match="tokenizers"):
                VoyageTokenizer._get_tokenizer("voyage-3")

    def test_invalid_model_name_still_raises(self):
        """Invalid model name raises an exception after emitting a warning."""
        MockTokenizerClass = MagicMock()
        MockTokenizerClass.from_pretrained = MagicMock(
            side_effect=Exception("Invalid model: voyageai/not-a-real-model")
        )

        with patch.object(
            VoyageTokenizer,
            "_resolve_hf_cache_path",
            return_value=None,
        ):
            with patch("builtins.__import__", side_effect=_make_import_patcher(MockTokenizerClass)):
                with pytest.warns(UserWarning, match="Failed to load the tokenizer"):
                    with pytest.raises(Exception, match="Invalid model"):
                        VoyageTokenizer._get_tokenizer("not-a-real-model")


# ---------------------------------------------------------------------------
# Integration test (conditional on real HF cache being present)
# ---------------------------------------------------------------------------


def _hf_cache_has_voyage3() -> bool:
    """Return True if voyage-3 tokenizer.json exists in the real HF cache."""
    hf_home = Path(os.environ.get("HF_HOME", "~/.cache/huggingface")).expanduser()
    snapshots_dir = hf_home / "hub" / "models--voyageai--voyage-3" / "snapshots"
    if not snapshots_dir.is_dir():
        return False
    for snap in snapshots_dir.iterdir():
        if (snap / "tokenizer.json").exists():
            return True
    return False


@pytest.mark.skipif(
    not _hf_cache_has_voyage3(),
    reason="Real HuggingFace cache for voyage-3 not present on this machine",
)
class TestIntegrationCacheVsNetwork:
    """Integration tests comparing from_file vs from_pretrained token counts.

    Only executed when a real HF cache for voyage-3 exists locally.
    """

    SAMPLE_TEXTS = [
        "Hello world",
        "def fibonacci(n): return n if n <= 1 else fibonacci(n-1) + fibonacci(n-2)",
        "The quick brown fox jumps over the lazy dog. " * 20,
    ]

    def test_token_counts_identical_from_file_vs_from_pretrained(self):
        """Token counts from local cache must match those from the network download."""
        from tokenizers import Tokenizer  # type: ignore[import-untyped]

        # Load via local cache path
        cache_path = VoyageTokenizer._resolve_hf_cache_path("voyage-3")
        assert cache_path is not None, "Expected valid cache path for this integration test"

        tok_file = Tokenizer.from_file(str(cache_path))
        tok_file.no_truncation()

        # Load via network (HF's own caching ensures same tokenizer)
        tok_net = Tokenizer.from_pretrained("voyageai/voyage-3")
        tok_net.no_truncation()

        for text in self.SAMPLE_TEXTS:
            count_file = len(tok_file.encode(text).ids)
            count_net = len(tok_net.encode(text).ids)
            assert count_file == count_net, (
                f"Token count mismatch for text {text!r}: "
                f"from_file={count_file}, from_pretrained={count_net}"
            )
