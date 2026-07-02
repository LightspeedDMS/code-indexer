"""
RED tests for Story #887 Blocker 4 — AC8 public API 2-tuple → 4-tuple break.

The development branch had get_cross_domain_graph() returning a 2-tuple
(edges, anomalies). The current implementation returns a 4-tuple, breaking
any caller that unpacks with exactly 2 variables.

Fix:
  - Restore get_cross_domain_graph() → 2-tuple (edges, anomalies)
  - Add get_cross_domain_graph_with_channels() → 4-tuple
    (edges, anomalies, parser_anomalies, data_anomalies)
  - Update handler to call get_cross_domain_graph_with_channels()

Blocker 4 payload shape: the legacy 2-tuple anomalies list must contain
plain dicts with {file, error} keys — the original public contract —
NOT AnomalyEntry/AnomalyAggregate typed objects.
"""

from pathlib import Path

import pytest

from tests.unit.server.services.test_dep_map_887_fixtures import (
    make_parser,
    make_simple_graph,
)


@pytest.fixture
def _simple_graph_root(tmp_path: Path) -> Path:
    """2-domain graph so get_cross_domain_graph returns non-empty results."""
    (tmp_path / "dependency-map").mkdir()
    make_simple_graph(tmp_path, "dom-a", "dom-b", bidirectional=True)
    return tmp_path


@pytest.fixture
def _one_way_graph_root(tmp_path: Path) -> Path:
    """One-way (non-bidirectional) graph — guaranteed to produce a bidi-mismatch anomaly."""
    (tmp_path / "dependency-map").mkdir()
    make_simple_graph(tmp_path, "dom-a", "dom-b", bidirectional=False)
    return tmp_path


class TestPublicApiTupleArity:
    """Blocker 4: get_cross_domain_graph() must return 2-tuple (legacy contract).
    get_cross_domain_graph_with_channels() must return 4-tuple (new extended API).
    """

    def test_get_cross_domain_graph_returns_two_tuple(
        self, _simple_graph_root: Path
    ) -> None:
        """get_cross_domain_graph() must return exactly 2 elements.

        Before fix: returns 4-tuple, breaking any 2-tuple unpacking callers.
        After fix: returns (edges, anomalies) — the legacy public contract.
        """
        result = make_parser(_simple_graph_root).get_cross_domain_graph()
        assert len(result) == 2, (
            f"get_cross_domain_graph() must return a 2-tuple (edges, anomalies) "
            f"for backward compatibility. Got {len(result)}-tuple. "
            f"Use get_cross_domain_graph_with_channels() for the 4-tuple form."
        )

    def test_get_cross_domain_graph_with_channels_returns_four_tuple(
        self, _simple_graph_root: Path
    ) -> None:
        """get_cross_domain_graph_with_channels() must return exactly 4 elements:
        (edges, anomalies, parser_anomalies, data_anomalies)."""
        result = make_parser(_simple_graph_root).get_cross_domain_graph_with_channels()
        assert len(result) == 4, (
            f"get_cross_domain_graph_with_channels() must return a 4-tuple. "
            f"Got {len(result)}-tuple."
        )


class TestAnomalyShapeInLegacyTuple:
    """Blocker 4 payload shape: anomalies in the legacy 2-tuple must be plain dicts.

    The original public contract of get_cross_domain_graph() returns anomalies as
    List[Dict[str, str]] with {file, error} keys so callers can directly use them
    for JSON serialization. Returning AnomalyEntry/AnomalyAggregate objects breaks
    callers that assume dict access via ["file"] and ["error"].
    """

    def test_get_cross_domain_graph_anomalies_are_dicts_with_file_and_error_keys(
        self, _one_way_graph_root: Path
    ) -> None:
        """get_cross_domain_graph() anomalies must be plain dicts with file+error keys.

        A one-way graph (no incoming confirmation) guarantees at least one anomaly.
        Without the fix, anomalies are AnomalyEntry/AnomalyAggregate objects, not dicts.
        After fix, each anomaly is a dict with exactly 'file' and 'error' string keys.
        """
        _, anomalies = make_parser(_one_way_graph_root).get_cross_domain_graph()

        assert len(anomalies) >= 1, (
            "Expected at least one anomaly from a one-way graph (bidi-mismatch)."
        )
        for i, anomaly in enumerate(anomalies):
            assert isinstance(anomaly, dict), (
                f"anomalies[{i}] must be a plain dict, got {type(anomaly).__name__}: "
                f"{anomaly!r}. Blocker 4: get_cross_domain_graph() must convert "
                f"AnomalyEntry/AnomalyAggregate to {{file, error}} dicts."
            )
            assert "file" in anomaly, (
                f"anomalies[{i}] dict must have 'file' key, got keys={set(anomaly.keys())}"
            )
            assert "error" in anomaly, (
                f"anomalies[{i}] dict must have 'error' key, got keys={set(anomaly.keys())}"
            )
