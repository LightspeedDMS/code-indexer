"""Embedded Cohere tokenizer for accurate token counting.

Follows embedded_voyage_tokenizer.py pattern. Caches tokenizer
locally at ~/.cache/cidx/tokenizers/cohere-{model}/.

Fallback: 3 chars per token ratio when tokenizer unavailable.
"""

# NO IMPORTS AT MODULE LEVEL - All imports happen lazily inside functions
# This keeps module import time near zero (<1ms)
# Exception: logging is stdlib and near-zero cost
import logging

logger = logging.getLogger(__name__)

FALLBACK_CHARS_PER_TOKEN = 3
FALLBACK_WARNING_TOKEN_THRESHOLD = 50_000

_tokenizer_cache: dict = {}


def _get_cache_dir(model: str) -> str:
    """Return cache directory for Cohere tokenizer."""
    from pathlib import Path

    cache_dir = Path.home() / ".cache" / "cidx" / "tokenizers" / f"cohere-{model}"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return str(cache_dir)


def _import_tokenizer_class():
    """Import and return the Tokenizer class from the tokenizers library.

    Extracted as a separate function so tests can patch it independently.
    Raises ImportError when the tokenizers library is not installed.
    """
    from tokenizers import Tokenizer  # type: ignore[import-untyped]

    return Tokenizer


def _get_tokenizer(model: str):
    """Load and cache tokenizer. Falls back to None on any failure."""
    if model in _tokenizer_cache:
        return _tokenizer_cache[model]

    try:
        from pathlib import Path

        cache_dir = _get_cache_dir(model)
        tokenizer_path = Path(cache_dir) / "tokenizer.json"

        TokenizerClass = _import_tokenizer_class()

        if tokenizer_path.exists():
            tokenizer = TokenizerClass.from_file(str(tokenizer_path))
            _tokenizer_cache[model] = tokenizer
            return tokenizer

        # Download from HuggingFace Hub via from_pretrained
        tokenizer = TokenizerClass.from_pretrained(f"Cohere/{model}")
        _tokenizer_cache[model] = tokenizer
        return tokenizer

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
    estimated = total_chars // FALLBACK_CHARS_PER_TOKEN + 1

    if estimated >= FALLBACK_WARNING_TOKEN_THRESHOLD:
        logger.warning(
            "Using char-ratio fallback for token counting (no tokenizer available). "
            "Estimated %d tokens from %d chars. Install 'tokenizers' library for accuracy.",
            estimated,
            total_chars,
        )

    return estimated


def count_tokens_single(text: str, model: str = "embed-v4.0") -> int:
    """Count tokens for a single text."""
    return count_tokens([text], model)
