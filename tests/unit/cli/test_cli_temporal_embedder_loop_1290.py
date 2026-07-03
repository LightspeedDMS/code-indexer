"""Story #1290: the CLI's additional-temporal-embedder loop must iterate
config.temporal.embedders, NOT EmbeddingProviderFactory.get_configured_providers.

BUG DISCOVERED VIA E2E TESTING: the pre-#1290 "Multi-provider temporal loop
(Story #640)" iterated every CONFIGURED regular embedding provider (e.g.
voyage-ai AND cohere, since both provider sub-configs always exist in
config.json) and tried to build a SECOND TemporalIndexer for each one,
resolving its collection name via resolve_temporal_collection_from_config()
(which reads config.embedding_provider / config.cohere.model -- the REGULAR
semantic-search model, completely unrelated to the temporal embedder
registry). In practice this ALWAYS crashes on any default config (Cohere's
default model "embed-v4.0" has no registered TemporalEmbedder adapter, no
COHERE_API_KEY, and the wrong collection-naming scheme), even though the
PRIMARY voyage-context-4 temporal indexing already succeeded.

Source-order regression tests in test_cli_temporal_multi_provider_display_1205.py
guard the Rich Live display plumbing around this loop and must stay green;
this test guards the ITERATION SOURCE fix only.
"""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLI_PATH = _REPO_ROOT / "src" / "code_indexer" / "cli.py"


def _read_cli_source() -> str:
    return _CLI_PATH.read_text()


def test_extra_temporal_providers_sourced_from_temporal_embedders_config():
    """The extra-embedder loop must NOT use EmbeddingProviderFactory to decide
    which temporal shards to build -- it must use config.temporal.embedders."""
    source = _read_cli_source()

    loop_marker = (
        "for _extra_idx, _extra_provider in enumerate(_extra_temporal_providers):"
    )
    loop_pos = source.find(loop_marker)
    assert loop_pos != -1, "Extra-embedder loop not found in cli.py"

    # The iteration source assignment must appear BEFORE the loop and must
    # reference config.temporal.embedders, not EmbeddingProviderFactory.
    pre_loop = source[:loop_pos]
    assignment_pos = pre_loop.rfind("_extra_temporal_providers")
    assert assignment_pos != -1, (
        "_extra_temporal_providers assignment not found before the loop"
    )

    # Scan backward from the loop to the most recent occurrence of the
    # config.temporal.embedders reference, and confirm no
    # EmbeddingProviderFactory.get_configured_providers call remains in
    # scope for building the extra-embedder set.
    window = source[max(0, assignment_pos - 800) : loop_pos]
    assert "config.temporal.embedders" in window, (
        "Story #1290: the extra-embedder set must be derived from "
        "config.temporal.embedders (the per-commit embedder registry), not "
        "EmbeddingProviderFactory.get_configured_providers. "
        f"Window inspected: {window!r}"
    )
    assert "get_configured_providers" not in window, (
        "Story #1290: EmbeddingProviderFactory.get_configured_providers must "
        "no longer drive the extra-temporal-embedder loop -- it iterates the "
        "regular semantic-search provider list, not the temporal embedder "
        "registry, and produces guaranteed-wrong collection names."
    )


def test_primary_temporal_indexer_collection_name_uses_active_embedder():
    """The PRIMARY TemporalIndexer construction must use a collection_name
    derived from config.temporal.active_embedder (resolve_temporal_collection_name),
    not resolve_temporal_collection_from_config(config) (the regular
    semantic-search provider/model scheme).

    BUG DISCOVERED VIA E2E TESTING: with the old scheme, the primary
    TemporalIndexer's bookkeeping collection_name (e.g.
    "code-indexer-temporal-voyage_code_3") never matches the ACTUAL
    per-commit quarterly shard the indexer writes to (e.g.
    "code-indexer-temporal-voyage_context_4-2026Q3"). AC19/20 blank-out then
    hard-deletes the mismatched legacy-named directory (it lacks a v2 marker)
    on a subsequent run, so the "no commits to reconcile" early-return path's
    `end_indexing(collection_name=self.collection_name)` call fails with
    "Collection does not exist" -- a rerun-breaking crash.
    """
    source = _read_cli_source()

    marker = "temporal_indexer = TemporalIndexer(\n                    config_manager, vector_store, collection_name=_temporal_coll_name"
    assert marker in source, (
        "Primary TemporalIndexer construction site not found in cli.py "
        "(expected literal collection_name=_temporal_coll_name call)"
    )

    construct_pos = source.find(marker)
    # The value assigned to _temporal_coll_name must come from
    # resolve_temporal_collection_name(config.temporal.active_embedder),
    # looked up in the ~500 chars preceding the construction call.
    window = source[max(0, construct_pos - 500) : construct_pos]
    assert "resolve_temporal_collection_name(" in window, (
        "Story #1290: _temporal_coll_name must be resolved via "
        "resolve_temporal_collection_name(config.temporal.active_embedder), "
        f"not the regular provider scheme. Window: {window!r}"
    )
    assert "config.temporal.active_embedder" in window, (
        "Story #1290: _temporal_coll_name must be derived from "
        f"config.temporal.active_embedder. Window: {window!r}"
    )


def test_active_embedder_excluded_from_extra_loop():
    """The already-indexed active_embedder must be excluded from the extra loop
    (it was already indexed as the primary pass)."""
    source = _read_cli_source()
    loop_marker = (
        "for _extra_idx, _extra_provider in enumerate(_extra_temporal_providers):"
    )
    loop_pos = source.find(loop_marker)
    assert loop_pos != -1

    window = source[max(0, loop_pos - 800) : loop_pos]
    assert "active_embedder" in window, (
        "Story #1290: the extra-embedder set must exclude the already-indexed "
        "active_embedder (analogous to the old _primary_provider exclusion)."
    )
