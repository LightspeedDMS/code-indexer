"""A non-git (``local://``) repository's refresh decides "changed" by file
IDENTITY against the PUBLISHED snapshot, never by clock ordering against a
snapshot name, and a removal makes the refresh's index run drop gone paths.

Real components throughout: the real ``RefreshScheduler`` refresh, the real
``cidx index`` / ``cidx fix-config`` children (resolved from PATH exactly as
the scheduler spawns them) paying the local fake VoyageAI server, the real
snapshot manager with the real ``LocalCloneBackend``, the real alias files
and a real SQLite metadata store.
"""

from __future__ import annotations

import contextlib
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, Set, Tuple
from unittest.mock import patch

import pytest

from code_indexer.server.services.embedding_stats_attribution import (
    StatsAttribution,
)
from code_indexer.server.utils.config_manager import (
    CidxMetaBackupConfig,
    ServerConfig,
)
from code_indexer.server.utils.index_command_layout import append_server_layout_args
from tests.utils.cidx_fake_provider_repo import unique_python_text
from tests.utils.fatal_chunk_store_fixtures import COLLECTION_NAME, cidx_child_command
from tests.utils.golden_repo_metadata_stores import golden_repo_metadata_store
from tests.utils.refresh_fake_provider_env import (
    ENDPOINT_PIN_FLAG,
    fake_provider_refresh_env,
    point_repo_at_fake,
)
from tests.utils.refresh_fatal_store_harness import ALIAS, Harness, build_harness

pytestmark = pytest.mark.timeout(300)

_FAKE_KEY = "local-identity-fake-key"
#: A write landing after the run's ``cidx index`` child exited, before the
#: scheduler named the snapshot: the shim performs it when this spec exists.
_PENDING_WRITE = "pending-write.txt"


@pytest.fixture
def metadata(tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store("sqlite", tmp_path) as backend:
        yield backend


@pytest.fixture
def provider_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Tuple[Any, Dict[str, str], Path]]:
    with fake_provider_refresh_env(tmp_path, monkeypatch, _FAKE_KEY) as env:
        _wrap_shim_with_late_writer(tmp_path / "bin" / "cidx", tmp_path)
        yield env


def _wrap_shim_with_late_writer(shim: Path, tmp_path: Path) -> None:
    """After an ``index`` child exits, perform the pending write (if any)
    with a whole-second mtime no later than the child's start -- a coarse
    (1 s) filesystem timestamp of a write made while the run was indexing."""
    spec = tmp_path / _PENDING_WRITE
    shim.write_text(
        f"#!{sys.executable}\n"
        "import math, os, subprocess, sys, time\n"
        "started = time.time()\n"
        f"args = [a for a in sys.argv[1:] if a != {ENDPOINT_PIN_FLAG!r}]\n"
        "rc = subprocess.call([sys.executable, '-m', 'code_indexer.cli', *args])\n"
        f"spec = {str(spec)!r}\n"
        "if args[:1] == ['index'] and os.path.exists(spec):\n"
        "    target, text = open(spec).read().split('\\n', 1)\n"
        "    with open(target, 'w') as handle:\n"
        "        handle.write(text)\n"
        "    coarse = math.floor(started)\n"
        "    os.utime(target, (coarse, coarse))\n"
        "    os.unlink(spec)\n"
        "sys.exit(rc)\n"
    )


def _indexed_non_git_source(fake: Any, env: Dict[str, str]) -> Callable[[Path], None]:
    """A non-git source indexed through the exact server ``cidx index``
    command line (the harness's snapshot is then a pre-manifest copy)."""

    def build(source: Path) -> None:
        source.mkdir(parents=True)
        for i in range(3):
            name = f"note_{i:03d}"
            (source / f"{name}.py").write_text(unique_python_text(name, 20))
        run = dict(cwd=source, env=env, capture_output=True, text=True, timeout=180)
        init = subprocess.run(cidx_child_command(["init"]), **run)  # type: ignore[call-overload]
        assert init.returncode == 0, init.stderr[-3000:]
        point_repo_at_fake(source, fake)
        args = append_server_layout_args(
            ["cidx", "index", "--fts", "--progress-json"],
            attribution=StatsAttribution(
                golden_repo_alias=ALIAS,
                job_id="nojob:test",
                node_id="node-a",
                purpose="index",
            ),
        )
        index = subprocess.run(
            cidx_child_command([a for a in args[1:] if a != ENDPOINT_PIN_FLAG]),
            **run,  # type: ignore[call-overload]
        )
        assert index.returncode == 0, index.stderr[-3000:]

    return build


