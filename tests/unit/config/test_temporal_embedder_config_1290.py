"""Unit tests for pluggable TemporalEmbedder config fields (Story #1290).

Covers the new TemporalConfig fields introduced for per-commit contextualized
temporal indexing: `embedders` (set of configured embedder names), `active_embedder`
(must be a member of `embedders`), and `aggregation_chunk_chars` (per-commit
aggregation chunk size in characters, default 4096).
"""

import pytest
from pydantic import ValidationError

from src.code_indexer.config import Config, TemporalConfig


class TestTemporalEmbedderConfig1290:
    """AC: pluggable embedder config fields on TemporalConfig."""

    def test_default_embedders_is_voyage_context_4(self):
        """Default embedders set contains only voyage-context-4."""
        config = Config()
        assert config.temporal.embedders == ["voyage-context-4"]

    def test_default_active_embedder_is_voyage_context_4(self):
        """Default active_embedder is voyage-context-4."""
        config = Config()
        assert config.temporal.active_embedder == "voyage-context-4"

    def test_default_aggregation_chunk_chars_is_4096(self):
        """Default aggregation_chunk_chars is 4096."""
        config = Config()
        assert config.temporal.aggregation_chunk_chars == 4096

    def test_active_embedder_must_be_member_of_embedders(self):
        """active_embedder not present in embedders raises ValidationError."""
        with pytest.raises(ValidationError, match="active_embedder"):
            TemporalConfig(
                embedders=["voyage-context-4"],
                active_embedder="cohere-embed-v4",
            )

    def test_active_embedder_valid_when_in_embedders_set(self):
        """active_embedder present in embedders passes validation."""
        config = TemporalConfig(
            embedders=["voyage-context-4", "cohere-embed-v4"],
            active_embedder="cohere-embed-v4",
        )
        assert config.active_embedder == "cohere-embed-v4"

    def test_embedders_rejects_empty_set(self):
        """embedders must be non-empty."""
        with pytest.raises(ValidationError, match="embedders"):
            TemporalConfig(embedders=[], active_embedder="voyage-context-4")

    def test_embedders_deduplicates_entries(self):
        """embedders list is deduplicated while preserving order."""
        config = TemporalConfig(
            embedders=["voyage-context-4", "voyage-context-4", "cohere-embed-v4"],
            active_embedder="voyage-context-4",
        )
        assert config.embedders == ["voyage-context-4", "cohere-embed-v4"]

    def test_aggregation_chunk_chars_rejects_non_positive(self):
        """aggregation_chunk_chars must be positive."""
        with pytest.raises(ValidationError, match="greater than 0"):
            TemporalConfig(aggregation_chunk_chars=0)
