"""Live-server-process embedding-stats writer lifecycle wiring (Story #1418
Phase 3).

Phase 1/2 wired a real writer only for the `cidx index` child-subprocess
path (CrossProcessBootstrapWriter, embedding_stats_child_wiring.py). The
live, long-running cidx-server process itself never installed a real
writer -- every embedding/reranker call instrumented from server-side code
(e.g. server-side query embeddings) silently fell through to the default
NoOpWriter. These two functions close that gap: extracted, independently
testable helpers that lifespan.py calls as thin glue at startup/shutdown,
mirroring how install_embedding_stats_writer_from_bootstrap() is the
testable unit for the child-process side rather than testing the giant
lifespan() function directly.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from code_indexer.server.services.embedding_stats_writer import (
    EmbeddingStatsWriter,
    InProcessAsyncWriter,
)

logger = logging.getLogger(__name__)

# Best-effort final-flush wait on shutdown -- mirrors
# InProcessAsyncWriter.stop()'s own default and other schedulers' shutdown
# timeouts in this codebase (e.g. ActivatedReaperScheduler.stop()).
_STOP_TIMEOUT_SECONDS = 10.0


def start_in_process_embedding_stats_writer(
    backend: Any, config_service: Any
) -> InProcessAsyncWriter:
    """Construct, start, and install the live-server InProcessAsyncWriter.

    Unlike the child-subprocess writer (which resolves flush_interval_seconds
    ONCE at bootstrap -- see embedding_stats_child_wiring.py), this writer
    reads the interval LIVE via a flush_interval_provider closure over
    ``config_service`` -- re-checked on every background-loop cycle, so a
    Web UI change to the setting takes effect without a server restart.

    Args:
        backend: EmbeddingCallStatsSqliteBackend or
            EmbeddingCallStatsPostgresBackend (typically
            backend_registry.embedding_call_stats).
        config_service: Object with get_config() returning a ServerConfig
            exposing embedding_stats_config.flush_interval_seconds.

    Returns:
        The started, installed InProcessAsyncWriter.
    """

    def _live_flush_interval() -> float:
        return config_service.get_config().embedding_stats_config.flush_interval_seconds  # type: ignore[no-any-return]

    writer = InProcessAsyncWriter(backend, flush_interval_provider=_live_flush_interval)
    writer.start()
    EmbeddingStatsWriter.set_active(writer)
    logger.info("Embedding stats writer started (in-process, live-server)")
    return writer


def stop_in_process_embedding_stats_writer(
    writer: Optional[InProcessAsyncWriter],
) -> None:
    """Stop the live-server InProcessAsyncWriter (best-effort final flush).

    No-op when ``writer`` is None (e.g. startup wiring never ran because
    the backend registry lacked embedding_call_stats -- fail-soft startup).
    """
    if writer is None:
        return
    writer.stop(timeout=_STOP_TIMEOUT_SECONDS)
    logger.info("Embedding stats writer stopped (in-process, live-server)")
