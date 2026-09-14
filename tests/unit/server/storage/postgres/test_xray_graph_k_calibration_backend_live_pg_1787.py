"""Story #1787 AC16: live-PostgreSQL round-trip tests for
XrayGraphKCalibrationPostgresBackend.

Mirrors test_cleanup_pending_deletion_state_live_pg_1567.py's exact
pattern: gated by TEST_POSTGRES_DSN, skips cleanly when no PostgreSQL is
available (this project's existing posture -- these tests are not run
in CI, only locally against a real instance), creates the real
xray_graph_k_calibration_samples table matching migration 051
(051_xray_graph_k_calibration_samples.sql) exactly, drops it afterward.

Per the project's "faithful DB mocks" lesson, this exercises a REAL
psycopg v3 connection -- not a mock -- so a silent no-op write cannot be
mistaken for a passing test.
"""

import os
from contextlib import contextmanager

import pytest

HAS_PSYCOPG_FOR_LIVE_PG = False
try:
    import psycopg
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
    from code_indexer.server.storage.postgres.xray_graph_k_calibration_backend import (
        XrayGraphKCalibrationPostgresBackend,
    )
    from code_indexer.server.services.xray_graph_governor.k_calibration_store import (
        KCalibrationSample,
    )

    HAS_PSYCOPG_FOR_LIVE_PG = True
except ImportError:
    pass


@contextmanager
def _backend(dsn: str, name: str):
    """Open a fresh ConnectionPool + XrayGraphKCalibrationPostgresBackend
    against *dsn*, closing the pool afterward. A FRESH pool/backend per
    call (never shared across writer/reader in a test) proves state is
    genuinely persisted server-side, not cached in-process."""
    pool = ConnectionPool(dsn, name=name)
    try:
        yield XrayGraphKCalibrationPostgresBackend(pool)
    finally:
        pool.close()


@pytest.fixture(scope="module")
def pg_dsn_for_k_calibration():
    """Module-scoped DSN string for live-PG K-calibration tests. Skips
    if unavailable (matches pg_dsn_for_cleanup_pending_deletion_state in
    test_cleanup_pending_deletion_state_live_pg_1567.py)."""
    if not HAS_PSYCOPG_FOR_LIVE_PG:
        pytest.skip("psycopg not available")
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    try:
        with psycopg.connect(dsn) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    return dsn


@pytest.fixture()
def k_calibration_table(pg_dsn_for_k_calibration):
    """Create a real xray_graph_k_calibration_samples table (matching
    051_xray_graph_k_calibration_samples.sql exactly) before each test,
    dropped after, for isolation from any other schema/table that may
    exist on the target DB."""
    dsn = pg_dsn_for_k_calibration
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS xray_graph_k_calibration_samples")
        conn.execute(
            """
            CREATE TABLE xray_graph_k_calibration_samples (
                id               BIGSERIAL PRIMARY KEY,
                language         TEXT             NOT NULL,
                source_bytes     BIGINT           NOT NULL,
                decls            BIGINT           NOT NULL,
                call_sites       BIGINT           NOT NULL,
                candidate_edges  BIGINT           NOT NULL,
                actual_peak_rss  BIGINT           NOT NULL,
                recorded_at      DOUBLE PRECISION NOT NULL
            )
            """
        )
    yield dsn
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS xray_graph_k_calibration_samples")


pytestmark = pytest.mark.skipif(
    not HAS_PSYCOPG_FOR_LIVE_PG, reason="psycopg not available"
)


def _sample(actual_peak_rss: int) -> "KCalibrationSample":
    return KCalibrationSample(
        language="java",
        source_bytes=1000,
        decls=1,
        call_sites=1,
        candidate_edges=1,
        actual_peak_rss=actual_peak_rss,
    )


def test_record_sample_then_get_k_round_trips_through_a_new_instance_live(
    k_calibration_table,
):
    dsn = k_calibration_table

    with _backend(dsn, "story1787-live-write") as write_backend:
        write_backend.record_sample(_sample(19000))

    with _backend(dsn, "story1787-live-read") as read_backend:
        assert read_backend.get_k("java") == 19.0


def test_multiple_samples_return_the_max_not_the_average_live(k_calibration_table):
    dsn = k_calibration_table

    with _backend(dsn, "story1787-live-multi") as backend:
        backend.record_sample(_sample(10500))
        backend.record_sample(_sample(19000))
        assert backend.get_k("java") == 19.0


def test_get_k_returns_none_for_a_language_with_no_recorded_samples_live(
    k_calibration_table,
):
    dsn = k_calibration_table

    with _backend(dsn, "story1787-live-miss") as backend:
        assert backend.get_k("cobol") is None
