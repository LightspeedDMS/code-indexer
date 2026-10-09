"""Real-service environment for the query repository-access tests.

Every service is real: AccessFilteringService over a GroupAccessManager
(temp SQLite), the global repo registry (GlobalReposSqliteBackend over the
real server schema), alias pointer files read by the real AliasManager,
ActivatedRepoManager / GoldenRepoManager over a temp data dir, and a real
BackgroundJobManager running query jobs on its worker threads. Nothing about
the access decision is mocked.

Only the genuinely external embedding + HNSW boundary
(SemanticSearchService.search_repository_path) is replaced -- the same
boundary the other query-manager tests in this directory fake -- by a
function that returns fixed per-repository rows and records which
repository paths were actually searched.

Repository aliases, URLs and usernames are neutral placeholders.
"""

from __future__ import annotations

import importlib
import shutil
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Set
from unittest.mock import patch

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from code_indexer.server.models.api_models import (
    SearchResultItem,
    SemanticSearchResponse,
)
from code_indexer.server.query.semantic_query_manager import SemanticQueryManager
from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.services.access_filtering_service import (
    AccessFilteringService,
)
from code_indexer.server.services.config_service import (
    ConfigService,
    reset_config_service,
    set_config_service,
)
from code_indexer.server.services.group_access_manager import GroupAccessManager
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend
from code_indexer.server.storage.sqlite_backends.global_repos_backend import (
    GlobalReposSqliteBackend,
)

# Registered FIRST so an unnarrowed repo selection reaches it first.
UNGRANTED_REPO = "other-repo"
GRANTED_REPO = "example-repo"
GRANTED_REPO_2 = "second-repo"
ALL_REPOS = (UNGRANTED_REPO, GRANTED_REPO, GRANTED_REPO_2)

USER = "example_user"
ADMIN = "example_admin"

SEARCH_BOUNDARY = (
    "code_indexer.server.services.search_service."
    "SemanticSearchService.search_repository_path"
)

# Rows per repository returned by the search boundary. The ungranted
# repository scores highest, so an unnarrowed search fills the result
# limit with its rows.
ROWS_PER_REPO = 3
_SCORE_BASE = {UNGRANTED_REPO: 0.95, GRANTED_REPO: 0.80, GRANTED_REPO_2: 0.70}
_ACTIVATED_SCORE_BASE = 0.60
# user_alias of the caller's own activation of GRANTED_REPO (see activate_for).
OWN_ACTIVATION = "my-repo"

# Repositories USER's group is granted (UNGRANTED_REPO is in no group).
GRANTED_REPOS = (GRANTED_REPO, GRANTED_REPO_2)
# A result limit large enough never to truncate the rows of every repo.
ALL_ROWS_LIMIT = len(ALL_REPOS) * ROWS_PER_REPO


def global_alias(repo: str) -> str:
    return f"{repo}-global"


SERVER_DB_NAME = "cidx_server.db"


def build_server_db_template(directory: Path) -> Path:
    """Build the real server schema once into *directory*; return its path.

    The schema-only database holds no rows and no paths, so one build can be
    byte-copied into every test's own data dir (QueryAccessEnv then writes
    its per-test rows, absolute repo paths included, into its copy only).
    initialize_database closes its connection; the WAL is then checkpointed
    and the last connection closed, so the main file alone is the complete
    database and no -wal/-shm sidecar is left to copy.
    """
    db_path = directory / SERVER_DB_NAME
    DatabaseSchema(str(db_path)).initialize_database()
    conn = sqlite3.connect(str(db_path))
    try:
        # Row is (busy, wal_frames, checkpointed_frames).
        busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
    finally:
        conn.close()
    assert busy == 0, "template database checkpoint was blocked"
    sidecars = sorted(p.name for p in directory.glob(f"{SERVER_DB_NAME}-*"))
    assert not sidecars, f"template database left sidecars: {sidecars}"
    return db_path


