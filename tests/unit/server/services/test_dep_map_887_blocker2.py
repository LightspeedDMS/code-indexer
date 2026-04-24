"""
RED tests for Story #887 Blocker 2 — Late-anomaly silent drop (Rule 13 violation).

finalize_graph_edges() emits GARBAGE_DOMAIN_REJECTED anomalies for edges with
empty types. These are appended AFTER _build_graph_anomalies() was called in
the original pipeline ordering, so they were silently dropped.

Fix: reorder pipeline so finalize_graph_edges() runs BEFORE _build_graph_anomalies().
"""

import json
from pathlib import Path
from typing import List, Tuple

import pytest

from tests.unit.server.services.test_dep_map_887_fixtures import (
    import_hygiene_symbol,
    make_parser,
)


@pytest.fixture
def AnomalyType():
    return import_hygiene_symbol("AnomalyType")


def _write_empty_types_fixture(dep_map_dir: Path) -> None:
    """Write a domain file with an outgoing row that has an empty dep_type cell.

    Produces an edge bucket with types=set() so finalize_graph_edges() emits a
    GARBAGE_DOMAIN_REJECTED anomaly instead of including the edge.
    """
    domains = [
        {"name": "src-domain", "description": "d", "participating_repos": []},
        {"name": "tgt-domain", "description": "d", "participating_repos": []},
    ]
    (dep_map_dir / "_domains.json").write_text(json.dumps(domains), encoding="utf-8")

    src_content = (
        "---\nname: src-domain\n---\n"
        "## Cross-Domain Connections\n\n"
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        "| repo-a | repo-b | tgt-domain |  | why | ev |\n\n"
        "### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
    )
    (dep_map_dir / "src-domain.md").write_text(src_content, encoding="utf-8")
    (dep_map_dir / "tgt-domain.md").write_text(
        "---\nname: tgt-domain\n---\n"
        "## Cross-Domain Connections\n\n"
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n\n"
        "### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n",
        encoding="utf-8",
    )


def _find_garbage(anomalies: List, AnomalyType) -> List:
    """Return entries from *anomalies* whose type is GARBAGE_DOMAIN_REJECTED."""
    return [a for a in anomalies if a.type == AnomalyType.GARBAGE_DOMAIN_REJECTED]


@pytest.fixture
def _empty_types_graph_result(tmp_path: Path, AnomalyType) -> Tuple:
    """Prepare the empty-types fixture and run get_cross_domain_graph().

    Returns the full 4-tuple (edges, all_anomalies, parser_anomalies, data_anomalies).
    """
    dep_map_dir = tmp_path / "dependency-map"
    dep_map_dir.mkdir()
    _write_empty_types_fixture(dep_map_dir)
    return make_parser(tmp_path).get_cross_domain_graph_with_channels()


class TestLateAnomalySilentDrop:
    """Blocker 2: anomalies emitted by finalize_graph_edges() must reach the response.

    finalize_graph_edges() appends GARBAGE_DOMAIN_REJECTED for edges with empty
    types. Without the fix (wrong call order), these anomalies are emitted after
    _build_graph_anomalies() has already deduped and split, so they never appear
    in data_anomalies[] or all_anomalies.
    """

    def test_garbage_domain_rejected_from_finalize_reaches_data_anomalies(
        self, _empty_types_graph_result: Tuple, AnomalyType
    ) -> None:
        """An edge with empty types causes finalize_graph_edges() to emit
        GARBAGE_DOMAIN_REJECTED. That anomaly MUST appear in data_anomalies[].

        Without the fix this fails because finalize runs after _build_graph_anomalies.
        """
        _, all_anomalies, _, data_anomalies = _empty_types_graph_result
        garbage = _find_garbage(data_anomalies, AnomalyType)
        assert len(garbage) >= 1, (
            f"GARBAGE_DOMAIN_REJECTED emitted by finalize_graph_edges() must appear "
            f"in data_anomalies[], but got 0. "
            f"data_anomalies={data_anomalies}, all_anomalies={all_anomalies}. "
            f"Blocker 2: finalize runs AFTER _build_graph_anomalies so late "
            f"anomalies are silently dropped."
        )

    def test_garbage_domain_rejected_from_finalize_reaches_all_anomalies_union(
        self, _empty_types_graph_result: Tuple, AnomalyType
    ) -> None:
        """The GARBAGE_DOMAIN_REJECTED from finalize must also appear in the
        all_anomalies union (legacy field), not only in data_anomalies."""
        _, all_anomalies, _, _ = _empty_types_graph_result
        garbage = _find_garbage(all_anomalies, AnomalyType)
        assert len(garbage) >= 1, (
            f"GARBAGE_DOMAIN_REJECTED from finalize must appear in all_anomalies union. "
            f"Got all_anomalies={all_anomalies}"
        )


class TestAnomalyToDictHandlesAggregates:
    """NEW-1 (ITEM C): _anomaly_to_dict must handle AnomalyAggregate without crashing.

    The handler's _anomaly_to_dict assumes AnomalyEntry with a .file attribute.
    When AnomalyAggregate appears (threshold crossed), calling _anomaly_to_dict
    raises AttributeError: 'AnomalyAggregate' object has no attribute 'file'.

    Fix: teach _anomaly_to_dict to handle both AnomalyEntry and AnomalyAggregate.
    """

    def test_anomaly_to_dict_does_not_crash_on_aggregate(self) -> None:
        """_anomaly_to_dict called with AnomalyAggregate must return a dict with
        'file' and 'error' keys, not raise AttributeError.

        Without the fix: AttributeError because AnomalyAggregate has no .file attribute.
        After the fix: returns a well-formed dict suitable for JSON serialization.
        """
        from code_indexer.server.mcp.handlers.depmap import _anomaly_to_dict
        from code_indexer.server.services.dep_map_parser_hygiene import (
            AnomalyAggregate,
            AnomalyEntry,
            AnomalyType,
        )

        example = AnomalyEntry(
            type=AnomalyType.GARBAGE_DOMAIN_REJECTED,
            file="some/domain.md",
            message="prose-fragment target domain rejected: 'some long prose text'",
            channel="data",
        )
        aggregate = AnomalyAggregate(
            type=AnomalyType.GARBAGE_DOMAIN_REJECTED,
            count=10,
            examples=[example],
        )

        # Must not raise AttributeError
        result = _anomaly_to_dict(aggregate)

        assert isinstance(result, dict), (
            f"_anomaly_to_dict(AnomalyAggregate) must return a dict, got {type(result)}"
        )
        assert "file" in result, (
            f"Result dict must have 'file' key, got keys={set(result.keys())}"
        )
        assert "error" in result, (
            f"Result dict must have 'error' key, got keys={set(result.keys())}"
        )
