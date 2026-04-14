"""Cross-process health telemetry bridge for Bug #678.

Provides fire-and-forget write of provider health events to a JSONL file
and atomic drain of those events by the health monitor process.
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

logger = logging.getLogger(__name__)


def read_provider_results(
    repo_path: str, start_mtime: float
) -> Optional[Dict[str, Any]]:
    """Read provider_results.json if present and not stale.

    Bug #679 AC7: Stale-guard helper. Returns None when:
    - The file does not exist.
    - The file's mtime is less than start_mtime (written before subprocess started).
    - The JSON is malformed or not an object at the root.
    - Any IOError/OSError occurs reading the file.

    Args:
        repo_path: Path to the repository root (parent of .code-indexer/).
        start_mtime: Epoch timestamp recorded just before the subprocess was launched.
                     Only files with mtime >= start_mtime are considered fresh.

    Returns:
        Parsed dict from provider_results.json, or None on any failure/staleness.
    """
    results_file = Path(repo_path) / ".code-indexer" / "provider_results.json"
    try:
        if not results_file.exists():
            return None
        file_mtime = results_file.stat().st_mtime
        if file_mtime < start_mtime:
            logger.debug(
                "provider_results.json is stale (mtime=%.3f < start=%.3f) — skipping",
                file_mtime,
                start_mtime,
            )
            return None
        with open(results_file, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            logger.debug("provider_results.json: JSON root is not an object — skipping")
            return None
        return cast(Dict[str, Any], data)
    except json.JSONDecodeError as exc:
        logger.debug("provider_results.json: malformed JSON — %s", exc)
        return None
    except OSError as exc:
        logger.debug("provider_results.json: IO error — %s", exc)
        return None


MAX_HEALTH_FILE_BYTES = 1_048_576  # 1 MB


@dataclass
class HealthEvent:
    """Single provider health measurement."""

    provider: str
    success: bool
    latency_ms: float
    timestamp: float


def _health_file_path(repo_path: str) -> Path:
    """Return the canonical health file path inside the repo .code-indexer dir."""
    base = Path(repo_path).resolve()
    return base / ".code-indexer" / "provider_health.jsonl"


def _truncate_health_file(health_file: Path) -> None:
    """Keep last half of lines when file exceeds MAX_HEALTH_FILE_BYTES.

    Failures are logged at DEBUG level and do not propagate to the caller.
    """
    try:
        text = health_file.read_text(encoding="utf-8", errors="replace")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        keep = lines[len(lines) // 2 :]
        health_file.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.debug("provider_health_bridge: truncation failed: %s", exc)


def write_provider_health_event(
    repo_path: str,
    provider: str,
    success: bool,
    latency_ms: float,
) -> None:
    """Append a JSON line to provider_health.jsonl.

    Fire-and-forget: all exceptions are caught and logged at DEBUG level so
    the caller is never interrupted by telemetry failures.
    """
    try:
        health_file = _health_file_path(repo_path)
        ci_dir = health_file.parent
        if not ci_dir.is_dir():
            logger.debug(
                "provider_health_bridge: .code-indexer dir missing at %s", ci_dir
            )
            return
        if health_file.exists() and health_file.stat().st_size > MAX_HEALTH_FILE_BYTES:
            _truncate_health_file(health_file)
        record = {
            "provider": provider,
            "success": success,
            "latency_ms": latency_ms,
            "timestamp": time.time(),
        }
        line = json.dumps(record) + "\n"
        with open(health_file, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception as exc:  # noqa: BLE001
        logger.warning("provider_health_bridge: write failed: %s", exc)


def drain_and_feed_monitor(repo_path: str) -> None:
    """Drain health events from JSONL file and feed them to ProviderHealthMonitor.

    Fire-and-forget: all exceptions are caught and logged at DEBUG level so the
    caller is never interrupted by telemetry failures.
    """
    try:
        events = drain_provider_health_events(repo_path)
        if not events:
            return
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        monitor = ProviderHealthMonitor.get_instance()
        for event in events:
            monitor.record_call(event.provider, event.latency_ms, event.success)
    except Exception as exc:  # noqa: BLE001
        logger.debug("drain_and_feed_monitor failed (non-fatal): %s", exc)


def drain_provider_health_events(repo_path: str) -> List[HealthEvent]:
    """Atomically move health file, parse events, delete temp file.

    Returns empty list if no file exists or if the atomic move fails.
    Malformed or incomplete JSON lines are skipped and logged at DEBUG level.
    """
    health_file = _health_file_path(repo_path)
    read_file = health_file.parent / ".provider_health.jsonl.read"

    try:
        os.replace(str(health_file), str(read_file))
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.debug("provider_health_bridge: atomic rename failed: %s", exc)
        return []

    events: List[HealthEvent] = []
    try:
        text = read_file.read_text(encoding="utf-8", errors="replace")
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                events.append(
                    HealthEvent(
                        provider=data["provider"],
                        success=data["success"],
                        latency_ms=data["latency_ms"],
                        timestamp=data["timestamp"],
                    )
                )
            except (json.JSONDecodeError, KeyError) as exc:
                logger.debug("provider_health_bridge: skipping bad line: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.debug("provider_health_bridge: read failed: %s", exc)
    finally:
        try:
            read_file.unlink()
        except Exception as exc:  # noqa: BLE001
            logger.debug("provider_health_bridge: cleanup of read file failed: %s", exc)

    return events
