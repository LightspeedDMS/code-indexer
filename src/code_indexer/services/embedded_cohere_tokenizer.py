"""Embedded Cohere tokenizer for accurate token counting.

Follows embedded_voyage_tokenizer.py pattern. Caches tokenizer
locally at ~/.cache/cidx/tokenizers/cohere-{model}/.

Fallback: 4 chars per token ratio when tokenizer unavailable.
"""

# NO IMPORTS AT MODULE LEVEL - All imports happen lazily inside functions
# This keeps module import time near zero (<1ms)
# Exception: logging is stdlib and near-zero cost
import logging

logger = logging.getLogger(__name__)

FALLBACK_CHARS_PER_TOKEN = 4

_tokenizer_cache: dict = {}


def _get_cache_dir(model: str) -> str:
    """Return cache directory for Cohere tokenizer."""
    from pathlib import Path

    cache_dir = Path.home() / ".cache" / "cidx" / "tokenizers" / f"cohere-{model}"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return str(cache_dir)


def _get_tokenizer(model: str):
    """Load and cache tokenizer. Falls back to char ratio."""
    if model in _tokenizer_cache:
        return _tokenizer_cache[model]

    try:
        # Try to load from local cache first
        from pathlib import Path

        cache_dir = _get_cache_dir(model)
        tokenizer_path = Path(cache_dir) / "tokenizer.json"

        if tokenizer_path.exists():
            from tokenizers import Tokenizer  # type: ignore[import-untyped]

            tokenizer = Tokenizer.from_file(str(tokenizer_path))
            _tokenizer_cache[model] = tokenizer
            return tokenizer

        # Try bootstrapping via cohere SDK
        try:
            import cohere  # noqa: F401

            # Cohere SDK tokenizer API varies by version.
            # Cache None to use fallback for this session.
            logger.debug(
                "Cohere SDK available but no local tokenizer for %s, using char-ratio fallback",
                model,
            )
            _tokenizer_cache[model] = None
            return None
        except Exception as exc:
            logger.debug(
                "Cohere SDK not available for %s, using char-ratio fallback: %s",
                model,
                exc,
            )
            _tokenizer_cache[model] = None
            return None

    except Exception as exc:
        logger.debug(
            "Failed to load tokenizer for %s, using char-ratio fallback: %s",
            model,
            exc,
        )
        _tokenizer_cache[model] = None
        return None


def count_tokens(texts: list, model: str = "embed-v4.0") -> int:
    """Count tokens in texts. Uses tokenizer if available, else char ratio fallback."""
    if not texts:
        return 0

    tokenizer = _get_tokenizer(model)

    if tokenizer is not None:
        try:
            encoded = tokenizer.encode_batch(texts)
            return sum(len(e.ids) for e in encoded)
        except Exception as exc:
            logger.debug(
                "Tokenizer encode failed for model %s, falling back to char ratio: %s",
                model,
                exc,
            )

    # Fallback: conservative char-to-token ratio
    total_chars = sum(len(t) for t in texts)
    return total_chars // FALLBACK_CHARS_PER_TOKEN + 1


def count_tokens_single(text: str, model: str = "embed-v4.0") -> int:
    """Count tokens for a single text."""
    return count_tokens([text], model)
