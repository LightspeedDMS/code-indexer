"""The dependency-map repair writes cidx-meta (domain files, _domains.json,
_index.md) under catalog admission as the dependency map.

Both repair doors -- the Web repair route and the scheduled auto-repair --
run through ``repair_under_catalog_admission``: it detects the map's health
and repairs it while holding the cidx-meta write lock as the dependency map
(or under a cooperative holder), refuses before writing anything while an
exclusive owner (write mode, the catalog backup) holds it, and requests the
catalog refresh once the repair is done.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Set

if TYPE_CHECKING:
    from code_indexer.server.services.dep_map_repair_executor import (
        DepMapRepairExecutor,
        RepairResult,
    )


def _detect_and_repair(
    executor: "DepMapRepairExecutor", output_dir: Path, known_repos: Optional[Set[str]]
) -> "RepairResult":
    from code_indexer.server.services.dep_map_health_detector import (
        DepMapHealthDetector,
    )

    health_report = DepMapHealthDetector().detect(output_dir, known_repos=known_repos)
    return executor.execute(output_dir, health_report)


def repair_under_catalog_admission(
    dep_map_service: Any,
    executor: "DepMapRepairExecutor",
    output_dir: Path,
    known_repos: Optional[Set[str]],
) -> "RepairResult":
    """Detect and repair the dependency map under catalog admission as the
    dependency map. Raises CatalogLockedExclusively, before anything is
    written, while an exclusive owner holds the catalog."""
    from code_indexer.global_repos.catalog_lock_holder import admitted_catalog_write
    from code_indexer.global_repos.meta_description_hook import (
        refresh_after_catalog_write,
    )
    from code_indexer.server.services.dependency_map_service import (
        DEPENDENCY_MAP_LOCK_OWNER,
    )

    refresh_scheduler = getattr(dep_map_service, "_refresh_scheduler", None)
    if refresh_scheduler is None:
        # ^ why same rule as the dependency-map analyses: with no refresh scheduler wired nothing publishes the catalog, so there is no lock to coordinate
        return _detect_and_repair(executor, output_dir, known_repos)
    admitted: Optional[bool] = None
    try:
        with admitted_catalog_write(
            refresh_scheduler, DEPENDENCY_MAP_LOCK_OWNER
        ) as held:
            admitted = held
            return _detect_and_repair(executor, output_dir, known_repos)
    finally:
        if admitted is not None:
            refresh_after_catalog_write(refresh_scheduler, lock_still_held=not admitted)
