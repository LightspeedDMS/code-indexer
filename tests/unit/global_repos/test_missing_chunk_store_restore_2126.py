"""Story #2126: a refresh whose ESTABLISHED collection lost its ``chunks.db``
restores the store from the published snapshot (never re-embedding it), and
stays failed -- bounded, with a health reason -- when no usable snapshot
exists.

Real components throughout (the Bug #2022 harness): the real ``cidx index``
child (resolved from PATH exactly as ``_index_source`` spawns it), a real
CHUNKS_DB collection, a real published snapshot under ``.versioned/``, the
real reflink restore (``LocalCloneBackend``), the real SQLite global
registry, the real write-lock manager, and a real metadata store (SQLite
always; PostgreSQL when ``TEST_POSTGRES_DSN`` is set).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Set, Tuple
from unittest.mock import patch

import pytest

from code_indexer.server.services.embedding_stats_attribution import (
    StatsAttribution,
)
from code_indexer.server.utils.index_command_layout import append_server_layout_args
from tests.utils.cidx_fake_provider_repo import (
    SRC_DIR,
    install_network_guard,
    unique_python_text,
)
from tests.utils.fake_voyage_provider import running_fake_voyage_server
from tests.utils.fatal_chunk_store_fixtures import (
    DUMMY_VOYAGE_KEY,
    cidx_child_command,
    install_cidx_shim,
    quick_check_ok,
    read_metadata_marker,
)
from tests.utils.golden_repo_metadata_stores import (
    STORE_KINDS,
    golden_repo_metadata_store,
)
from code_indexer.global_repos.refresh_failure_recovery import RefreshDeferredError
from tests.utils.refresh_fatal_store_harness import (
    ALIAS,
    Harness,
    RecordingJobManager,
    build_harness,
    iter_chain,
    sha256_of,
    typed_kinds,
)

pytestmark = pytest.mark.timeout(300)


@pytest.fixture(params=STORE_KINDS)
def metadata(request, tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store(request.param, tmp_path) as backend:
        yield backend


@pytest.fixture
def cidx_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shim_dir = install_cidx_shim(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("VOYAGE_API_KEY", DUMMY_VOYAGE_KEY)


class _LoseLockAfterFirstCheck:
    """The real write-lock manager, except that a rival writer takes the lock
    right after the self-heal's first ownership check succeeds."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.renew_calls = 0

    def renew(self, alias: str, owner_name: str, owner_token: Any = None) -> bool:
        self.renew_calls += 1
        renewed = bool(
            self._real.renew(alias, owner_name=owner_name, owner_token=owner_token)
        )
        if self.renew_calls == 1:
            self._real.release(alias, owner_name=owner_name)
        return renewed

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _refresh_expecting_failure(harness: Harness) -> BaseException:
    with pytest.raises(Exception) as raised:
        harness.scheduler._execute_refresh(ALIAS)
    return raised.value


