"""Embedded VoyageAI tokenizer - minimal extraction from voyageai library.

This module provides token counting functionality for VoyageAI models without
requiring the full voyageai library import (which adds 440ms+ import overhead).

We only need the tokenizer for accurate token counting before sending batches
to VoyageAI API. This implementation:
- Uses the tokenizers library directly (11ms import vs 440ms+ for voyageai)
- Loads official VoyageAI tokenizers from HuggingFace
- Caches tokenizers per model for performance
- Provides identical token counts to voyageai.Client.count_tokens()
- Zero imports at module level for maximum performance

Original voyageai implementation:
https://github.com/voyage-ai/voyageai-python/blob/main/voyageai/_base.py
"""

# NO IMPORTS AT MODULE LEVEL - All imports happen lazily inside functions
# This keeps module import time near zero (<1ms)


class VoyageTokenizer:
    """Minimal VoyageAI tokenizer for token counting.

    This is a direct extraction of the tokenization logic from voyageai._base._BaseClient,
    avoiding the overhead of importing the entire voyageai library.
    """

    # Cache for loaded tokenizers (model_name -> tokenizer instance)
    _tokenizer_cache: dict[str, object] = {}

    @staticmethod
    def _resolve_hf_cache_path(model: str):  # type: (...) -> object
        """Resolve the tokenizer.json path from the local HuggingFace cache.

        Checks the standard HuggingFace cache directory structure for a locally
        downloaded tokenizer, avoiding a network round-trip when the model has
        already been fetched.  The cache root is taken from the ``HF_HOME``
        environment variable, defaulting to ``~/.cache/huggingface``.

        Args:
            model: VoyageAI model name (e.g., 'voyage-3')

        Returns:
            pathlib.Path pointing to ``tokenizer.json`` inside the newest
            snapshot directory, or ``None`` if no valid cached tokenizer is
            found.
        """
        # Lazy imports - keep module-level import time near zero
        import os
        from pathlib import Path

        cache_root = Path(
            os.environ.get("HF_HOME", "~/.cache/huggingface")
        ).expanduser()

        snapshots_dir = cache_root / "hub" / f"models--voyageai--{model}" / "snapshots"

        if not snapshots_dir.is_dir():
            return None

        # Collect subdirectories - each one is a snapshot hash
        snapshot_dirs = [p for p in snapshots_dir.iterdir() if p.is_dir()]
        if not snapshot_dirs:
            return None

        # Pick the most recently modified snapshot
        snapshot_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        newest = snapshot_dirs[0]

        tokenizer_json = newest / "tokenizer.json"
        if not tokenizer_json.is_file():
            return None

        return tokenizer_json

    @staticmethod
    def _get_tokenizer(model: str):
        """Load and cache tokenizer for a specific model.

        Attempts to load from the local HuggingFace cache first (no network
        required), then falls back to ``Tokenizer.from_pretrained()`` if no
        cached file is found or if the cached file is unreadable.

        Args:
            model: VoyageAI model name (e.g., 'voyage-code-3')

        Returns:
            Tokenizer instance from HuggingFace

        Raises:
            ImportError: If tokenizers package is not installed
            Exception: If model tokenizer cannot be loaded
        """
        # Check in-memory cache first
        if model in VoyageTokenizer._tokenizer_cache:
            return VoyageTokenizer._tokenizer_cache[model]

        # Lazy import - only load when first needed
        try:
            from tokenizers import Tokenizer  # type: ignore[import-untyped]
        except ImportError:
            raise ImportError(
                "The package `tokenizers` is required for VoyageAI token counting. "
                "Please run `pip install tokenizers` to install the dependency."
            )

        import warnings

        # --- Cache-first path: try local HuggingFace cache before network ---
        cached_path = VoyageTokenizer._resolve_hf_cache_path(model)
        if cached_path is not None:
            try:
                tokenizer = Tokenizer.from_file(str(cached_path))
                tokenizer.no_truncation()
                VoyageTokenizer._tokenizer_cache[model] = tokenizer
                return tokenizer
            except Exception as exc:
                # APPROVED FALLBACK: 2026-03-08 - Story #384 cache corruption degrades to network
                import logging

                logging.getLogger(__name__).debug(
                    "HF cache tokenizer unreadable, falling back to network: %s", exc
                )

        # --- Fallback: download from HuggingFace (original behaviour) ---
        try:
            # Load official VoyageAI tokenizer from HuggingFace
            tokenizer = Tokenizer.from_pretrained(f"voyageai/{model}")
            tokenizer.no_truncation()

            # Cache for future use
            VoyageTokenizer._tokenizer_cache[model] = tokenizer

            return tokenizer
        except Exception:
            warnings.warn(
                f"Failed to load the tokenizer for `{model}`. "
                "Please ensure that it is a valid VoyageAI model name."
            )
            raise

    @staticmethod
    def count_tokens(texts, model):  # type: (list[str], str) -> int
        """Count tokens accurately using VoyageAI's official tokenizer.

        Args:
            texts: List of text strings to tokenize
            model: VoyageAI model name (e.g., 'voyage-code-3')

        Returns:
            Total token count across all texts

        Examples:
            >>> tokenizer = VoyageTokenizer()
            >>> tokenizer.count_tokens(["Hello world"], "voyage-code-3")
            2
            >>> tokenizer.count_tokens(["Hello", "world"], "voyage-code-3")
            2
        """
        if not texts:
            return 0

        # Get cached tokenizer for this model
        tokenizer = VoyageTokenizer._get_tokenizer(model)

        # Tokenize all texts in batch
        encodings = tokenizer.encode_batch(texts)

        # Count total tokens
        return sum(len(encoding.ids) for encoding in encodings)

    @staticmethod
    def tokenize(texts, model):  # type: (list[str], str) -> list[list[int]]
        """Tokenize texts and return token IDs.

        Args:
            texts: List of text strings to tokenize
            model: VoyageAI model name (e.g., 'voyage-code-3')

        Returns:
            List of token ID lists, one per input text

        Examples:
            >>> tokenizer = VoyageTokenizer()
            >>> tokenizer.tokenize(["Hello world"], "voyage-code-3")
            [[9707, 1879]]
        """
        if not texts:
            return []

        # Get cached tokenizer for this model
        tokenizer = VoyageTokenizer._get_tokenizer(model)

        # Tokenize all texts in batch
        encodings = tokenizer.encode_batch(texts)

        # Return token IDs
        return [encoding.ids for encoding in encodings]
