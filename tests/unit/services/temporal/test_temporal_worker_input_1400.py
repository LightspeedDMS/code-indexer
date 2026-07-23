"""
Tests for Story #1400 Phase 3: TemporalWorkerInput.

The FINAL LOCKED DESIGN's fully-typed dataclass enumerating every one of
execute_temporal_query_with_fusion's real fusion parameters explicitly (not
a vague "filters"/"temporal_params" catch-all), so both MCP and REST doors
can normalize their protocol-specific payload into ONE shared shape the
(future) worker consumes. min_score/file_extensions are deliberately NOT
carried forward for temporal (parity-preserving: today's inline path
already ignores them for temporal too) -- they are documented via the
*_ignored_for_temporal fields.

TDD: written BEFORE implementation.
"""

import pytest

from code_indexer.services.temporal.temporal_worker_input import (
    TemporalWorkerInput,
)


def _minimal_kwargs(**overrides):
    base = dict(
        repo_path="/repo",
        repository_alias="my-repo",
        username="alice",
        query_text="auth logic",
        requested_limit=10,
        fusion_fetch_limit=30,
        time_range=("2024-01-01", "2024-12-31"),
        time_range_raw=None,
        time_range_all=False,
        file_path_filter=None,
        provider_filter=None,
        at_commit=None,
        language=None,
        exclude_language=None,
        exclude_path=None,
        diff_types=None,
        author=None,
        chunk_type=None,
        no_embedding_cache_shortcut=False,
        temporal_embedder=None,
        rerank_query=None,
        rerank_instruction=None,
        min_score_ignored_for_temporal=None,
        file_extensions_ignored_for_temporal=None,
    )
    base.update(overrides)
    return base


class TestTemporalWorkerInputConstruction:
    def test_constructs_with_all_fields(self):
        wi = TemporalWorkerInput(**_minimal_kwargs())
        assert wi.repo_path == "/repo"
        assert wi.repository_alias == "my-repo"
        assert wi.username == "alice"
        assert wi.query_text == "auth logic"
        assert wi.requested_limit == 10
        assert wi.fusion_fetch_limit == 30
        assert wi.time_range == ("2024-01-01", "2024-12-31")

    def test_is_frozen(self):
        wi = TemporalWorkerInput(**_minimal_kwargs())
        with pytest.raises(Exception):
            wi.query_text = "changed"  # type: ignore[misc]

    def test_diff_types_accepts_tuple(self):
        wi = TemporalWorkerInput(**_minimal_kwargs(diff_types=("added", "modified")))
        assert wi.diff_types == ("added", "modified")

    def test_rerank_fields_optional(self):
        wi = TemporalWorkerInput(
            **_minimal_kwargs(
                rerank_query="auth logic", rerank_instruction="prioritize recency"
            )
        )
        assert wi.rerank_query == "auth logic"
        assert wi.rerank_instruction == "prioritize recency"

    def test_min_score_and_file_extensions_are_ignored_for_temporal_fields(self):
        """Documents the intentional parity gap -- these are NEVER forwarded
        to fusion, but the struct still carries them for observability."""
        wi = TemporalWorkerInput(
            **_minimal_kwargs(
                min_score_ignored_for_temporal=0.5,
                file_extensions_ignored_for_temporal=("py", "js"),
            )
        )
        assert wi.min_score_ignored_for_temporal == 0.5
        assert wi.file_extensions_ignored_for_temporal == ("py", "js")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
