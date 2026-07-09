"""Unit tests for the pluggable TemporalEmbedder adapter interface + registry (Story #1290).

The registry is the single wiring point that lets a new embedder be added as
one adapter with zero core indexer/recall change (Epic #1289 primary objective).
"""

import pytest

from src.code_indexer.services.temporal.embedders.base import TemporalEmbedder
from src.code_indexer.services.temporal.embedders.registry import (
    create_embedder,
    register_embedder,
    registered_embedder_names,
    unregister_embedder_for_tests,
)


class _FakeEmbedder(TemporalEmbedder):
    """Minimal concrete TemporalEmbedder for registry tests."""

    name = "fake-embedder"
    model_slug = "fake_embedder"
    dimensions = 4
    overlap_percentage = 0.0

    def embed_commit_chunks(self, chunks):
        return [[0.0, 0.0, 0.0, 0.0] for _ in chunks]

    def embed_query(self, text):
        return [0.0, 0.0, 0.0, 0.0]


class TestTemporalEmbedderIsAbstract:
    def test_cannot_instantiate_base_class_directly(self):
        with pytest.raises(TypeError):
            TemporalEmbedder()  # type: ignore[abstract]

    def test_concrete_subclass_exposes_contract_attributes(self):
        embedder = _FakeEmbedder()
        assert embedder.name == "fake-embedder"
        assert embedder.dimensions == 4
        assert embedder.overlap_percentage == 0.0
        assert embedder.embed_commit_chunks(["a", "b"]) == [
            [0.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 0.0],
        ]
        assert embedder.embed_query("q") == [0.0, 0.0, 0.0, 0.0]


class TestTemporalEmbedderRegistry:
    def setup_method(self):
        unregister_embedder_for_tests("fake-embedder")

    def teardown_method(self):
        unregister_embedder_for_tests("fake-embedder")

    def test_register_and_create_embedder_by_name(self):
        register_embedder("fake-embedder", lambda config: _FakeEmbedder())

        embedder = create_embedder("fake-embedder", config=None)

        assert isinstance(embedder, _FakeEmbedder)

    def test_create_unknown_embedder_raises_key_error(self):
        with pytest.raises(KeyError, match="unknown-embedder-xyz"):
            create_embedder("unknown-embedder-xyz", config=None)

    def test_registered_embedder_names_includes_registered_name(self):
        register_embedder("fake-embedder", lambda config: _FakeEmbedder())

        assert "fake-embedder" in registered_embedder_names()

    def test_voyage_context_4_is_registered_by_default(self):
        """AC: voyage-context-4 ships as a first-class adapter registered at import time."""
        assert "voyage-context-4" in registered_embedder_names()