@pytest.mark.usefixtures("cidx_on_path")
class TestMissingStoreRestore:
    def test_restored_from_snapshot_then_next_cycle_publishes(
        self, tmp_path: Path, metadata: Any
    ) -> None:
        harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
        snapshot_sha = sha256_of(harness.snapshot_db())
        harness.source_db.unlink()

        error = _refresh_expecting_failure(harness)

        # Classified as its own kind, never corruption or environment.
        assert typed_kinds(error) == ["missing_store"], repr(error)
        # Restored byte-identical from the published snapshot (read only),
        # with the snapshot's metadata, so the next run resumes from the
        # snapshot's commit and reuses every stored embedding.
        assert harness.source_db.exists(), "missing store not restored"
        assert quick_check_ok(harness.source_db)
        assert sha256_of(harness.source_db) == snapshot_sha
        assert sha256_of(harness.snapshot_db()) == snapshot_sha
        assert read_metadata_marker(harness.source) == "snapshot-good"
        # The cycle itself never publishes; one strike bounds repeated loss.
        assert harness.scheduler.alias_manager.read_alias(ALIAS) == str(
            harness.snapshot
        )
        assert harness.strikes() == 1

        with patch.object(harness.scheduler, "_index_source"):
            result = harness.scheduler._execute_refresh(ALIAS)

        assert result["success"] is True, result
        assert harness.scheduler.alias_manager.read_alias(ALIAS) != str(
            harness.snapshot
        )
        assert harness.strikes() == 0

    def test_store_lost_again_and_again_is_quarantined_and_reported(
        self, tmp_path: Path, metadata: Any
    ) -> None:
        harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
        for attempt in range(1, 4):
            harness.source_db.unlink()
            _refresh_expecting_failure(harness)
            assert harness.source_db.exists(), f"restore {attempt} did not run"
            assert harness.strikes() == attempt

        with patch.object(harness.scheduler, "_index_source") as index_source:
            result = harness.scheduler._execute_refresh(ALIAS)

        assert result.get("skipped") == "integrity_quarantined", result
        index_source.assert_not_called()
        reasons = {
            row["golden_alias"]: row["reason"]
            for row in metadata.list_unrepairable_refresh_failures()
        }
        assert "lost its chunk store 3" in reasons.get(ALIAS, ""), reasons

    def test_writer_taking_the_lock_vetoes_the_restore(
        self, tmp_path: Path, metadata: Any
    ) -> None:
        from code_indexer.server.services.alias_lock_store.base import (
            AliasLockOwnershipLostError,
        )

        harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
        harness.source_db.unlink()
        lock = _LoseLockAfterFirstCheck(harness.scheduler.write_lock_manager)
        harness.scheduler.write_lock_manager = lock  # type: ignore[assignment]

        error = _refresh_expecting_failure(harness)

        assert lock.renew_calls >= 2, "ownership not re-checked before the copy"
        assert any(
            isinstance(e, AliasLockOwnershipLostError) for e in iter_chain(error)
        ), repr(error)
        assert not harness.source_db.exists(), "restored without the write lock"
        assert harness.strikes() == 0

    def test_reported_missing_store_outside_the_index_is_not_restored(
        self, tmp_path: Path, metadata: Any
    ) -> None:
        from code_indexer.global_repos.refresh_failure_recovery import (
            self_heal_after_fatal_chunk_store_failure,
        )
        from code_indexer.services.index_failure_exit_codes import (
            ChunkStoreFailureKind,
            FatalChunkStoreIndexError,
        )

        harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
        source_sha = sha256_of(harness.source_db)
        snapshot_sha = sha256_of(harness.snapshot_db())
        ownership_checks: List[int] = []

        self_heal_after_fatal_chunk_store_failure(
            metadata=metadata,
            snapshot_manager=harness.scheduler._snapshot_manager,
            alias_name=ALIAS,
            source_path=str(harness.source),
            current_target=str(harness.snapshot),
            error=FatalChunkStoreIndexError(
                "temporal indexing failed: chunk store missing",
                ChunkStoreFailureKind.MISSING_STORE,
            ),
            verify_ownership=lambda: ownership_checks.append(1),
        )

        assert sha256_of(harness.source_db) == source_sha
        assert sha256_of(harness.snapshot_db()) == snapshot_sha
        assert ownership_checks, "ownership not verified"
        assert harness.strikes() == 0
        state = metadata.get_refresh_failure_backoff_state(ALIAS)
        assert state is not None and state["consecutive_failure_count"] == 1
        reasons = {
            row["golden_alias"]: row["reason"]
            for row in metadata.list_unrepairable_refresh_failures()
        }
        assert "no collection under the repository index" in reasons.get(ALIAS, ""), (
            reasons
        )

    def test_corrupt_sibling_collection_is_restored_with_one_strike(
        self, tmp_path: Path, metadata: Any
    ) -> None:
        import shutil

        from tests.utils.fatal_chunk_store_fixtures import corrupt_btree_pages

        harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
        assert harness.snapshot is not None
        sibling = "voyage-code-3-sibling"
        for root in (harness.source, harness.snapshot):
            index = root / ".code-indexer" / "index"
            shutil.copytree(index / "voyage-code-3", index / sibling)
        sibling_db = harness.source_db.parent.parent / sibling / "chunks.db"
        corrupt_btree_pages(sibling_db)
        snapshot_sha = sha256_of(harness.snapshot_db())
        harness.source_db.unlink()

        error = _refresh_expecting_failure(harness)

        assert typed_kinds(error) == ["missing_store"], repr(error)
        assert sha256_of(harness.source_db) == snapshot_sha
        assert quick_check_ok(sibling_db), "corrupt sibling not restored"
        assert harness.strikes() == 1

    def test_snapshot_without_the_collection_store_is_reported(
        self, tmp_path: Path, metadata: Any
    ) -> None:
        harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
        harness.snapshot_db().unlink()
        harness.source_db.unlink()

        _refresh_expecting_failure(harness)

        assert not harness.source_db.exists()
        assert harness.strikes() == 0
        reasons = {
            row["golden_alias"]: row["reason"]
            for row in metadata.list_unrepairable_refresh_failures()
        }
        assert "has no copy of it" in reasons.get(ALIAS, ""), reasons

    def test_failed_restore_copy_is_reported(
        self, tmp_path: Path, metadata: Any
    ) -> None:
        if os.geteuid() == 0:
            pytest.skip("root ignores directory modes; the copy cannot fail")
        harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
        harness.source_db.unlink()
        collection_dir = harness.source_db.parent
        collection_dir.chmod(0o555)
        try:
            _refresh_expecting_failure(harness)
        finally:
            collection_dir.chmod(0o755)

        assert not harness.source_db.exists()
        assert harness.strikes() == 0
        reasons = {
            row["golden_alias"]: row["reason"]
            for row in metadata.list_unrepairable_refresh_failures()
        }
        assert "the restore failed" in reasons.get(ALIAS, ""), reasons

    def test_no_snapshot_stays_failed_with_operator_reason(
        self, tmp_path: Path, metadata: Any
    ) -> None:
        harness = build_harness(tmp_path, metadata, snapshot_mode="none")
        harness.source_db.unlink()

        error = _refresh_expecting_failure(harness)

        assert typed_kinds(error) == ["missing_store"], repr(error)
        assert not harness.source_db.exists(), "the missing store was recreated"
        assert harness.strikes() == 0
        state = metadata.get_refresh_failure_backoff_state(ALIAS)
        assert state is not None and state["consecutive_failure_count"] == 1
        reasons = {
            row["golden_alias"]: row["reason"]
            for row in metadata.list_unrepairable_refresh_failures()
        }
        assert "no published snapshot" in reasons.get(ALIAS, ""), reasons
        # Bounded: a system trigger is deferred by the backoff, not rerun.
        jobs = RecordingJobManager()
        harness.scheduler.background_job_manager = jobs  # type: ignore[assignment]
        with pytest.raises(RefreshDeferredError):
            harness.scheduler.trigger_refresh_for_repo(ALIAS)
        assert jobs.submitted == []

    def test_corrupt_snapshot_is_never_restored_and_reason_says_so(
        self, tmp_path: Path, metadata: Any
    ) -> None:
        harness = build_harness(tmp_path, metadata, snapshot_mode="corrupt")
        snapshot_sha = sha256_of(harness.snapshot_db())
        harness.source_db.unlink()

        _refresh_expecting_failure(harness)

        assert not harness.source_db.exists(), "restored from a corrupt snapshot"
        assert sha256_of(harness.snapshot_db()) == snapshot_sha
        assert harness.strikes() == 0
        reasons = {
            row["golden_alias"]: row["reason"]
            for row in metadata.list_unrepairable_refresh_failures()
        }
        assert "failed its integrity check" in reasons.get(ALIAS, ""), reasons


