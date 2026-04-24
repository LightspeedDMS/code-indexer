"""
RED tests for Story #887 AC6 — Anomaly Deduplication by Key.

deduplicate_anomalies() key=(type, file, message); frozenset bidi mismatch fix.
AnomalyEntry always has a .type attribute — no hasattr guards needed.
"""

from pathlib import Path

import pytest

from tests.unit.server.services.test_dep_map_887_fixtures import (
    import_hygiene_symbol,
    make_parser,
    make_simple_graph,
)

EXPECTED_ONE = 1
EXPECTED_TWO = 2


@pytest.fixture
def dep_map_root(tmp_path: Path) -> Path:
    (tmp_path / "dependency-map").mkdir()
    return tmp_path


@pytest.fixture
def AnomalyType():
    return import_hygiene_symbol("AnomalyType")


@pytest.fixture
def AnomalyEntry():
    return import_hygiene_symbol("AnomalyEntry")


@pytest.fixture
def dedup_fn():
    return import_hygiene_symbol("deduplicate_anomalies")


def _bidi_entry(AnomalyType, AnomalyEntry, message: str):
    return AnomalyEntry(
        type=AnomalyType.BIDIRECTIONAL_MISMATCH,
        file="domain.md",
        message=message,
        channel="data",
    )


class TestDeduplicatePureFunction:
    """Key-based dedup: identical (type, file, message) collapses; distinct keys do not."""

    def test_identical_key_collapses_to_count_two(
        self, AnomalyType, AnomalyEntry, dedup_fn
    ) -> None:
        """Two entries with same (type, file, message) → 1 entry, count=2."""
        entries = [
            _bidi_entry(AnomalyType, AnomalyEntry, "mismatch A→B"),
            _bidi_entry(AnomalyType, AnomalyEntry, "mismatch A→B"),
        ]
        result = dedup_fn(entries)

        assert len(result) == EXPECTED_ONE
        assert result[0].count == EXPECTED_TWO

    def test_different_messages_not_collapsed(
        self, AnomalyType, AnomalyEntry, dedup_fn
    ) -> None:
        """Same type+file but different message → 2 separate entries."""
        entries = [
            _bidi_entry(AnomalyType, AnomalyEntry, "msg-A"),
            _bidi_entry(AnomalyType, AnomalyEntry, "msg-B"),
        ]
        result = dedup_fn(entries)

        assert len(result) == EXPECTED_TWO

    def test_unique_entry_has_count_one(
        self, AnomalyType, AnomalyEntry, dedup_fn
    ) -> None:
        """Single entry of any type → count=1 (dedup key is type-independent)."""
        entries = [
            AnomalyEntry(
                type=AnomalyType.SELF_LOOP,
                file="f.md",
                message="loop",
                channel="data",
            )
        ]
        result = dedup_fn(entries)

        assert len(result) == EXPECTED_ONE
        assert result[0].count == EXPECTED_ONE
        assert result[0].type == AnomalyType.SELF_LOOP


class TestBidirectionalFrozensetDedup:
    """Frozenset-keyed aggregation fix: symmetric mismatch pair emits exactly 1 anomaly."""

    def test_bidi_mismatch_emits_exactly_one_data_anomaly(
        self, AnomalyType, dep_map_root: Path
    ) -> None:
        """src→tgt outgoing with no tgt incoming → exactly 1 BIDIRECTIONAL_MISMATCH.

        Before fix: direction-1 (outgoing not confirmed by incoming) and
        direction-2 (incoming not confirmed by outgoing) both emit — 2 anomalies
        per logical mismatch. After frozenset fix: symmetric pair collapses to 1.
        """
        make_simple_graph(dep_map_root, "dom-x", "dom-y", bidirectional=False)

        _, _, _, data_anomalies = make_parser(
            dep_map_root
        ).get_cross_domain_graph_with_channels()

        # AnomalyEntry always has .type — no hasattr guard needed
        bidi = [
            a for a in data_anomalies if a.type == AnomalyType.BIDIRECTIONAL_MISMATCH
        ]
        assert len(bidi) == EXPECTED_ONE, (
            f"Expected exactly 1 BIDIRECTIONAL_MISMATCH, got {len(bidi)}: {bidi}"
        )