def _init_git_repo(path: Path) -> None:
    """Create a real one-commit git repository on branch 'main' at *path*."""
    path.mkdir(parents=True)
    (path / "README.md").write_text("example\n")
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "add", "README.md"],
        [
            "git",
            "-c",
            "user.name=example",
            "-c",
            "user.email=example@example.com",
            "commit",
            "-q",
            "-m",
            "init",
        ],
    ):
        subprocess.run(cmd, cwd=path, check=True, capture_output=True)


class QueryAccessEnv:
    """Real services wired the way the server wires them."""

    def __init__(self, tmp_path: Path, server_db_template: Path) -> None:
        self.data_dir = tmp_path / "data"
        self.data_dir.mkdir()
        db_path = str(self.data_dir / SERVER_DB_NAME)
        # This test's own byte copy of the schema built once by
        # build_server_db_template (closed and checkpointed).
        shutil.copyfile(server_db_template, db_path)
        # Real config service rooted at this temp server dir (its
        # data/cidx_server.db is the DB above), never the operator's home.
        config_service = ConfigService(server_dir_path=str(tmp_path))
        # Memory retrieval is a separate feature that embeds through a real
        # provider; switch it off with its real kill switch.
        config_service.update_setting(
            "memory_retrieval", "memory_retrieval_enabled", False
        )
        set_config_service(config_service)
        self.payload_cache = PayloadCache(
            db_path=tmp_path / "payload_cache.db", config=PayloadCacheConfig()
        )
        self.payload_cache.initialize()
        self.golden_repo_manager = GoldenRepoManager(data_dir=str(self.data_dir))
        golden_dir = Path(self.golden_repo_manager.golden_repos_dir)
        self.alias_manager = AliasManager(str(golden_dir / "aliases"))
        self.global_repos = GlobalReposSqliteBackend(db_path)
        self.repo_paths: Dict[str, Path] = {}
        for repo in ALL_REPOS:
            repo_path = golden_dir / repo
            _init_git_repo(repo_path)
            self.repo_paths[repo] = repo_path
            self.golden_repo_manager._sqlite_backend.add_repo(
                alias=repo,
                repo_url=f"https://git.example.com/example/{repo}.git",
                default_branch="main",
                clone_path=str(repo_path),
                created_at=datetime(2024, 1, 1, tzinfo=timezone.utc).isoformat(),
            )
            self.global_repos.register_repo(
                alias_name=global_alias(repo),
                repo_name=repo,
                repo_url=f"https://git.example.com/example/{repo}.git",
                index_path=str(repo_path),
            )
            self.alias_manager.create_alias(
                global_alias(repo), str(repo_path), repo_name=repo
            )

        self.job_manager = BackgroundJobManager(
            storage_path=str(tmp_path / "jobs.json")
        )
        self.activated_repo_manager = ActivatedRepoManager(
            data_dir=str(self.data_dir),
            golden_repo_manager=self.golden_repo_manager,
            background_job_manager=self.job_manager,
            clone_backend=LocalCloneBackend(),
        )
        self.query_manager = SemanticQueryManager(
            data_dir=str(self.data_dir),
            activated_repo_manager=self.activated_repo_manager,
            background_job_manager=self.job_manager,
        )

        gam = GroupAccessManager(tmp_path / "groups.db")
        group = gam.create_group("restricted", "test group")
        gam.assign_user_to_group(USER, group.id, assigned_by="test")
        gam.grant_repo_access(GRANTED_REPO, group.id, granted_by="test")
        gam.grant_repo_access(GRANTED_REPO_2, group.id, granted_by="test")
        # UNGRANTED_REPO is granted to NO group at all, so an admin can only
        # reach it through the admin bypass, never through a group grant.
        admins = gam.get_group_by_name("admins")
        assert admins is not None, "bootstrap must create the 'admins' group"
        gam.assign_user_to_group(ADMIN, admins.id, assigned_by="test")
        self.group_manager = gam
        self.group_id = group.id
        self.access_service = AccessFilteringService(
            gam, activated_repo_manager=self.activated_repo_manager
        )

        self.searched_paths: List[str] = []
        self.failing_repos: Set[str] = set()

    def app_stand_in(self, access_service: Optional[Any]) -> SimpleNamespace:
        """Object exposing the app.state attributes the query paths read."""
        return SimpleNamespace(
            state=SimpleNamespace(
                backend_registry=SimpleNamespace(global_repos=self.global_repos),
                access_filtering_service=access_service,
                payload_cache=self.payload_cache,
                query_tracker=None,
                golden_repos_dir=self.golden_repo_manager.golden_repos_dir,
            )
        )

    @contextmanager
    def installed(self, access_service: Optional[Any]) -> Iterator[None]:
        """Install the app stand-in, services and search boundary for a block.

        Written straight into the namespace of the code_indexer.server.app
        MODULE (what production code reaches as ``app_module.app`` /
        ``app_module.semantic_query_manager``): the stand-in app plus this
        env's real query and activated-repo managers. Patching those names
        through getattr would first trigger the module's PEP 562 lazy
        full-application construction (real config, locks and caches under
        the operator's home directory).
        """
        app_module = importlib.import_module("code_indexer.server.app")
        namespace = vars(app_module)
        # Every lazy name a query path probes must be installed: probing a
        # missing one (e.g. golden_repo_manager in the category-map lookup)
        # constructs the full application, which replaces the stand-in app
        # mid-request and silently drops the access service.
        installs = {
            "app": self.app_stand_in(access_service),
            "semantic_query_manager": self.query_manager,
            "activated_repo_manager": self.activated_repo_manager,
            "golden_repo_manager": self.golden_repo_manager,
        }
        previous = {name: namespace[name] for name in installs if name in namespace}
        namespace.update(installs)
        try:
            with patch(SEARCH_BOUNDARY, self.fake_search_repository_path()):
                yield
        finally:
            for name in installs:
                if name in previous:
                    namespace[name] = previous[name]
                else:
                    del namespace[name]

    def fake_search_repository_path(self) -> Any:
        env = self

        def _search(
            _self: Any, *, repo_path: str, search_request: Any, **_kw: Any
        ) -> SemanticSearchResponse:
            repo = Path(repo_path).name
            env.searched_paths.append(repo_path)
            if repo in env.failing_repos:
                raise RuntimeError("index unreadable")
            # A path outside the golden set is one of the caller's activations.
            base = _SCORE_BASE.get(repo, _ACTIVATED_SCORE_BASE)
            items = [
                SearchResultItem(
                    score=base - i * 0.01,
                    file_path=f"src/{repo}_{i}.py",
                    line_start=1,
                    line_end=1,
                    content=f"content of {repo} file {i}",
                    language=None,
                    file_last_modified=None,
                    indexed_timestamp=None,
                )
                for i in range(ROWS_PER_REPO)
            ]
            return SemanticSearchResponse(
                query=search_request.query, total=len(items), results=items
            )

        return _search

    def activate_for(self, username: str, golden_alias: str, user_alias: str) -> str:
        """Activate *golden_alias* for *username* through the real manager."""
        job_id = self.activated_repo_manager.activate_repository(
            username=username, golden_repo_alias=golden_alias, user_alias=user_alias
        )
        status = self.wait_for_job(job_id, username)
        assert status["status"] == "completed", status
        return job_id

    def searched_repos(self) -> Set[str]:
        return {Path(p).name for p in self.searched_paths}

    def wait_for_job(self, job_id: str, username: str) -> Dict[str, Any]:
        """Poll the real job manager until the job is terminal (bounded)."""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = self.job_manager.get_job_status(job_id, username)
            assert status is not None, f"job {job_id} not visible to {username}"
            if status["status"] in ("completed", "failed", "cancelled"):
                return status
            time.sleep(0.05)
        raise AssertionError(f"query job {job_id} did not finish within 30s")

    def close(self) -> None:
        self.job_manager.shutdown()
        self.payload_cache.close()
        reset_config_service()


def result_repos(rows: List[Dict[str, Any]]) -> Set[str]:
    return {str(r["repository_alias"]) for r in rows}
