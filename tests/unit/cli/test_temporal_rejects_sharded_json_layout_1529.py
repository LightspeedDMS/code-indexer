"""Bug #1529 review finding #3: --new-collection-layout=sharded_json must not
be honored for temporal indexing.

Bug #1528's binding rule is that temporal indexing NEVER writes another legacy
`vector_*.json` file -- that file explosion (487,076 files for one real repo)
is the entire reason Epic #1454 exists. But `sharded_json` still defeated it
through two independent doors:

  1. `cli.py`'s temporal branch SKIPPED the pre-index in-place consolidation
     (`if new_collection_layout != "sharded_json":`), so a repo with legacy
     shards kept growing its legacy tree on every incremental run.
  2. `filesystem_vector_store.create_collection` honors an EXPLICIT `False`
     for temporal collections, so brand-new temporal shards were built in the
     legacy layout too.

Since the CLI flag is the only production route to that explicit `False` for
temporal (the server always passes chunks_db; the daemon refuses legacy
shards outright), refusing the combination at the front door closes both
doors at once -- loudly, rather than silently upgrading the request, so an
operator who asked for something impossible is told so.

Semantic indexing is UNAFFECTED: `sharded_json` remains a legitimate,
supported choice there (Story #1488's CLI default is in fact SHARDED_JSON).

The guard is tested as the pure predicate it is, plus a real AST assertion
that the `index` command body actually calls it -- an unwired guard is exactly
the half-wiring class of defect this whole bug is about. The full CLI is
deliberately NOT invoked here: `cidx index` performs a real indexing run.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from code_indexer.cli import index, reject_sharded_json_for_temporal

GUARD_NAME = "reject_sharded_json_for_temporal"


def test_temporal_plus_sharded_json_is_refused() -> None:
    """The impossible combination must raise, naming both halves of it."""
    with pytest.raises(ValueError) as exc_info:
        reject_sharded_json_for_temporal(
            index_commits=True, new_collection_layout="sharded_json"
        )

    message = str(exc_info.value).lower()
    assert "sharded_json" in message
    assert "temporal" in message


@pytest.mark.parametrize(
    ("index_commits", "layout"),
    [
        # Semantic indexing: sharded_json stays fully supported.
        (False, "sharded_json"),
        # Temporal with the consolidated layout, explicitly or by default.
        (True, "chunks_db"),
        (True, None),
        (False, "chunks_db"),
        (False, None),
    ],
)
def test_every_other_combination_is_allowed(index_commits: bool, layout) -> None:
    """Proves the guard is scoped, not a blanket removal of the option."""
    reject_sharded_json_for_temporal(
        index_commits=index_commits, new_collection_layout=layout
    )


def test_index_command_actually_calls_the_guard() -> None:
    """An unwired guard would leave the real production path unprotected."""
    # `index` is a click Command; its Python body is the wrapped callback.
    tree = ast.parse(inspect.getsource(index.callback))

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert GUARD_NAME in called, (
        f"_index_impl does not call {GUARD_NAME}(); the temporal sharded_json "
        "refusal is inert and legacy vector_*.json files can still be written"
    )
