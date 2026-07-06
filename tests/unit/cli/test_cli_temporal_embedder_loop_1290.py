"""Story #1290/#1291: the CLI's PRIMARY temporal collection naming must use
config.temporal.active_embedder, NOT resolve_temporal_collection_from_config()
(the regular semantic-search provider/model scheme).

Story #1291 code-review Finding 1 REMOVED the "additional-temporal-embedder
loop" this file used to guard: TemporalIndexer.index_commits() now natively
builds shard sets for EVERY embedder configured in temporal.embedders in a
SINGLE call (see index_commits()'s per-embedder loop, AC1/AC4/AC5/AC10), so a
second, CLI-level per-embedder loop was redundant and double-ran indexing --
crashing AC4 whenever a non-active embedder was unavailable. That loop, and
the two tests that pinned its presence
(test_extra_temporal_providers_sourced_from_temporal_embedders_config and
test_active_embedder_excluded_from_extra_loop), have been removed. See
tests/unit/cli/test_cli_no_redundant_embedder_loop_1291.py for the behavioral
regression guard proving the CLI no longer double-runs indexing.

The one remaining test below (primary TemporalIndexer collection naming) is
unrelated to the removed loop and stays valid.
"""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLI_PATH = _REPO_ROOT / "src" / "code_indexer" / "cli.py"


def _read_cli_source() -> str:
    return _CLI_PATH.read_text()


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
