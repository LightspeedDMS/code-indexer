"""SemanticQueryManager.query_tracker default + set_query_tracker() setter
(Story #1457 AC1/AC2 live wiring, final piece).

Mirrors the EXISTING established `_shard_ownership`/`set_shard_ownership`
pattern exactly (semantic_query_manager.py:315-323): defaults to None in
__init__ (so a manager constructed without one -- every current production
construction site, until lifespan.py is updated -- behaves exactly as
before), with a setter for POST-HOC injection from lifespan.py once the
real server-wide QueryTracker singleton (app.state.query_tracker) exists.

This is the LAST missing piece for Story #1457's discovery-through-pin-
through-dispatch chain to become live: _execute_temporal_query's resolver
construction (test_temporal_resolver_server_wiring_1457.py) is gated on
`getattr(self, "query_tracker", None) is not None` -- so without this
wired, no resolver is ever constructed even when golden_repo_alias is
known, and behavior remains byte-identical to today.
"""

from __future__ import annotations


def test_query_tracker_defaults_to_none(tmp_path):
    from code_indexer.server.query.semantic_query_manager import (
        SemanticQueryManager,
    )

    manager = SemanticQueryManager(data_dir=str(tmp_path))

    assert manager.query_tracker is None


def test_set_query_tracker_injects_real_instance(tmp_path):
    from code_indexer.global_repos.query_tracker import QueryTracker
    from code_indexer.server.query.semantic_query_manager import (
        SemanticQueryManager,
    )

    manager = SemanticQueryManager(data_dir=str(tmp_path))
    real_tracker = QueryTracker()

    manager.set_query_tracker(real_tracker)

    assert manager.query_tracker is real_tracker