_FAKE_KEY = "issue-2126-fake-key"
_GIT = ["git", "-c", "user.email=test@example.com", "-c", "user.name=test"]
#: Pins the vendor endpoint in server context; the only flag the shim drops
#: (as in the Story #2118 test), so the child reaches the local fake that the
#: repository config names. The network guard still refuses anything else.
_ENDPOINT_PIN_FLAG = "--server-managed-provider-settings"


def _install_endpoint_shim(bin_dir: Path) -> Path:
    """A ``cidx`` on PATH running this interpreter's real CLI without
    ``_ENDPOINT_PIN_FLAG``."""
    bin_dir.mkdir(parents=True)
    shim = bin_dir / "cidx"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        f"args = [a for a in sys.argv[1:] if a != {_ENDPOINT_PIN_FLAG!r}]\n"
        "os.execv(sys.executable, "
        "[sys.executable, '-m', 'code_indexer.cli', *args])\n"
    )
    shim.chmod(0o755)
    return bin_dir


def _commit_files(repo: Path, prefix: str, count: int) -> None:
    for i in range(count):
        name = f"{prefix}_{i:03d}"
        (repo / f"{name}.py").write_text(unique_python_text(name, 40))
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run([*_GIT, "commit", "-qm", prefix], cwd=repo, check=True)


