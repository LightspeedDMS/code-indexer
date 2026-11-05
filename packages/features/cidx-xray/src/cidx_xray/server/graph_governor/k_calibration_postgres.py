"""PostgreSQL backend for the X-Ray graph-build K calibration store
(Story #1787, S2 amendment, AC16).

Drop-in replacement for `SqliteKCalibrationBackend`
(`k_calibration_store.py`) using psycopg v3 sync connections from a pool --
anything whose `connection()` is a context manager yielding a connection that
commits on a normal exit (the feature's `storage` capability in cluster
mode). Interface-identical (`record_sample`/`get_k`) so a caller can select
whichever backend `storage_mode` implies without branching on its own logic.

Schema (`xray_graph_k_calibration_samples` table and its index) is owned
entirely by the feature's migration 1 (`cidx_xray.server.migrations`, the
SQL in `k_calibration_samples.sql`, formerly the core's migration 051) --
this backend does NOT create or alter any table. The core runs a feature's
migrations while the app is built, before `on_startup` constructs this
backend, so the schema is present by the time an instance exists.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from typing import Any

from .k_calibration_store import KCalibrationSample, _require_valid_sample

logger = logging.getLogger(__name__)


class XrayGraphKCalibrationPostgresBackend:
    """PostgreSQL backend for K calibration samples (cluster deployment).

    Satisfies the SAME `record_sample`/`get_k` interface as
    `SqliteKCalibrationBackend`. All mutations commit immediately after
    executing the DML statement (matching this codebase's other
    Postgres backends' auto-commit-per-call convention).
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    def record_sample(self, sample: KCalibrationSample) -> None:
        """Persists one build's real measurements. Fails OPEN: a
        database write failure is logged as a WARNING and swallowed,
        never raised -- recording data ABOUT a build must never abort
        (or appear to fail) the build itself.
        """
        _require_valid_sample(sample)
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO xray_graph_k_calibration_samples
                        (language, source_bytes, decls, call_sites, candidate_edges, actual_peak_rss, recorded_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        sample.language,
                        sample.source_bytes,
                        sample.decls,
                        sample.call_sites,
                        sample.candidate_edges,
                        sample.actual_peak_rss,
                        time.time(),
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "XrayGraphKCalibrationPostgresBackend.record_sample: write failed (fail-open): %s",
                exc,
            )

    def get_k(self, language: str) -> Optional[float]:
        """Returns the MAX observed `actual_peak_rss / source_bytes`
        multiplier recorded for `language`, or `None` on an invalid
        input, a miss, or a read failure (fail-safe: the caller degrades
        to the conservative default, matching
        `SqliteKCalibrationBackend.get_k`'s contract).
        """
        if not language or not isinstance(language, str):
            return None
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    """
                    SELECT MAX(actual_peak_rss::DOUBLE PRECISION / source_bytes)
                    FROM xray_graph_k_calibration_samples
                    WHERE language = %s AND source_bytes > 0
                    """,
                    (language,),
                ).fetchone()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "XrayGraphKCalibrationPostgresBackend.get_k: read failed (fail-safe None): %s",
                exc,
            )
            return None

        if row is None or row[0] is None:
            return None
        return float(row[0])
