"""Bug #1313 round-3: end-to-end CLI-child PG-wiring integration tests.

Codex round-3 review found the round-1/round-2 in-process fix INERT on the
real cluster hot path: cluster temporal indexing runs in a CHILD `cidx index
--index-commits` subprocess, spawned via Popen by golden_repo_manager.py /
refresh_scheduler.py, whose CLI entrypoint never installed the PostgreSQL
temporal-metadata factory. This module drives the REAL `cidx` CLI as a real
subprocess against a REAL throwaway git repository and REAL PostgreSQL
(gated by TEST_POSTGRES_DSN) to prove the fix end-to-end:

  Test A (headline): env var present, postgres bootstrap config valid ->
      child indexes successfully, NO temporal_metadata.db is created on
      disk, and rows land in the real `temporal_metadata` PostgreSQL table
      under the EXACT collection_key the child would have derived.
  Test B (opt-in, no PG required): same repo, run WITHOUT the env var ->
      default (today's) SQLite behavior is unchanged -- temporal_metadata.db
      IS created with rows.
  Test C (fail-loud): bootstrap config has storage_mode=postgres but an
      unreachable/garbage postgres_dsn -> child exits non-zero, stderr
      mentions the PostgreSQL temporal backend, and NO temporal_metadata.db
      is created (no silent SQLite fallback in the child).

Bounded by DATA (--max-commits 2), never a wall-clock timeout on the
indexing work itself (Bug #1218) -- only a generous subprocess.run(timeout=)
safety net so a genuinely hung test process doesn't block CI forever.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest

try:
    import psycopg  # noqa: F401

    HAS_PSYCOPG = True
except ImportError:
    HAS_PSYCOPG = False


# Safety-net wall-clock bound on the TEST PROCESS itself (subprocess hang
# protection for the test harness) -- NOT an indexing-work timeout (Bug
# #1218 forbids those on the indexing path; this bounds the outer test only,
# generously, for a 2-3 commit / --max-commits 2 repo).
_SUBPROCESS_SAFETY_TIMEOUT_SECONDS = 180


def _postgres_available() -> bool:
    if not HAS_PSYCOPG:
        return False
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        return False
    try:
        from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

        pool = ConnectionPool(dsn)
        try:
            with pool.connection() as conn:
                conn.execute("SELECT 1")
            return True
        finally:
            pool.close()
    except Exception:
        return False


def _voyage_api_key() -> str:
    return os.environ.get("E2E_VOYAGE_API_KEY", "") or os.environ.get(
        "VOYAGE_API_KEY", ""
    )


def _make_throwaway_git_repo(repo_dir: Path) -> None:
    """Create a real git repo with 3 commits touching a real source file."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )

    src_file = repo_dir / "example.py"
    for i in range(3):
        src_file.write_text(f"def func_{i}():\n    return {i}\n")
        subprocess.run(
            ["git", "add", "."], cwd=repo_dir, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", f"commit {i}"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )


def _cidx_init(repo_dir: Path, env: dict) -> None:
    result = subprocess.run(
        ["cidx", "init", "--embedding-provider", "voyage-ai", "--no-override-file"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        env=env,
        timeout=_SUBPROCESS_SAFETY_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, (
        f"cidx init failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def _temporal_metadata_collection_dir(repo_dir: Path) -> Path:
    """Return the collection_path TemporalMetadataStore actually uses.

    Manually verified (Bug #1313 round-3 investigation): regardless of the
    provider-aware / quarterly-sharded directory that holds the actual
    vector files (e.g. code-indexer-temporal-voyage_context_4-2026Q3), the
    hash_prefix->point_id metadata store (temporal_metadata.db in SQLite
    mode, or the PostgreSQL collection_key derived from this same path) is
    always scoped to the single LEGACY collection directory name -- a
    single shared metadata index across all shards/quarters/providers of a
    given temporal collection family.
    """
    from code_indexer.services.temporal.temporal_collection_naming import (
        LEGACY_TEMPORAL_COLLECTION,
    )

    return repo_dir / ".code-indexer" / "index" / str(LEGACY_TEMPORAL_COLLECTION)


def _collection_key_for(collection_path: Path) -> str:
    from code_indexer.storage.temporal_metadata_store import COLLECTION_KEY_LENGTH

    return hashlib.sha256(str(collection_path).encode()).hexdigest()[
        :COLLECTION_KEY_LENGTH
    ]


@pytest.mark.integration
@pytest.mark.real_api
@pytest.mark.skipif(
    not _postgres_available(),
    reason="TEST_POSTGRES_DSN not set or PostgreSQL unavailable",
)
@pytest.mark.skipif(
    not _voyage_api_key(),
    reason="E2E_VOYAGE_API_KEY/VOYAGE_API_KEY not set",
)
class TestCliIndexCommitsRoutesThroughRealPostgresChildWiring:
    """Test A (headline): the CIDX_TEMPORAL_PG_BOOTSTRAP_DIR env var makes a
    REAL child `cidx index --index-commits` subprocess route its writes
    through REAL PostgreSQL instead of creating temporal_metadata.db."""

    def test_child_indexes_via_postgres_no_sqlite_db_created(self, tmp_path):
        from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
        from code_indexer.storage.temporal_metadata_backend_registry import (
            TEMPORAL_PG_BOOTSTRAP_DIR_ENV,
        )

        dsn = os.environ["TEST_POSTGRES_DSN"]
        repo_dir = tmp_path / "repo"
        _make_throwaway_git_repo(repo_dir)

        server_dir = tmp_path / "server_dir"
        server_dir.mkdir()
        (server_dir / "config.json").write_text(
            json.dumps(
                {
                    "server_dir": str(server_dir),
                    "storage_mode": "postgres",
                    "postgres_dsn": dsn,
                }
            )
        )

        child_env = dict(os.environ)
        child_env["VOYAGE_API_KEY"] = _voyage_api_key()

        _cidx_init(repo_dir, child_env)

        child_env[TEMPORAL_PG_BOOTSTRAP_DIR_ENV] = str(server_dir)

        pool = ConnectionPool(dsn)
        collection_path = None
        try:
            result = subprocess.run(
                [
                    "cidx",
                    "index",
                    "--index-commits",
                    "--max-commits",
                    "2",
                    "--progress-json",
                ],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                env=child_env,
                timeout=_SUBPROCESS_SAFETY_TIMEOUT_SECONDS,
            )

            assert result.returncode == 0, (
                f"cidx index --index-commits failed: exit={result.returncode} "
                f"stdout={result.stdout!r} stderr={result.stderr!r}"
            )

            collection_path = _temporal_metadata_collection_dir(repo_dir)
            db_path = collection_path / "temporal_metadata.db"
            assert not db_path.exists(), (
                "Bug #1313 round-3: temporal_metadata.db must NOT be created "
                "in the CHILD subprocess when CIDX_TEMPORAL_PG_BOOTSTRAP_DIR "
                "points at a valid postgres bootstrap config"
            )

            collection_key = _collection_key_for(collection_path)
            with pool.connection() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM temporal_metadata WHERE collection_key = %s",
                    (collection_key,),
                ).fetchone()
            assert row is not None and row[0] > 0, (
                f"expected rows in real PostgreSQL under the EXACT "
                f"collection_key {collection_key!r} derived from "
                f"{collection_path}, found {row}"
            )
        finally:
            if collection_path is not None:
                collection_key = _collection_key_for(collection_path)
                with pool.connection() as conn:
                    conn.execute(
                        "DELETE FROM temporal_metadata WHERE collection_key = %s",
                        (collection_key,),
                    )
                    conn.commit()
            pool.close()


@pytest.mark.integration
@pytest.mark.real_api
@pytest.mark.skipif(
    not _voyage_api_key(),
    reason="E2E_VOYAGE_API_KEY/VOYAGE_API_KEY not set",
)
class TestCliIndexCommitsDefaultSqliteBehaviorUnchanged:
    """Test B (opt-in, no PG required): WITHOUT the env var, default SQLite
    behavior must be completely unchanged."""

    def test_no_env_var_creates_sqlite_db_with_rows(self, tmp_path):
        repo_dir = tmp_path / "repo"
        _make_throwaway_git_repo(repo_dir)

        child_env = dict(os.environ)
        child_env["VOYAGE_API_KEY"] = _voyage_api_key()
        # Explicitly absent: no CIDX_TEMPORAL_PG_BOOTSTRAP_DIR in child_env.
        child_env.pop("CIDX_TEMPORAL_PG_BOOTSTRAP_DIR", None)

        _cidx_init(repo_dir, child_env)

        result = subprocess.run(
            [
                "cidx",
                "index",
                "--index-commits",
                "--max-commits",
                "2",
                "--progress-json",
            ],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            env=child_env,
            timeout=_SUBPROCESS_SAFETY_TIMEOUT_SECONDS,
        )

        assert result.returncode == 0, (
            f"cidx index --index-commits failed: exit={result.returncode} "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

        collection_path = _temporal_metadata_collection_dir(repo_dir)
        db_path = collection_path / "temporal_metadata.db"
        assert db_path.exists(), (
            "default (no env var) behavior must be unchanged: "
            "temporal_metadata.db must still be created"
        )

        import sqlite3

        conn = sqlite3.connect(str(db_path))
        try:
            count = conn.execute("SELECT COUNT(*) FROM temporal_metadata").fetchone()[0]
        finally:
            conn.close()
        assert count > 0, "expected rows written to the default SQLite backend"


@pytest.mark.integration
@pytest.mark.real_api
@pytest.mark.skipif(
    not _voyage_api_key(),
    reason="E2E_VOYAGE_API_KEY/VOYAGE_API_KEY not set",
)
class TestCliIndexCommitsFailsLoudOnUnreachablePostgres:
    """Test C (fail-loud): bootstrap config claims postgres mode but the DSN
    is garbage/unreachable -> child must fail LOUD, never silently fall back
    to SQLite."""

    def test_unreachable_dsn_fails_loud_no_sqlite_fallback(self, tmp_path):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            TEMPORAL_PG_BOOTSTRAP_DIR_ENV,
        )

        repo_dir = tmp_path / "repo"
        _make_throwaway_git_repo(repo_dir)

        server_dir = tmp_path / "server_dir"
        server_dir.mkdir()
        garbage_dsn = (
            f"postgresql://nouser:nopass@127.0.0.1:1/nonexistent_db_{uuid.uuid4().hex}"
        )
        (server_dir / "config.json").write_text(
            json.dumps(
                {
                    "server_dir": str(server_dir),
                    "storage_mode": "postgres",
                    "postgres_dsn": garbage_dsn,
                }
            )
        )

        child_env = dict(os.environ)
        child_env["VOYAGE_API_KEY"] = _voyage_api_key()

        _cidx_init(repo_dir, child_env)

        child_env[TEMPORAL_PG_BOOTSTRAP_DIR_ENV] = str(server_dir)

        result = subprocess.run(
            [
                "cidx",
                "index",
                "--index-commits",
                "--max-commits",
                "2",
                "--progress-json",
            ],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            env=child_env,
            timeout=_SUBPROCESS_SAFETY_TIMEOUT_SECONDS,
        )

        assert result.returncode != 0, (
            "child must exit non-zero when the PG bootstrap DSN is "
            "unreachable -- no silent SQLite fallback allowed"
        )
        combined = result.stdout + result.stderr
        assert "PostgreSQL" in combined or "postgres" in combined, (
            f"error output must mention the PostgreSQL temporal backend: {combined!r}"
        )

        index_dir = repo_dir / ".code-indexer" / "index"
        if index_dir.exists():
            for collection_dir in index_dir.iterdir():
                db_path = collection_dir / "temporal_metadata.db"
                assert not db_path.exists(), (
                    f"Bug #1313 round-3: no SQLite fallback allowed on "
                    f"fail-loud path, but found {db_path}"
                )
