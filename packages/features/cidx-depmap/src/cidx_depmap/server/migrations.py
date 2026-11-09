"""The ``depmap`` feature's schema migrations (``migrations()``).

The dependency map's tables -- ``dependency_map_tracking`` (PostgreSQL: core
migration 001; SQLite: the core's database initialisation),
``dependency_map_run_history`` (PostgreSQL: core migration
``021_dependency_map_run_history.sql``; SQLite: the core's tracking backend)
and ``dependency_map_dashboard_cache`` (the core's dashboard cache backend)
-- are created by the core, which keeps them: the storage backends that read
and write them, the data-retention cleanup of the run history and the
SQLite-to-PostgreSQL data migration stay in the core
(``docs/architecture/cidx-depmap.md``). Version 1 therefore imports that
state: it runs no DDL on either database and the runner records version 1.

Removing the feature leaves the tables, inert.
"""

from __future__ import annotations

from typing import Any, Sequence

from cidx_sdk import Migration

#: The core migrations whose tables version 1 adopts.
ADOPTED_CORE_MIGRATIONS = (
    "001_initial_schema.sql",
    "021_dependency_map_run_history.sql",
)


def _adopt_core_tables(connection: Any) -> None:
    """Version 1: the tables exist through the core (see the module
    docstring); nothing to create."""
    return None


DEPMAP_MIGRATIONS: Sequence[Migration] = (
    Migration(
        version=1,
        description="dependency-map tables (core migrations 001, 021; SQLite "
        "initialisation) adopted",
        apply=_adopt_core_tables,
    ),
)