def _published(harness: Harness) -> Path:
    target = harness.scheduler.alias_manager.read_alias(ALIAS)
    assert target is not None
    return Path(target)


def _stored_paths(snapshot: Path) -> Set[str]:
    db = snapshot / ".code-indexer" / "index" / COLLECTION_NAME / "chunks.db"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {str(row[0]) for row in conn.execute("SELECT path FROM chunks")}
    finally:
        conn.close()


def _fts_hits(snapshot: Path, env: Dict[str, str], term: str) -> str:
    proc = subprocess.run(
        cidx_child_command(["query", term, "--fts", "--quiet", "--rerank-query", ""]),
        cwd=snapshot,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return proc.stdout


def _harness(
    tmp_path: Path, metadata: Any, provider_env
) -> Tuple[Harness, Dict[str, str]]:
    fake, env, _ = provider_env
    harness = build_harness(
        tmp_path,
        metadata,
        snapshot_mode="clean",
        source_builder=_indexed_non_git_source(fake, env),
    )
    # The pre-manifest snapshot is an unknown baseline: the first refresh
    # publishes and records the run's source manifest.
    first = harness.scheduler._execute_refresh(ALIAS)
    assert first["message"] == "Refresh complete", first
    assert _published(harness) != harness.snapshot
    return harness, env


class TestUnchangedSource:
    def test_no_change_after_publish(
        self, tmp_path: Path, metadata: Any, provider_env
    ) -> None:
        harness, _ = _harness(tmp_path, metadata, provider_env)
        before = _published(harness)

        result = harness.scheduler._execute_refresh(ALIAS)

        assert result["message"] == "No changes detected", result
        assert _published(harness) == before


class TestDeletedFile:
    def test_deleted_file_leaves_the_published_index(
        self, tmp_path: Path, metadata: Any, provider_env
    ) -> None:
        harness, env = _harness(tmp_path, metadata, provider_env)
        before = _published(harness)
        assert "note_001.py" in _stored_paths(before)
        assert "note_001.py" in _fts_hits(before, env, "note_001_function_3")
        (harness.source / "note_001.py").unlink()

        result = harness.scheduler._execute_refresh(ALIAS)

        assert result["message"] == "Refresh complete", result
        after = _published(harness)
        assert after != before
        assert "note_001.py" not in _stored_paths(after)
        assert "note_000.py" in _stored_paths(after)
        assert "note_001.py" not in _fts_hits(after, env, "note_001_function_3")


META_ALIAS = "cidx-meta-global"
_GIT = ["git", "-c", "user.email=test@example.com", "-c", "user.name=test"]


def _git(repo: Path, *args: str) -> None:
    subprocess.run([*_GIT, *args], cwd=repo, check=True, capture_output=True)


def _origin(tmp_path: Path) -> Path:
    return tmp_path / "cidx-meta-backup.git"


def _git_backed_catalog(
    harness: Harness, fake: Any, env: Dict[str, str], tmp_path: Path
) -> Path:
    """A ``local://cidx-meta`` catalog that is a git repository mirrored to a
    local bare origin, indexed for real, published as a pre-manifest
    snapshot."""
    from code_indexer.server.services.cidx_meta_backup.bootstrap import (
        CidxMetaBackupBootstrap,
    )

    meta = harness.source.parent / "cidx-meta"
    (meta / "memories").mkdir(parents=True)
    (meta / "memories" / "seed.md").write_text("# seed\n\nseed memory body\n")
    _git(tmp_path, "init", "-q", "--bare", str(_origin(tmp_path)))
    CidxMetaBackupBootstrap().bootstrap(str(meta), str(_origin(tmp_path)))
    _indexed_catalog(meta, fake, env)
    snapshot = meta.parent / ".versioned" / "cidx-meta" / "v_1000000000"
    shutil.copytree(meta, snapshot, symlinks=True)
    harness.registry.register_global_repo(
        "cidx-meta",
        META_ALIAS,
        "local://cidx-meta",
        index_path=str(snapshot),
        allow_reserved=True,
    )
    harness.scheduler.alias_manager.create_alias(
        META_ALIAS, str(snapshot), repo_name="cidx-meta"
    )
    return meta


def _indexed_catalog(meta: Path, fake: Any, env: Dict[str, str]) -> None:
    run = dict(cwd=meta, env=env, capture_output=True, text=True, timeout=180)
    init = subprocess.run(cidx_child_command(["init"]), **run)  # type: ignore[call-overload]
    assert init.returncode == 0, init.stderr[-3000:]
    point_repo_at_fake(meta, fake)
    index = subprocess.run(
        cidx_child_command(["index", "--fts", "--new-collection-layout=chunks_db"]),
        **run,  # type: ignore[call-overload]
    )
    assert index.returncode == 0, index.stderr[-3000:]


@contextlib.contextmanager
def _backup_enabled(tmp_path: Path) -> Iterator[None]:
    """The real ``ServerConfig`` with the catalog backup mirror enabled,
    pointing at the bare origin the catalog was bootstrapped with."""
    config = ServerConfig(server_dir=str(tmp_path / "server"))
    config.cidx_meta_backup_config = CidxMetaBackupConfig(
        enabled=True, remote_url=str(_origin(tmp_path))
    )
    config_service = SimpleNamespace(
        get_config=lambda: config,
        sync_repo_extensions_if_drifted=lambda repo_path: None,
    )
    with patch(
        "code_indexer.global_repos.refresh_scheduler.get_config_service",
        return_value=config_service,
    ):
        yield


class TestBackupMirrorCommittedWrite:
    def test_write_committed_and_pushed_by_its_writer_is_indexed(
        self, tmp_path: Path, metadata: Any, provider_env
    ) -> None:
        fake, env, _ = provider_env
        harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
        meta = _git_backed_catalog(harness, fake, env, tmp_path)
        (meta / "memories" / "first.md").write_text("# first\n\nfirst memory\n")
        with _backup_enabled(tmp_path):
            first = harness.scheduler._execute_refresh(META_ALIAS)
            assert first["message"] == "Refresh complete", first
            # The X-Ray pattern service's end state: its writer committed and
            # pushed the file itself, so the refresh's sync has nothing to do.
            (meta / "memories" / "pushed.md").write_text("# pushed\n\npushed body\n")
            _git(meta, "add", "memories/pushed.md")
            _git(meta, "commit", "-qm", "writer commit")
            _git(meta, "push", "-q", "origin", "master")

            second = harness.scheduler._execute_refresh(META_ALIAS)

        assert second["message"] == "Refresh complete", second
        published = harness.scheduler.alias_manager.read_alias(META_ALIAS)
        assert published is not None
        assert "memories/pushed.md" in _stored_paths(Path(published))


class TestWriteAfterListing:
    def test_write_landing_after_indexing_is_indexed_by_the_next_refresh(
        self, tmp_path: Path, metadata: Any, provider_env
    ) -> None:
        harness, env = _harness(tmp_path, metadata, provider_env)
        (harness.source / "note_000.py").write_text(unique_python_text("edited", 20))
        late = harness.source / "late_000.py"
        (tmp_path / _PENDING_WRITE).write_text(
            f"{late}\n{unique_python_text('late_000', 20)}"
        )

        during = harness.scheduler._execute_refresh(ALIAS)

        assert during["message"] == "Refresh complete", during
        assert late.exists() and not (tmp_path / _PENDING_WRITE).exists()
        assert "late_000.py" not in _stored_paths(_published(harness))

        after = harness.scheduler._execute_refresh(ALIAS)

        assert after["message"] == "Refresh complete", after
        assert "late_000.py" in _stored_paths(_published(harness))
        assert "late_000.py" in _fts_hits(
            _published(harness), env, "late_000_function_2"
        )
