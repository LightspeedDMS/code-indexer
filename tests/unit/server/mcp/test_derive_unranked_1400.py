"""
Tests for Story #1400 Phase 5: derive_unranked().

FINAL LOCKED DESIGN: the reranker's actual outcome is already exposed via
reranker_status.status in the tuple _apply_reranking_sync returns
({"success","failed","skipped","disabled"}) -- no new metadata needed.

unranked = (reranker_status.status != "success")

NEVER derive it from mere presence of rerank_query in context. A terminal
read where rerank was requested but came back disabled/skipped/failed
reports unranked: true (order not trustworthy). Only an actual "success"
outcome reports unranked: false.

TDD: written BEFORE implementation.
"""

import pytest

from code_indexer.server.mcp.reranking import derive_unranked


@pytest.mark.parametrize(
    "status,expected_unranked",
    [
        ("success", False),
        ("failed", True),
        ("skipped", True),
        ("disabled", True),
    ],
)
def test_derive_unranked_from_status(status, expected_unranked):
    rerank_metadata = {"reranker_status": {"status": status}}
    assert derive_unranked(rerank_metadata) is expected_unranked


def test_missing_reranker_status_is_unranked():
    """No reranker_status key at all (rerank never attempted) -> unranked."""
    assert derive_unranked({}) is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