def _real_indexed_source(fake: Any, env: Dict[str, str]) -> Callable[[Path], None]:
    """Build the source as a real git repo indexed through the exact server
    ``cidx index`` command line, paying the local fake provider."""

    def build(source: Path) -> None:
        source.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=source, check=True)
        _commit_files(source, "base", 4)
        run = dict(cwd=source, env=env, capture_output=True, text=True, timeout=180)
        init = subprocess.run(cidx_child_command(["init"]), **run)  # type: ignore[call-overload]
        assert init.returncode == 0, init.stderr[-3000:]
        config_path = source / ".code-indexer" / "config.json"
        config = json.loads(config_path.read_text())
        config.setdefault("voyage_ai", {})["api_endpoint"] = (
            f"{fake.base_url}/v1/embeddings"
        )
        config_path.write_text(json.dumps(config))
        args = append_server_layout_args(
            ["cidx", "index", "--fts", "--progress-json"],
            attribution=StatsAttribution(
                golden_repo_alias=ALIAS,
                job_id="nojob:test",
                node_id="node-a",
                purpose="index",
            ),
        )
        fake.ledger.begin_run("initial")
        index = subprocess.run(
            cidx_child_command([a for a in args[1:] if a != _ENDPOINT_PIN_FLAG]),
            **run,  # type: ignore[call-overload]
        )
        assert index.returncode == 0, index.stderr[-3000:]
        assert fake.ledger.run_stats("initial")["inputs"] > 0

    return build


@pytest.fixture
def fake_provider_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Tuple[Any, Dict[str, str], Path]]:
    """The local fake VoyageAI server, and the environment every cidx child
    runs with -- also the ones the scheduler spawns: the cidx shim on PATH,
    the fake's sentinel key, an offline tokenizer cache, and a network guard
    refusing any non-loopback connection."""
    with running_fake_voyage_server(_FAKE_KEY) as fake:
        guard_root = tmp_path / "guard"
        guard_root.mkdir()
        site_dir, blocked_log = install_network_guard(guard_root)
        shim_dir = _install_endpoint_shim(tmp_path / "bin")
        monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("VOYAGE_API_KEY", _FAKE_KEY)
        monkeypatch.setenv("PYTHONPATH", f"{site_dir}{os.pathsep}{SRC_DIR}")
        monkeypatch.setenv("HF_HUB_OFFLINE", "1")
        yield fake, dict(os.environ), blocked_log
        assert not blocked_log.exists(), blocked_log.read_text()


def _stored_rows_of(chunks_db: Path, path_suffix: str) -> Set[str]:
    from code_indexer.storage.sqlite_chunk_store import ChunkStore

    with ChunkStore(chunks_db, read_only=True) as store:
        return {
            record["payload"]["content_hash"]
            for record in store.stream_all()
            if str(record["payload"].get("path", "")).endswith(path_suffix)
        }


