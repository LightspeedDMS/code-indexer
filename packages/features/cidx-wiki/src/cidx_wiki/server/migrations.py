"""The ``wiki`` feature's schema migrations (``migrations()``).

The wiki's tables -- ``wiki_cache``, ``wiki_sidebar_cache`` (PostgreSQL:
core migration 001, the initial schema) and ``wiki_article_views`` (core
migration ``024_wiki_article_views.sql``) -- were created by the core before
the feature owned them, and the core keeps creating them: 001 holds the
whole initial schema, and the core's SQLite-to-PostgreSQL data migration
copies the three tables. Version 1 therefore imports that state:

=========================================  ===================================
Database                                   What version 1 does
=========================================  ===================================
PostgreSQL, core 024 applied               no DDL; the runner records version
                                           1 (the ledger entry is imported)
PostgreSQL, 024 not applied                ``wiki_article_views`` as 024
                                           creates it (``IF NOT EXISTS``)
SQLite (solo)                              the three tables in the server
                                           database, as ``WikiCache`` creates
                                           them (``IF NOT EXISTS``)
=========================================  ===================================

Backward compatible, as every migration (rolling restarts share a schema):
``CREATE ... IF NOT EXISTS`` only. Removing the feature leaves the tables.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Sequence

from cidx_sdk import Migration

from cidx_wiki.server.wiki_cache import (
    _CREATE_WIKI_ARTICLE_VIEWS_TABLE,
    _CREATE_WIKI_CACHE_TABLE,
    _CREATE_WIKI_SIDEBAR_CACHE_TABLE,
)

#: The core migration version 1 imports.
CORE_MIGRATION_024 = "024_wiki_article_views.sql"

#: The PostgreSQL DDL of core migration 024, unchanged.
_PG_ARTICLE_VIEWS_DDL = """
CREATE TABLE IF NOT EXISTS wiki_article_views (
    repo_alias TEXT NOT NULL,
    article_path TEXT NOT NULL,
    real_views INTEGER DEFAULT 0,
    first_viewed_at TIMESTAMP WITH TIME ZONE,
    last_viewed_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (repo_alias, article_path)
)
"""

_LEDGER_EXISTS_PG = "SELECT to_regclass('schema_migrations') IS NOT NULL"
_CORE_MIGRATION_APPLIED_PG = "SELECT 1 FROM schema_migrations WHERE filename = %s"


def core_migration_024_applied(connection: Any) -> bool:
    """Whether the core's migration runner applied 024 on this PostgreSQL
    database (its ``schema_migrations`` ledger has the file)."""
    with connection.cursor() as cur:
        cur.execute(_LEDGER_EXISTS_PG)
        row = cur.fetchone()
        if not row or not row[0]:
            return False
        cur.execute(_CORE_MIGRATION_APPLIED_PG, (CORE_MIGRATION_024,))
        return cur.fetchone() is not None


def _import_wiki_tables(connection: Any) -> None:
    if isinstance(connection, sqlite3.Connection):
        for statement in (
            _CREATE_WIKI_CACHE_TABLE,
            _CREATE_WIKI_SIDEBAR_CACHE_TABLE,
            _CREATE_WIKI_ARTICLE_VIEWS_TABLE,
        ):
            connection.execute(statement)
        return
    if core_migration_024_applied(connection):
        # Already created by the core (001 and 024): import that state (the
        # runner records version 1) and run nothing.
        return
    with connection.cursor() as cur:
        cur.execute(_PG_ARTICLE_VIEWS_DDL)


WIKI_MIGRATIONS: Sequence[Migration] = (
    Migration(
        version=1,
        description="wiki cache and article view tables (core migrations 001, 024)",
        apply=_import_wiki_tables,
    ),
)
