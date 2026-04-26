"""
Service-object builder helpers for Story #908 Phase 3.7 tests — Part 1.

Provides make_executor, make_repair_journal, and make_self_loop_anomaly.
make_journal_entry lives in test_dep_map_908_entry_builder.py to stay
within the 3-function operation limit.
"""

import re
from pathlib import Path

_SAFE_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def make_executor(
    *,
    enable_graph_channel_repair: bool = True,
    progress_callback=None,
    journal_callback=None,
):
    """Build a DepMapRepairExecutor with real health detector and index regenerator.

    Raises TypeError/ValueError on invalid arguments before constructing anything.
    """
    if not isinstance(enable_graph_channel_repair, bool):
        raise TypeError(
            f"enable_graph_channel_repair must be bool, got {type(enable_graph_channel_repair).__name__}"
        )
    if progress_callback is not None and not callable(progress_callback):
        raise TypeError("progress_callback must be None or callable")
    if journal_callback is not None and not callable(journal_callback):
        raise TypeError("journal_callback must be None or callable")

    from code_indexer.server.services.dep_map_health_detector import (
        DepMapHealthDetector,
    )
    from code_indexer.server.services.dep_map_index_regenerator import IndexRegenerator
    from code_indexer.server.services.dep_map_repair_executor import (
        DepMapRepairExecutor,
    )

    return DepMapRepairExecutor(
        health_detector=DepMapHealthDetector(),
        index_regenerator=IndexRegenerator(),
        enable_graph_channel_repair=enable_graph_channel_repair,
        progress_callback=progress_callback,
        journal_callback=journal_callback,
    )


def make_repair_journal(journal_path: Path):
    """Instantiate a RepairJournal pointed at journal_path.

    Raises TypeError when journal_path is not a Path instance.
    """
    if not isinstance(journal_path, Path):
        raise TypeError(
            f"journal_path must be a pathlib.Path, got {type(journal_path).__name__}"
        )
    from code_indexer.server.services.dep_map_repair_executor import RepairJournal

    return RepairJournal(journal_path=journal_path)


def make_self_loop_anomaly(domain_name: str):
    """Create a real AnomalyEntry of type SELF_LOOP for the given domain.

    Raises TypeError when domain_name is not a str.
    Raises ValueError when domain_name is empty or contains unsafe characters.
    """
    if not isinstance(domain_name, str):
        raise TypeError(f"domain_name must be str, got {type(domain_name).__name__}")
    if not domain_name:
        raise ValueError("domain_name must not be empty")
    if not _SAFE_DOMAIN_RE.match(domain_name):
        raise ValueError(f"domain_name contains unsafe characters: {domain_name!r}")

    from code_indexer.server.services.dep_map_parser_hygiene import (
        AnomalyEntry,
        AnomalyType,
    )

    return AnomalyEntry(
        type=AnomalyType.SELF_LOOP,
        file=f"{domain_name}.md",
        message=f"self-loop: {domain_name} -> {domain_name}",
        channel="data",
        count=1,
    )