class TestRestoreThenRefreshPaysOnlyNewContent:
    """Acceptance 1: after the restore, the next refresh pays only content
    changed since the snapshot -- 0 inputs when nothing changed."""

    def _restore(self, tmp_path: Path, metadata: Any, env_fixture, change: bool):
        fake, env, _ = env_fixture
        harness = build_harness(
            tmp_path,
            metadata,
            snapshot_mode="clean",
            source_builder=_real_indexed_source(fake, env),
        )
        if change:
            _commit_files(harness.source, "added", 1)
        harness.source_db.unlink()

        fake.ledger.begin_run("restoring-refresh")
        error = _refresh_expecting_failure(harness)

        assert typed_kinds(error) == ["missing_store"], repr(error)
        assert fake.ledger.run_stats("restoring-refresh")["inputs"] == 0
        assert sha256_of(harness.source_db) == sha256_of(harness.snapshot_db())
        return fake, harness

    def test_nothing_changed_pays_nothing(
        self, tmp_path: Path, metadata: Any, fake_provider_env
    ) -> None:
        fake, harness = self._restore(tmp_path, metadata, fake_provider_env, False)

        fake.ledger.begin_run("next-refresh")
        result = harness.scheduler._execute_refresh(ALIAS)

        assert result["success"] is True, result
        assert harness.scheduler.alias_manager.read_alias(ALIAS) != str(
            harness.snapshot
        ), "the next refresh did not publish"
        assert fake.ledger.run_stats("next-refresh")["inputs"] == 0
        assert harness.strikes() == 0

    def test_only_content_added_since_the_snapshot_is_paid(
        self, tmp_path: Path, metadata: Any, fake_provider_env
    ) -> None:
        fake, harness = self._restore(tmp_path, metadata, fake_provider_env, True)

        fake.ledger.begin_run("next-refresh")
        result = harness.scheduler._execute_refresh(ALIAS)

        assert result["success"] is True, result
        added = _stored_rows_of(harness.source_db, "added_000.py")
        assert added, "the file committed after the snapshot was not indexed"
        assert fake.ledger.run_hashes("next-refresh") == added
        assert fake.ledger.run_stats("next-refresh")["inputs"] == len(added)
        assert _stored_rows_of(harness.source_db, "base_000.py"), (
            "content restored from the snapshot is gone"
        )


_QUIET_AGE_SECONDS = 86400


def _make_quiet(repo: Path) -> None:
    """Age every working-tree file past the last run's watermark, as in a
    repository nothing has touched for a day: the next ``cidx index`` then
    plans nothing and never opens the store (Bug #2150)."""
    old = time.time() - _QUIET_AGE_SECONDS
    for path in repo.rglob("*"):
        if {".git", ".code-indexer"} & set(path.parts) or not path.is_file():
            continue
        os.utime(path, (old, old))


def _metadata_files(repo: Path) -> Dict[str, bytes]:
    return {
        p.name: p.read_bytes()
        for p in sorted((repo / ".code-indexer").glob("metadata*.json"))
    }


def _unrepairable_reasons(metadata: Any) -> Dict[str, str]:
    return {
        row["golden_alias"]: row["reason"]
        for row in metadata.list_unrepairable_refresh_failures()
    }


