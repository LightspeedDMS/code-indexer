"""Per-provider temporal indexing loop for Bug #679.

Encapsulates the multi-provider temporal indexing loop so it can be
unit tested independently from the CLI command handler.

Provides:
    ProviderResult                   — per-provider outcome data
    run_extra_provider_temporal_loop — iterate extra providers with isolation
    write_provider_results_atomic    — atomic JSON write via tempfile + os.replace
    compute_exit_code                — 0 / 1 / 2 semantics per AC4
"""

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ProviderResult:
    """Outcome of a single provider's temporal indexing pass."""

    status: str  # "success" | "failed" | "skipped"
    error: Optional[str] = None
    latency_seconds: float = 0.0
    files_indexed: int = 0
    chunks_indexed: int = 0


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _health_check_provider(
    embedding_factory: Any, config: Any, provider_name: str
) -> bool:
    """Return True if the provider passes its health check, False otherwise."""
    embedding = embedding_factory.create(config, provider_name=provider_name)
    return bool(embedding.health_check())


def _index_one_provider(
    indexer_factory: Callable,
    config_manager: Any,
    vector_store: Any,
    collection_name: str,
    all_branches: bool,
    max_commits: Optional[int],
    since_date: Optional[str],
    progress_callback: Optional[Callable],
    reconcile: bool,
) -> Tuple[Any, Any]:
    """Create an indexer, run index_commits(), return (indexer, indexing_result).

    The caller is responsible for calling indexer.close() in a finally block.
    """
    indexer = indexer_factory(config_manager, vector_store, collection_name)
    result = indexer.index_commits(
        all_branches=all_branches,
        max_commits=max_commits,
        since_date=since_date,
        progress_callback=progress_callback,
        reconcile=reconcile,
    )
    return indexer, result


def _record_failure_and_mark_metadata(
    indexer: Any,
    provider_name: str,
    exc: Exception,
    latency: float,
) -> ProviderResult:
    """Record failure and set TemporalProgressiveMetadata state to 'failed'."""
    if indexer is not None:
        try:
            from code_indexer.services.temporal.temporal_progressive_metadata import (
                TemporalProgressiveMetadata,
            )

            TemporalProgressiveMetadata(indexer.temporal_dir).set_state("failed")
        except Exception as meta_exc:
            logger.debug(
                "Bug #679: could not set metadata state for %s: %s",
                provider_name,
                meta_exc,
            )
    return ProviderResult(
        status="failed",
        error=str(exc),
        latency_seconds=latency,
        files_indexed=0,
        chunks_indexed=0,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_extra_provider_temporal_loop(
    extra_providers: List[str],
    config: Any,
    config_manager: Any,
    vector_store: Any,
    indexer_factory: Callable,
    embedding_factory: Any,
    resolve_collection_fn: Callable[[Any], str],
    progress_callback: Optional[Callable],
    all_branches: bool,
    max_commits: Optional[int],
    since_date: Optional[str],
    reconcile: bool,
    repo_path: str,
) -> Dict[str, ProviderResult]:
    """Iterate extra providers with per-provider exception isolation (Bug #679 AC2).

    For each provider: health-check, index, record result. On exception: log
    error, mark metadata failed, continue to next provider. Writes
    provider_results.json atomically when done.

    Returns:
        Dict mapping provider_name -> ProviderResult for every provider attempted.
    """
    results: Dict[str, ProviderResult] = {}

    for provider_name in extra_providers:
        start_time = time.monotonic()
        _orig_provider = config.embedding_provider
        indexer = None
        try:
            if not _health_check_provider(embedding_factory, config, provider_name):
                logger.warning(
                    "Bug #679: %s health check failed — skipping", provider_name
                )
                results[provider_name] = ProviderResult(status="skipped")
                continue

            config.embedding_provider = provider_name
            config_manager._config = config
            collection_name = resolve_collection_fn(config)
            indexer = indexer_factory(config_manager, vector_store, collection_name)
            indexing_result = indexer.index_commits(
                all_branches=all_branches,
                max_commits=max_commits,
                since_date=since_date,
                progress_callback=progress_callback,
                reconcile=reconcile,
            )
            latency = time.monotonic() - start_time
            results[provider_name] = ProviderResult(
                status="success",
                latency_seconds=latency,
                files_indexed=getattr(indexing_result, "files_processed", 0),
                chunks_indexed=getattr(
                    indexing_result, "approximate_vectors_created", 0
                ),
            )
            logger.info("Bug #679: %s succeeded in %.1fs", provider_name, latency)

        except Exception as exc:
            latency = time.monotonic() - start_time
            logger.error(
                "Bug #679: %s failed after %.1fs", provider_name, latency, exc_info=True
            )
            results[provider_name] = _record_failure_and_mark_metadata(
                indexer, provider_name, exc, latency
            )
        finally:
            if indexer is not None:
                try:
                    indexer.close()
                except Exception as close_exc:
                    logger.debug(
                        "Bug #679: indexer.close() failed for %s: %s",
                        provider_name,
                        close_exc,
                    )
            config.embedding_provider = _orig_provider
            config_manager._config = config

    write_provider_results_atomic(repo_path, results)
    return results


def write_provider_results_atomic(
    repo_path: str,
    results: Dict[str, ProviderResult],
) -> None:
    """Write provider_results.json atomically via tempfile + os.replace (AC3).

    Args:
        repo_path: Repository root. Writes to <repo_path>/.code-indexer/provider_results.json.
        results: Mapping of provider_name -> ProviderResult.
    """
    ci_dir = Path(repo_path) / ".code-indexer"
    ci_dir.mkdir(parents=True, exist_ok=True)
    target = ci_dir / "provider_results.json"

    payload: Dict[str, Any] = {
        "provider_results": {
            name: {
                "status": r.status,
                "error": r.error,
                "latency_seconds": r.latency_seconds,
                "files_indexed": r.files_indexed,
                "chunks_indexed": r.chunks_indexed,
            }
            for name, r in results.items()
        }
    }

    fd, tmp_path = tempfile.mkstemp(dir=str(ci_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp_path, str(target))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def compute_exit_code(results: Dict[str, ProviderResult]) -> int:
    """Compute CLI exit code from per-provider results (AC4).

    Semantics:
        0 — all active providers succeeded (or no providers ran)
        1 — all active providers failed
        2 — partial: some succeeded, some failed

    Skipped providers (health-check failure) are excluded from the tally.
    """
    active = {name: r for name, r in results.items() if r.status != "skipped"}
    if not active:
        return 0
    successes = sum(1 for r in active.values() if r.status == "success")
    failures = sum(1 for r in active.values() if r.status == "failed")
    if failures == 0:
        return 0
    if successes == 0:
        return 1
    return 2
