"""Story #1787 AC16: SQLite-backed K calibration store, real DB round trips.

Uses a real temp-file SQLite database via DatabaseConnectionManager (the
same connection-management primitive QueryEmbeddingCacheSqliteBackend
already uses) -- no mocking of the store under test.
"""

from __future__ import annotations

from code_indexer.server.services.xray_graph_governor.k_calibration_store import (
    KCalibrationSample,
    SqliteKCalibrationBackend,
)


def test_record_sample_then_get_k_returns_the_observed_multiplier(tmp_path):
    backend = SqliteKCalibrationBackend(str(tmp_path / "k_calibration.db"))

    backend.record_sample(
        KCalibrationSample(
            language="java",
            source_bytes=1000,
            decls=10,
            call_sites=20,
            candidate_edges=30,
            actual_peak_rss=19000,
        )
    )

    assert backend.get_k("java") == 19.0


def test_multiple_samples_return_the_max_not_the_average():
    import tempfile
    import os

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "k_calibration.db")
        backend = SqliteKCalibrationBackend(db_path)
        backend.record_sample(
            KCalibrationSample(
                language="java",
                source_bytes=1000,
                decls=1,
                call_sites=1,
                candidate_edges=1,
                actual_peak_rss=10500,
            )
        )
        backend.record_sample(
            KCalibrationSample(
                language="java",
                source_bytes=1000,
                decls=1,
                call_sites=1,
                candidate_edges=1,
                actual_peak_rss=19000,
            )
        )

        # Conservative: the WORSE (larger) observed multiplier wins, not
        # the average of 10.5x and 19.0x.
        assert backend.get_k("java") == 19.0


def test_get_k_returns_none_for_a_language_with_no_recorded_samples(tmp_path):
    backend = SqliteKCalibrationBackend(str(tmp_path / "k_calibration.db"))

    assert backend.get_k("cobol") is None


def test_a_fresh_instance_reads_samples_recorded_by_a_different_instance(tmp_path):
    """Proves genuine SQLite persistence -- not an in-process cache --
    matching this project's 'faithful DB' testing convention."""
    db_path = str(tmp_path / "k_calibration.db")
    writer = SqliteKCalibrationBackend(db_path)
    writer.record_sample(
        KCalibrationSample(
            language="java",
            source_bytes=100,
            decls=1,
            call_sites=1,
            candidate_edges=1,
            actual_peak_rss=1900,
        )
    )

    reader = SqliteKCalibrationBackend(db_path)
    assert reader.get_k("java") == 19.0