class TestQuietRepositoryMissingStore2150:
    """Bug #2150: with nothing to index the child exits 0 without opening the
    store, so the PUBLISH gate must classify an established collection's
    missing chunks.db as a missing store and run the #2126 restore."""

    def _quiet_harness(
        self,
        tmp_path: Path,
        metadata: Any,
        env_fixture: Tuple[Any, Dict[str, str], Path],
        snapshot_mode: str,
    ) -> Tuple[Any, Harness]:
        fake, env, _ = env_fixture
        harness = build_harness(
            tmp_path,
            metadata,
            snapshot_mode=snapshot_mode,
            source_builder=_real_indexed_source(fake, env),
        )
        _make_quiet(harness.source)
        harness.source_db.unlink()
        return fake, harness

    def test_restored_at_publish_gate_then_next_refresh_publishes_free(
        self, tmp_path: Path, metadata: Any, fake_provider_env
    ) -> None:
        fake, harness = self._quiet_harness(
            tmp_path, metadata, fake_provider_env, "clean"
        )
        snapshot_sha = sha256_of(harness.snapshot_db())
        assert harness.snapshot is not None
        snapshot_metadata = _metadata_files(harness.snapshot)
        assert snapshot_metadata, "the snapshot carries no indexing metadata"

        fake.ledger.begin_run("restoring-refresh")
        result = harness.scheduler._execute_refresh(ALIAS)

        assert result["success"] is False, result
        assert fake.ledger.run_stats("restoring-refresh")["inputs"] == 0
        assert harness.source_db.exists(), "missing store not restored"
        assert sha256_of(harness.source_db) == snapshot_sha
        assert sha256_of(harness.snapshot_db()) == snapshot_sha
        assert _metadata_files(harness.source) == snapshot_metadata
        assert harness.scheduler.alias_manager.read_alias(ALIAS) == str(
            harness.snapshot
        )
        assert harness.strikes() == 1
        assert ALIAS not in _unrepairable_reasons(metadata)
        # The job error names the missing store (never a generic failure).
        assert "chunks.db" in result.get("error", ""), result
        assert "missing" in result["error"], result

        fake.ledger.begin_run("next-refresh")
        result = harness.scheduler._execute_refresh(ALIAS)

        assert result["success"] is True, result
        assert harness.scheduler.alias_manager.read_alias(ALIAS) != str(
            harness.snapshot
        ), "the next refresh did not publish"
        assert fake.ledger.run_stats("next-refresh")["inputs"] == 0
        assert harness.strikes() == 0
        assert metadata.get_refresh_failure_backoff_state(ALIAS) is None

    @pytest.mark.parametrize(
        "snapshot_mode, cause",
        [
            ("none", "no published snapshot"),
            ("corrupt", "failed its integrity check"),
        ],
    )
    def test_unrestorable_stays_failed_with_operator_health_reason(
        self,
        tmp_path: Path,
        metadata: Any,
        fake_provider_env,
        snapshot_mode: str,
        cause: str,
    ) -> None:
        from types import SimpleNamespace

        from code_indexer.server.services.refresh_repair_health import (
            unrepairable_refresh_health_reasons,
        )

        fake, harness = self._quiet_harness(
            tmp_path, metadata, fake_provider_env, snapshot_mode
        )
        snapshot_sha = (
            sha256_of(harness.snapshot_db()) if harness.snapshot is not None else None
        )

        fake.ledger.begin_run("failing-refresh")
        result = harness.scheduler._execute_refresh(ALIAS)

        assert result["success"] is False, result
        assert fake.ledger.run_stats("failing-refresh")["inputs"] == 0
        assert not harness.source_db.exists(), "the missing store was recreated"
        if snapshot_sha is not None:
            assert sha256_of(harness.snapshot_db()) == snapshot_sha
        assert harness.strikes() == 0
        state = metadata.get_refresh_failure_backoff_state(ALIAS)
        assert state is not None and state["consecutive_failure_count"] == 1
        assert cause in _unrepairable_reasons(metadata).get(ALIAS, ""), (
            _unrepairable_reasons(metadata)
        )
        health = unrepairable_refresh_health_reasons(
            SimpleNamespace(golden_repo_metadata=metadata)
        )
        assert any(
            f"Refresh of {ALIAS} needs an operator" in r and cause in r for r in health
        ), health

    def test_writer_taking_the_lock_at_the_gate_vetoes_the_restore(
        self, tmp_path: Path, metadata: Any, fake_provider_env
    ) -> None:
        from code_indexer.global_repos import refresh_failure_recovery
        from code_indexer.server.services.alias_lock_store.base import (
            AliasLockOwnershipLostError,
        )
        from tests.utils.refresh_fatal_store_harness import REPO

        _, harness = self._quiet_harness(tmp_path, metadata, fake_provider_env, "clean")
        lock = harness.scheduler.write_lock_manager
        real_gate = refresh_failure_recovery.run_integrity_gate_against_published

        def rival_takes_the_lock_then_gate(*args: Any, **kwargs: Any) -> Any:
            assert lock.release(REPO, owner_name="refresh_scheduler")
            assert lock.acquire(REPO, owner_name="rival-writer")
            return real_gate(*args, **kwargs)

        with patch.object(
            refresh_failure_recovery,
            "run_integrity_gate_against_published",
            side_effect=rival_takes_the_lock_then_gate,
        ):
            error = _refresh_expecting_failure(harness)

        assert any(
            isinstance(e, AliasLockOwnershipLostError) for e in iter_chain(error)
        ), repr(error)
        assert not harness.source_db.exists(), "restored without the write lock"
        assert harness.strikes() == 0
