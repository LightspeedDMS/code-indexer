"""Regression guard: DescriptionRefreshScheduler must use backend_registry.golden_repo_metadata.

Bug: In lifespan.py, DescriptionRefreshScheduler is constructed WITHOUT a golden_backend
argument, so it always defaults to GoldenRepoMetadataSqliteBackend(db_path). In
cluster/postgres mode the golden repos live in PostgreSQL (golden_repos_metadata table),
but the local SQLite db has only 1 stale row. The scheduler's _list_golden_aliases()
and get_repo() see only 1 local repo, so description backfill, lifecycle backfill, and
scheduled refresh never process the real cluster repos.

Fix: In lifespan.py where DescriptionRefreshScheduler(...) is constructed, pass:
    golden_backend=(backend_registry.golden_repo_metadata if backend_registry is not None else None)

- Cluster mode: backend_registry is not None => passes GoldenRepoMetadataPostgresBackend.
- Solo mode: backend_registry is None => passes None => scheduler falls back to SQLite (unchanged).

Tests:
1. Wiring/source-order test: lifespan.py constructs DescriptionRefreshScheduler with golden_backend=
   referencing backend_registry.golden_repo_metadata.
2. Contract/consistency test: both GoldenRepoMetadataPostgresBackend._row_to_dict_basic and
   GoldenRepoMetadataSqliteBackend.list_repos return dicts with an "alias" key.
3. Scheduler injection test: _list_golden_aliases() returns all aliases when injected
   with a fake golden_backend whose list_repos() returns multiple repos.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)

# ---------------------------------------------------------------------------
# Anchors for source-text searches
# ---------------------------------------------------------------------------

# The DescriptionRefreshScheduler constructor call in lifespan.py
_SCHEDULER_CTOR = "description_refresh_scheduler = DescriptionRefreshScheduler("

# The golden_backend kwarg we require to be passed
_GOLDEN_BACKEND_ARG = "golden_backend="

# The registry attribute we require
_REGISTRY_ATTR = "backend_registry.golden_repo_metadata"


def _source() -> str:
    return _LIFESPAN_PATH.read_text()


# ---------------------------------------------------------------------------
# 1. Wiring / source-order tests
# ---------------------------------------------------------------------------


class TestLifespanGoldenBackendWiringSource:
    """Source-text guard: DescriptionRefreshScheduler must receive golden_backend=
    referencing backend_registry.golden_repo_metadata."""

    def test_golden_backend_arg_passed_to_description_refresh_scheduler(self):
        """lifespan.py must pass golden_backend= to DescriptionRefreshScheduler.

        Without this argument the scheduler always defaults to
        GoldenRepoMetadataSqliteBackend(db_path) — the local per-node SQLite — even
        in cluster/postgres mode where the real repos are in PostgreSQL.

        Fails before fix (golden_backend= absent); passes after fix.
        """
        source = _source()

        ctor_pos = source.find(_SCHEDULER_CTOR)
        assert ctor_pos != -1, (
            f"DescriptionRefreshScheduler constructor not found in lifespan.py: "
            f"{_SCHEDULER_CTOR!r}"
        )

        # Find the closing paren of the constructor call — scan forward from ctor_pos
        # looking for golden_backend= somewhere between the opening paren and a
        # reasonable distance (2000 chars covers the multi-line ctor).
        ctor_block = source[ctor_pos : ctor_pos + 2000]
        assert _GOLDEN_BACKEND_ARG in ctor_block, (
            f"Bug: DescriptionRefreshScheduler construction in lifespan.py does not "
            f"pass {_GOLDEN_BACKEND_ARG!r}.\n"
            "In cluster/postgres mode the scheduler will always use the local SQLite "
            "backend (1 stale repo) instead of PostgreSQL (15+ cluster repos), making "
            "description backfill and scheduled refresh non-functional.\n"
            "Fix: add golden_backend=(backend_registry.golden_repo_metadata "
            "if backend_registry is not None else None) to the DescriptionRefreshScheduler call."
        )

    def test_golden_backend_references_backend_registry_golden_repo_metadata(self):
        """The golden_backend= arg must reference backend_registry.golden_repo_metadata.

        This ensures cluster mode receives GoldenRepoMetadataPostgresBackend, not a
        freshly-constructed SQLite backend.

        Fails before fix; passes after fix.
        """
        source = _source()

        assert _REGISTRY_ATTR in source, (
            f"Bug: lifespan.py does not reference {_REGISTRY_ATTR!r}.\n"
            "The DescriptionRefreshScheduler golden_backend must be wired from "
            "backend_registry.golden_repo_metadata so cluster mode uses PostgreSQL."
        )

        # The reference must appear after the DescriptionRefreshScheduler ctor starts
        ctor_pos = source.find(_SCHEDULER_CTOR)
        assert ctor_pos != -1, (
            f"DescriptionRefreshScheduler constructor not found: {_SCHEDULER_CTOR!r}"
        )

        registry_attr_pos = source.find(_REGISTRY_ATTR, ctor_pos)
        assert registry_attr_pos != -1, (
            f"{_REGISTRY_ATTR!r} must appear AFTER the DescriptionRefreshScheduler "
            f"constructor call (pos {ctor_pos}) — either inside the call or in the "
            "surrounding wiring block.\n"
            "Current state: the attribute exists elsewhere but not near the scheduler ctor."
        )

    def test_golden_backend_wiring_is_guarded_for_solo_mode(self):
        """The wiring must fall back to None when backend_registry is None (solo mode).

        Solo/SQLite mode has no backend_registry; passing None lets the scheduler
        fall back to GoldenRepoMetadataSqliteBackend(db_path) as before.

        We verify the ternary / guard pattern is present near the constructor.
        """
        source = _source()

        ctor_pos = source.find(_SCHEDULER_CTOR)
        assert ctor_pos != -1, (
            f"DescriptionRefreshScheduler constructor not found: {_SCHEDULER_CTOR!r}"
        )

        # Look for a None-guard pattern in the vicinity of the constructor
        ctor_region = source[ctor_pos : ctor_pos + 2000]
        has_none_guard = (
            "backend_registry is not None" in ctor_region
            or "if backend_registry" in ctor_region
        )
        assert has_none_guard, (
            "Bug: The golden_backend wiring near DescriptionRefreshScheduler does not "
            "guard against backend_registry being None (solo/SQLite mode).\n"
            "Use: golden_backend=(backend_registry.golden_repo_metadata "
            "if backend_registry is not None else None)"
        )


# ---------------------------------------------------------------------------
# 2. Contract / consistency test
# ---------------------------------------------------------------------------


class TestGoldenRepoMetadataBackendContract:
    """Verify both SQLite and Postgres backends return List[Dict] with 'alias' key.

    The scheduler's _list_golden_aliases() does:
        repos = self._golden_backend.list_repos() or []
        alias = repo.get("alias") if isinstance(repo, dict) else None

    Both backends must return dicts with "alias" so this lookup works correctly.
    """

    def test_postgres_backend_row_to_dict_basic_has_alias_key(self):
        """GoldenRepoMetadataPostgresBackend._row_to_dict_basic must include 'alias' key.

        _list_golden_aliases() extracts alias via repo.get("alias"). If the Postgres
        backend returns a different key (e.g., 'alias_name'), aliases would all be None
        and the scheduler would process 0 repos even with correct wiring.
        """
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        # Construct a minimal 8-column row matching the list_repos SELECT:
        # alias, repo_url, default_branch, clone_path, created_at,
        # enable_temporal, temporal_options, wiki_enabled
        sample_row = (
            "my-repo",  # alias
            "https://example.com/repo.git",  # repo_url
            "main",  # default_branch
            "/data/repos/my-repo",  # clone_path
            "2024-01-01T00:00:00Z",  # created_at
            True,  # enable_temporal
            None,  # temporal_options
            False,  # wiki_enabled
        )

        result = GoldenRepoMetadataPostgresBackend._row_to_dict_basic(sample_row)

        assert isinstance(result, dict), (
            "GoldenRepoMetadataPostgresBackend._row_to_dict_basic must return a dict"
        )
        assert "alias" in result, (
            "GoldenRepoMetadataPostgresBackend._row_to_dict_basic must include 'alias' key.\n"
            f"Got keys: {list(result.keys())}\n"
            "The scheduler's _list_golden_aliases() does repo.get('alias') — if the key "
            "is named differently, all cluster repos are silently skipped."
        )
        assert result["alias"] == "my-repo", (
            f"Expected alias='my-repo', got {result['alias']!r}"
        )

    def test_sqlite_backend_list_repos_returns_dicts_with_alias_key(self, tmp_path):
        """GoldenRepoMetadataSqliteBackend.list_repos() must return List[Dict] with 'alias'.

        Baseline parity test: confirms the SQLite backend (which the scheduler has
        always used) returns the same shape the scheduler expects.
        Uses a real in-memory SQLite database (no mocks).
        """
        import sqlite3

        from code_indexer.server.storage.sqlite_backends import (
            GoldenRepoMetadataSqliteBackend,
        )

        db_path = str(tmp_path / "test.db")

        # Create the table and insert a row directly
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE golden_repos_metadata (
                alias TEXT PRIMARY KEY NOT NULL,
                repo_url TEXT NOT NULL,
                default_branch TEXT NOT NULL,
                clone_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                enable_temporal INTEGER NOT NULL DEFAULT 0,
                temporal_options TEXT,
                category_id INTEGER,
                category_auto_assigned INTEGER DEFAULT 0,
                wiki_enabled INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            "INSERT INTO golden_repos_metadata "
            "(alias, repo_url, default_branch, clone_path, created_at, enable_temporal) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "test-repo",
                "https://example.com/test.git",
                "main",
                "/repos/test",
                "2024-01-01",
                0,
            ),
        )
        conn.commit()
        conn.close()

        backend = GoldenRepoMetadataSqliteBackend(db_path)
        repos = backend.list_repos()

        assert isinstance(repos, list), (
            "GoldenRepoMetadataSqliteBackend.list_repos() must return a list"
        )
        assert len(repos) == 1, f"Expected 1 repo, got {len(repos)}"
        assert isinstance(repos[0], dict), (
            "Each element from list_repos() must be a dict"
        )
        assert "alias" in repos[0], (
            f"SQLite list_repos() dict must have 'alias' key. Got: {list(repos[0].keys())}"
        )
        assert repos[0]["alias"] == "test-repo"

    def test_both_backends_have_consistent_alias_key_shape(self, tmp_path):
        """Both Postgres and SQLite backends must produce dicts with the same 'alias' key.

        Shape parity: the scheduler uses one code path (_list_golden_aliases) against
        whichever backend is injected. If one returns 'alias' and the other returns
        something different, the cluster/solo behaviour diverges silently.
        """
        import sqlite3

        from code_indexer.server.storage.sqlite_backends import (
            GoldenRepoMetadataSqliteBackend,
        )
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        # SQLite side: real backend with in-memory DB
        db_path = str(tmp_path / "parity.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE golden_repos_metadata (
                alias TEXT PRIMARY KEY NOT NULL,
                repo_url TEXT NOT NULL,
                default_branch TEXT NOT NULL,
                clone_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                enable_temporal INTEGER NOT NULL DEFAULT 0,
                temporal_options TEXT,
                category_id INTEGER,
                category_auto_assigned INTEGER DEFAULT 0,
                wiki_enabled INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            "INSERT INTO golden_repos_metadata "
            "(alias, repo_url, default_branch, clone_path, created_at, enable_temporal) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "parity-repo",
                "https://example.com/parity.git",
                "main",
                "/repos/parity",
                "2024-01-01",
                0,
            ),
        )
        conn.commit()
        conn.close()

        sqlite_backend = GoldenRepoMetadataSqliteBackend(db_path)
        sqlite_repos = sqlite_backend.list_repos()
        sqlite_keys = set(sqlite_repos[0].keys()) if sqlite_repos else set()

        # Postgres side: use _row_to_dict_basic with a sample row
        sample_row = (
            "parity-repo",
            "https://example.com/parity.git",
            "main",
            "/repos/parity",
            "2024-01-01",
            False,
            None,
            False,
        )
        pg_dict = GoldenRepoMetadataPostgresBackend._row_to_dict_basic(sample_row)
        pg_keys = set(pg_dict.keys())

        # Both must have 'alias'
        assert "alias" in sqlite_keys, (
            f"SQLite backend missing 'alias' key. Keys: {sqlite_keys}"
        )
        assert "alias" in pg_keys, (
            f"Postgres backend missing 'alias' key. Keys: {pg_keys}"
        )

        # Both must have the same core keys
        core_keys = {
            "alias",
            "repo_url",
            "default_branch",
            "clone_path",
            "created_at",
            "enable_temporal",
            "temporal_options",
            "wiki_enabled",
        }
        missing_sqlite = core_keys - sqlite_keys
        missing_pg = core_keys - pg_keys
        assert not missing_sqlite, (
            f"SQLite list_repos() missing core keys: {missing_sqlite}"
        )
        assert not missing_pg, (
            f"Postgres _row_to_dict_basic missing core keys: {missing_pg}"
        )


# ---------------------------------------------------------------------------
# 3. Scheduler injection test
# ---------------------------------------------------------------------------


class TestDescriptionRefreshSchedulerGoldenBackendInjection:
    """Verify that _list_golden_aliases() returns all aliases when a fake golden_backend
    is injected via the constructor. This proves injection works end-to-end."""

    def _make_fake_backend(self, repos: List[Dict[str, Any]]):
        """Build a minimal fake golden backend that returns fixed repos."""

        class FakeGoldenBackend:
            def __init__(self, repo_list):
                self._repos: List[Dict[str, Any]] = repo_list

            def list_repos(self) -> List[Dict[str, Any]]:
                return list(self._repos)

            def get_repo(self, alias: str) -> Optional[Dict[str, Any]]:
                for r in self._repos:
                    if r.get("alias") == alias:
                        return r
                return None

            def close(self) -> None:
                pass

        return FakeGoldenBackend(repos)

    def _make_fake_tracking_backend(self):
        """Build a minimal fake tracking backend (required by scheduler ctor)."""

        class FakeTrackingBackend:
            def get_refresh_record(self, alias):
                return None

            def upsert_refresh_record(self, *args, **kwargs):
                pass

            def list_repos(self):
                return []

            def close(self):
                pass

        return FakeTrackingBackend()

    def _make_config_manager(self, tmp_path):
        """Build a real ServerConfig with ClaudeIntegrationConfig for the scheduler ctor.

        The scheduler reads config_manager.load_config().claude_integration_config.
        max_concurrent_claude_cli — it must be an int, not a MagicMock.
        """
        from code_indexer.server.utils.config_manager import (
            ServerConfig,
            ClaudeIntegrationConfig,
        )

        config = ServerConfig(server_dir=str(tmp_path))
        config.claude_integration_config = ClaudeIntegrationConfig()

        config_manager = MagicMock()
        config_manager.load_config.return_value = config
        return config_manager

    def test_list_golden_aliases_returns_all_injected_repos(self, tmp_path):
        """_list_golden_aliases() must return all aliases from the injected backend.

        When a fake golden_backend is injected with 3 repos, _list_golden_aliases()
        must return exactly those 3 aliases — proving the constructor injection is
        wired through to the internal _golden_backend attribute.
        """
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        fake_repos = [
            {
                "alias": "click",
                "repo_url": "https://example.com/click.git",
                "default_branch": "main",
                "clone_path": "/repos/click",
                "created_at": "2024-01-01",
                "enable_temporal": False,
                "temporal_options": None,
                "wiki_enabled": False,
            },
            {
                "alias": "fastapi",
                "repo_url": "https://example.com/fastapi.git",
                "default_branch": "main",
                "clone_path": "/repos/fastapi",
                "created_at": "2024-01-01",
                "enable_temporal": False,
                "temporal_options": None,
                "wiki_enabled": False,
            },
            {
                "alias": "pydantic",
                "repo_url": "https://example.com/pydantic.git",
                "default_branch": "main",
                "clone_path": "/repos/pydantic",
                "created_at": "2024-01-01",
                "enable_temporal": False,
                "temporal_options": None,
                "wiki_enabled": False,
            },
        ]

        fake_golden = self._make_fake_backend(fake_repos)
        fake_tracking = self._make_fake_tracking_backend()
        config_manager = self._make_config_manager(tmp_path)

        scheduler = DescriptionRefreshScheduler(
            db_path=None,  # not needed when both backends injected
            config_manager=config_manager,
            tracking_backend=fake_tracking,
            golden_backend=fake_golden,
        )

        aliases = scheduler._list_golden_aliases()

        assert aliases is not None, (
            "_list_golden_aliases() must not return None on success"
        )
        assert set(aliases) == {"click", "fastapi", "pydantic"}, (
            f"Expected aliases {{'click', 'fastapi', 'pydantic'}}, got {aliases}\n"
            "The injected golden_backend must be used by _list_golden_aliases()."
        )

    def test_list_golden_aliases_with_cluster_sized_repo_list(self, tmp_path):
        """_list_golden_aliases() handles a cluster-sized list (15 repos) correctly.

        Mirrors the staging scenario: 15 repos in PostgreSQL, all aliases returned.
        """
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        cluster_aliases = [
            "click",
            "fastapi",
            "pydantic",
            "rich",
            "jinja",
            "langfuse-core",
            "langfuse-python",
            "langfuse-js",
            "langfuse-docs",
            "evolution",
            "requests",
            "httpx",
            "sqlalchemy",
            "alembic",
            "celery",
        ]
        fake_repos = [
            {
                "alias": a,
                "repo_url": f"https://example.com/{a}.git",
                "default_branch": "main",
                "clone_path": f"/repos/{a}",
                "created_at": "2024-01-01",
                "enable_temporal": False,
                "temporal_options": None,
                "wiki_enabled": False,
            }
            for a in cluster_aliases
        ]

        fake_golden = self._make_fake_backend(fake_repos)
        fake_tracking = self._make_fake_tracking_backend()
        config_manager = self._make_config_manager(tmp_path)

        scheduler = DescriptionRefreshScheduler(
            db_path=None,
            config_manager=config_manager,
            tracking_backend=fake_tracking,
            golden_backend=fake_golden,
        )

        aliases = scheduler._list_golden_aliases()

        assert aliases is not None
        assert set(aliases) == set(cluster_aliases), (
            f"Expected {len(cluster_aliases)} aliases, got {len(aliases) if aliases else 0}.\n"
            f"Missing: {set(cluster_aliases) - set(aliases or [])}"
        )

    def test_scheduler_falls_back_to_sqlite_when_golden_backend_is_none(self, tmp_path):
        """When golden_backend=None, scheduler defaults to GoldenRepoMetadataSqliteBackend.

        Solo mode safety: passing golden_backend=None (when backend_registry is None)
        must not break the scheduler — it should fall back to the local SQLite backend.
        """
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )
        from code_indexer.server.storage.sqlite_backends import (
            GoldenRepoMetadataSqliteBackend,
        )

        db_path = str(tmp_path / "solo.db")
        config_manager = self._make_config_manager(tmp_path)

        scheduler = DescriptionRefreshScheduler(
            db_path=db_path,
            config_manager=config_manager,
            golden_backend=None,  # None => falls back to SQLite
        )

        assert isinstance(scheduler._golden_backend, GoldenRepoMetadataSqliteBackend), (
            "When golden_backend=None, scheduler must fall back to "
            "GoldenRepoMetadataSqliteBackend. Solo mode must be unaffected by the fix."
        )
