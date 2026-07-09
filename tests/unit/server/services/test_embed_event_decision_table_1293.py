"""Tests for the Story #1293 (path x outcome) -> (role, live_batch_id) decision
table and its S1a-reachable classifier function.

The full DECISION_TABLE documents all 11 rows from the Story #1293 Algorithm 2
spec (coalescer owner/joiner rows included, even though their runtime wiring is
S1b). decide_role_and_outcome() is the S1a-reachable pure classifier used by
the shared emit helper at the two inline (non-coalesced) call sites.
"""

import pytest


# ---------------------------------------------------------------------------
# Full reference table -- all 11 rows (documentation-as-code, per Testing
# Requirements: "Unit tests covering: the decision table (all rows)").
# ---------------------------------------------------------------------------


class TestDecisionTableAllRows:
    @pytest.mark.parametrize(
        "path_key,expected",
        [
            ("coalescer_owner_cold", ("miss", "owner", "new")),
            ("coalescer_joiner", ("hit", "joiner", "owner")),
            ("warm_hit", ("hit", "warm_hit", None)),
            ("direct_live", ("miss", "direct", None)),
            ("direct_hit", ("hit", "warm_hit", None)),
            ("coalesced_shadow_live", ("shadow_miss", "owner", "new")),
            ("direct_shadow_live", ("shadow_miss", "direct", None)),
            ("shadow_hit", ("shadow_hit", "warm_hit", None)),
            ("bypass", ("bypass", "direct", None)),
            ("failover_primary_fail", ("error", "direct", None)),
            ("failover_secondary_ok", ("miss", "direct", None)),
        ],
    )
    def test_row(self, path_key, expected):
        from code_indexer.server.services.embed_event_decision_table import (
            DECISION_TABLE,
        )

        assert DECISION_TABLE[path_key] == expected

    def test_table_has_exactly_11_rows(self):
        from code_indexer.server.services.embed_event_decision_table import (
            DECISION_TABLE,
        )

        assert len(DECISION_TABLE) == 11

    def test_live_batch_id_kind_only_new_or_owner_or_none(self):
        """live_batch_id_kind must be one of the 3 documented values."""
        from code_indexer.server.services.embed_event_decision_table import (
            DECISION_TABLE,
        )

        for _outcome, _role, kind in DECISION_TABLE.values():
            assert kind in ("new", "owner", None)


# ---------------------------------------------------------------------------
# decide_role_and_outcome -- S1a-reachable classifier (direct / non-coalesced
# rows only). Driven purely by cache_hit / cache_mode / bypass / error, no
# coalescer awareness required.
# ---------------------------------------------------------------------------


class TestDecideRoleAndOutcome:
    def test_direct_live_call(self):
        """No cache consulted at all (cache_hit=None) -> miss/direct."""
        from code_indexer.server.services.embed_event_decision_table import (
            decide_role_and_outcome,
        )

        outcome, role = decide_role_and_outcome(cache_hit=None, cache_mode=None)
        assert (outcome, role) == ("miss", "direct")

    def test_direct_live_call_on_mode_miss(self):
        from code_indexer.server.services.embed_event_decision_table import (
            decide_role_and_outcome,
        )

        outcome, role = decide_role_and_outcome(cache_hit=False, cache_mode="on")
        assert (outcome, role) == ("miss", "direct")

    def test_direct_cache_hit(self):
        from code_indexer.server.services.embed_event_decision_table import (
            decide_role_and_outcome,
        )

        outcome, role = decide_role_and_outcome(cache_hit=True, cache_mode="on")
        assert (outcome, role) == ("hit", "warm_hit")

    def test_direct_shadow_live(self):
        from code_indexer.server.services.embed_event_decision_table import (
            decide_role_and_outcome,
        )

        outcome, role = decide_role_and_outcome(cache_hit=False, cache_mode="shadow")
        assert (outcome, role) == ("shadow_miss", "direct")

    def test_shadow_hit(self):
        from code_indexer.server.services.embed_event_decision_table import (
            decide_role_and_outcome,
        )

        outcome, role = decide_role_and_outcome(cache_hit=True, cache_mode="shadow")
        assert (outcome, role) == ("shadow_hit", "warm_hit")

    def test_bypass(self):
        from code_indexer.server.services.embed_event_decision_table import (
            decide_role_and_outcome,
        )

        outcome, role = decide_role_and_outcome(
            cache_hit=False, cache_mode=None, bypass=True
        )
        assert (outcome, role) == ("bypass", "direct")

    def test_error_takes_precedence_over_everything(self):
        from code_indexer.server.services.embed_event_decision_table import (
            decide_role_and_outcome,
        )

        outcome, role = decide_role_and_outcome(
            cache_hit=True, cache_mode="on", bypass=True, error=True
        )
        assert (outcome, role) == ("error", "direct")
