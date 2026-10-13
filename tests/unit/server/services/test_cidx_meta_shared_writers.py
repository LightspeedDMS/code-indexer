"""Server-side cidx-meta writers and readers on a mount-less pod.

Each writer runs twice on identical starting content:

* locally, on ``local/golden-repos/cidx-meta`` (today's code path), and
* delegated, on the POD's ``golden-repos/cidx-meta`` -- index state only --
  with every local file access under it forbidden; the content lives with the
  fake workspace-mcp.

The resulting local tree and remote tree must be identical, and the pod's own
directory must be untouched.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Tuple
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services import cidx_meta_content as cmc
from tests.unit.server.cidx_meta_seam_fakes import (
    FakeWriteError,
    MetaWorld,
)


@pytest.fixture
def meta_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MetaWorld:
    return MetaWorld(tmp_path, monkeypatch)


def _both(world: MetaWorld, run: Callable[[Path, Path], Any]) -> Tuple[Any, Any]:
    """Run *run(golden_repos_dir, meta_dir)* locally then delegated; assert
    the content agrees and the pod's directory was not touched."""
    world.local_mode()
    local_result = run(world.local_golden, world.local_meta)
    pod_before = world.pod_tree()
    world.delegated()
    with world.forbid_local_io():
        delegated_result = run(world.pod_golden, world.pod_meta)
    world.local_mode()
    assert world.remote_tree() == world.local_tree()
    assert world.pod_tree() == pod_before
    return local_result, delegated_result


class _Scheduler:
    """The write-lock surface of RefreshScheduler, recording every call."""

    def __init__(self, acquire: bool = True) -> None:
        self.acquire = acquire
        self.events: List[Tuple[str, str, str]] = []

    def acquire_write_lock(self, alias: str, owner_name: str = "x", **_: Any) -> bool:
        self.events.append(("acquire", alias, owner_name))
        return self.acquire

    def release_write_lock(self, alias: str, owner_name: str = "x") -> bool:
        self.events.append(("release", alias, owner_name))
        return True

    def is_write_locked(self, alias: str) -> bool:
        return False

    def trigger_refresh_for_repo(self, alias: str) -> None:
        self.events.append(("refresh", alias, ""))

    def get_write_lock_holder(self, alias: str) -> None:
        return None


# ── MetaDirectoryUpdater ─────────────────────────────────────────────────


def _registry(*aliases: str) -> SimpleNamespace:
    return SimpleNamespace(
        list_global_repos=lambda: [{"alias_name": f"{a}-global"} for a in aliases]
    )


def test_meta_directory_updater_syncs_shared_content(meta_world: MetaWorld) -> None:
    from code_indexer.global_repos.meta_directory_updater import MetaDirectoryUpdater

    meta_world.seed("kept.md", "# real description\n")
    meta_world.seed("orphan.md", "# gone\n")
    meta_world.seed("README.md", "not managed\n")
    registry = _registry("kept", "added")

    def run(_golden: Path, meta: Path) -> Tuple[bool, bool, List[Tuple[str, str, str]]]:
        scheduler = _Scheduler()
        updater = MetaDirectoryUpdater(str(meta), registry, refresh_scheduler=scheduler)
        before = updater.has_changes()
        updater.update()
        return before, updater.has_changes(), scheduler.events

    local, delegated = _both(meta_world, run)
    assert (
        local
        == delegated
        == (
            True,
            False,
            [
                ("acquire", "cidx-meta", "meta_directory_updater"),
                ("release", "cidx-meta", "meta_directory_updater"),
            ],
        )
    )
    assert meta_world.remote_tree() == {
        "README.md": b"not managed\n",
        "added.md": b"# added\n\nGolden repository: added\n",
        "kept.md": b"# real description\n",
    }


def test_meta_directory_updater_stub_never_overwrites(meta_world: MetaWorld) -> None:
    """Another pod wrote the description between the listing and the stub:
    the stub's "must not exist" precondition keeps it."""
    from code_indexer.global_repos.meta_directory_updater import MetaDirectoryUpdater

    meta_world.delegated()
    shared = cmc.shared_cidx_meta(meta_world.pod_meta)
    assert shared is not None
    (meta_world.remote_meta / "raced.md").write_text("real\n")
    MetaDirectoryUpdater(str(meta_world.pod_meta), _registry())._create_shared_stub(
        shared, "raced"
    )
    assert (meta_world.remote_meta / "raced.md").read_text() == "real\n"


def test_meta_directory_updater_mass_delete_gate_on_shared(
    meta_world: MetaWorld,
) -> None:
    from code_indexer.global_repos.meta_directory_updater import (
        MetaDirectoryMassDeleteBlocked,
        MetaDirectoryUpdater,
    )

    for name in ("a", "b", "c", "d"):
        meta_world.seed(f"{name}.md", "x")

    def run(_golden: Path, meta: Path) -> str:
        with pytest.raises(MetaDirectoryMassDeleteBlocked) as exc:
            MetaDirectoryUpdater(str(meta), _registry("a")).update()
        return str(exc.value)

    local, delegated = _both(meta_world, run)
    assert local == delegated


def test_meta_directory_updater_unreachable_backend_raises(
    meta_world: MetaWorld,
) -> None:
    from code_indexer.global_repos.meta_directory_updater import MetaDirectoryUpdater

    meta_world.delegated()
    meta_world.backend.fail = ConnectionError("down")
    with meta_world.forbid_local_io():
        with pytest.raises(cmc.CidxMetaUnavailableError):
            MetaDirectoryUpdater(str(meta_world.pod_meta), _registry("a")).has_changes()
    assert meta_world.writer.calls == []


# ── description hook ─────────────────────────────────────────────────────


def test_atomic_write_description(meta_world: MetaWorld) -> None:
    from code_indexer.global_repos.meta_description_hook import (
        atomic_write_description,
    )

    meta_world.seed("repo.md", "old\n")

    def run(_golden: Path, meta: Path) -> List[Tuple[str, str, str]]:
        scheduler = _Scheduler()
        atomic_write_description(meta / "repo.md", "new ✓\n", scheduler)
        atomic_write_description(meta / "fresh.md", "fresh\n", scheduler)
        return scheduler.events

    local, delegated = _both(meta_world, run)
    assert local == delegated
    assert meta_world.remote_tree() == {
        "repo.md": "new ✓\n".encode(),
        "fresh.md": b"fresh\n",
    }


def test_atomic_write_description_failure_releases_the_lock(
    meta_world: MetaWorld,
) -> None:
    from code_indexer.global_repos.meta_description_hook import (
        atomic_write_description,
    )

    meta_world.delegated()
    meta_world.writer.fail = FakeWriteError("EUNAVAILABLE", "down")
    scheduler = _Scheduler()
    with meta_world.forbid_local_io():
        with pytest.raises(cmc.CidxMetaUnavailableError):
            atomic_write_description(meta_world.pod_meta / "r.md", "x", scheduler)
    assert [e[0] for e in scheduler.events] == ["acquire", "release"]


def test_on_repo_removed_deletes_the_shared_description(
    meta_world: MetaWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.global_repos import meta_description_hook as hook

    monkeypatch.setattr(hook, "_refresh_scheduler", None)
    monkeypatch.setattr(hook, "_tracking_backend", None)
    meta_world.seed("gone.md", "x")
    meta_world.seed("stays.md", "y")

    def run(golden: Path, _meta: Path) -> None:
        hook.on_repo_removed("gone", str(golden))
        hook.on_repo_removed("never-existed", str(golden))

    _both(meta_world, run)
    assert meta_world.remote_tree() == {"stays.md": b"y"}


def test_on_repo_added_writes_the_shared_description(
    meta_world: MetaWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.global_repos import meta_description_hook as hook

    monkeypatch.setattr(hook, "_refresh_scheduler", None)
    monkeypatch.setattr(hook, "_tracking_backend", None)
    cli = MagicMock(spec=hook.ClaudeCliManager)
    cli.check_cli_available.return_value = True
    monkeypatch.setattr(hook, "get_claude_cli_manager", lambda: cli)
    monkeypatch.setattr(
        hook, "_generate_repo_description", lambda *a, **k: "---\nname: r\n---\nbody\n"
    )
    config = SimpleNamespace(
        claude_integration_config=SimpleNamespace(dep_map_fact_check_enabled=False)
    )
    monkeypatch.setattr(
        "code_indexer.server.services.config_service.get_config_service",
        lambda: SimpleNamespace(get_config=lambda: config),
    )

    def run(golden: Path, _meta: Path) -> None:
        hook.on_repo_added("r", "https://x/r.git", "/clone/r", str(golden))

    _both(meta_world, run)
    assert meta_world.remote_tree() == {"r.md": b"---\nname: r\n---\nbody\n"}


# ── lifecycle writer ─────────────────────────────────────────────────────


def test_write_meta_md_and_broken_scan(meta_world: MetaWorld) -> None:
    from code_indexer.global_repos.lifecycle_batch_runner import (
        CURRENT_LIFECYCLE_SCHEMA_VERSION,
        LifecycleFleetScanner,
        _read_meta_md,
        write_meta_md,
    )

    meta_world.seed("old.md", "no frontmatter\n")

    def run(golden: Path, meta: Path) -> Tuple[List[str], Any, Any]:
        scheduler = _Scheduler()
        write_meta_md(
            alias="fresh",
            description_body="A repo.",
            lifecycle_frontmatter={
                "lifecycle": {"ci": "gitlab"},
                "lifecycle_schema_version": CURRENT_LIFECYCLE_SCHEMA_VERSION,
            },
            already_locked=False,
            refresh_scheduler=scheduler,
            golden_repos_dir=golden,
        )
        runner = object.__new__(LifecycleFleetScanner)
        runner._cidx_meta_dir = meta
        runner._repo_aliases = ["fresh", "old", "missing"]
        return (
            runner.find_broken_or_missing(),
            _read_meta_md(meta / "missing.md"),
            scheduler.events,
        )

    local, delegated = _both(meta_world, run)
    assert local == delegated
    assert local[0] == ["old", "missing"] and local[1] is None
    with pytest.raises(FileNotFoundError):
        meta_world.delegated()
        _read_meta_md(meta_world.pod_meta / "missing.md", required=True)


# ── description refresh scheduler ────────────────────────────────────────


def test_description_scheduler_reads_and_renames(meta_world: MetaWorld) -> None:
    from code_indexer.server.services.description_refresh_scheduler import (
        DescriptionRefreshScheduler as DRS,
    )

    meta_world.seed("legacy-global.md", "---\nname: legacy\n---\n" + "x" * 900)
    meta_world.seed("both-global.md", "old")
    meta_world.seed("both.md", "new")
    meta_world.seed("terse.md", "---\na: b\n---\nshort\n")
    meta_world.seed("empty.md", "   \n")

    def run(_golden: Path, meta: Path) -> Tuple[int, Any, List[bool]]:
        fake = SimpleNamespace(
            _meta_dir=meta, _list_golden_aliases=lambda: ["legacy", "both", "none"]
        )
        renamed = DRS._migrate_global_suffix_filenames(fake)  # type: ignore[arg-type]
        terse = DRS._find_terse_description_aliases(
            fake,
            ["legacy", "terse", "none", "cidx-meta"],  # type: ignore[arg-type]
        )
        has = [
            DRS._has_existing_description(fake, a)  # type: ignore[arg-type]
            for a in ("legacy", "empty", "none")
        ]
        return renamed, terse, has

    local, delegated = _both(meta_world, run)
    assert local == delegated == (1, ["terse"], [True, False, False])
    assert "legacy.md" in meta_world.remote_tree()
    assert "both-global.md" in meta_world.remote_tree()  # target existed: kept


# ── memories ─────────────────────────────────────────────────────────────


def _memory_service(memories_dir: Path, locks: Path, ids: List[str]) -> Any:
    from code_indexer.server.services.memory_file_lock_manager import (
        MemoryFileLockManager,
    )
    from code_indexer.server.services.memory_rate_limiter import (
        MemoryRateLimiter,
        RateLimitConfig,
    )
    from code_indexer.server.services.memory_store_service import (
        MemoryStoreConfig,
        MemoryStoreService,
    )
    from datetime import datetime, timezone

    scheduler = MagicMock()
    scheduler.is_write_locked.return_value = False
    scheduler.acquire_write_lock.return_value = True
    queue = list(ids)
    return MemoryStoreService(
        config=MemoryStoreConfig(memories_dir=memories_dir),
        lock_manager=MemoryFileLockManager(locks),
        refresh_scheduler=scheduler,
        refresh_debouncer=MagicMock(),
        rate_limiter=MemoryRateLimiter(
            RateLimitConfig(capacity=100, refill_per_second=100.0)
        ),
        hostname="h",
        clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
        id_factory=lambda: queue.pop(0),
    )


_MEMORY = {
    "type": "architectural-fact",
    "scope": "global",
    "summary": "short",
    "evidence": [{"commit": "abc123"}],
}


def test_memory_lifecycle_on_shared_content(
    meta_world: MetaWorld, tmp_path: Path
) -> None:
    from code_indexer.server.services.memory_io import read_memory_file

    def run(_golden: Path, meta: Path) -> Dict[str, Any]:
        svc = _memory_service(
            meta / "memories",
            tmp_path / f"locks-{meta.parent.parent.name}",
            ["m1", "m2"],
        )
        one = svc.create_memory(dict(_MEMORY), "alice")
        two = svc.create_memory(dict(_MEMORY), "bob")
        edited = svc.edit_memory(
            "m1", {"summary": "edited"}, one["content_hash"], "carol"
        )
        svc.delete_memory("m2", two["content_hash"], "bob")
        fm, body, digest = read_memory_file(meta / "memories" / "m1.md")
        return {
            "hashes": (one["content_hash"], edited["content_hash"], digest),
            "summary": fm["summary"],
            "body": body,
        }

    local, delegated = _both(meta_world, run)
    assert local == delegated
    assert local["summary"] == "edited"
    assert list(meta_world.remote_tree()) == ["memories/m1.md"]


def test_memory_edit_conflicts_with_another_pod(
    meta_world: MetaWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another pod rewrites the memory between this pod's read and write: the
    write's compare-and-swap fails and the caller sees StaleContentError."""
    from code_indexer.server.services import memory_io
    from code_indexer.server.services.memory_store_service import StaleContentError

    meta_world.delegated()
    svc = _memory_service(meta_world.pod_meta / "memories", tmp_path / "l", ["m1"])
    created = svc.create_memory(dict(_MEMORY), "alice")
    target = meta_world.remote_meta / "memories" / "m1.md"
    real_precondition = memory_io._shared_precondition

    def racing_precondition(shared, path, expected):
        token = real_precondition(shared, path, expected)
        target.write_text(target.read_text().replace("short", "other pod"))
        return token

    monkeypatch.setattr(memory_io, "_shared_precondition", racing_precondition)
    with meta_world.forbid_local_io():
        with pytest.raises(StaleContentError) as exc:
            svc.edit_memory("m1", {"summary": "mine"}, created["content_hash"], "bob")
    assert "other pod" in target.read_text()
    assert exc.value.current_hash != created["content_hash"]


def test_memory_stale_hash_and_missing_on_shared(
    meta_world: MetaWorld, tmp_path: Path
) -> None:
    from code_indexer.server.services.memory_store_service import (
        NotFoundError,
        StaleContentError,
    )

    meta_world.delegated()
    svc = _memory_service(meta_world.pod_meta / "memories", tmp_path / "l", ["m1"])
    svc.create_memory(dict(_MEMORY), "alice")
    with meta_world.forbid_local_io():
        with pytest.raises(StaleContentError):
            svc.edit_memory("m1", {"summary": "x"}, "0" * 64, "bob")
        with pytest.raises(StaleContentError):
            svc.delete_memory("m1", "0" * 64, "bob")
        with pytest.raises(NotFoundError):
            svc.delete_memory("absent", "0" * 64, "bob")


def test_memory_io_create_refuses_an_existing_shared_file(
    meta_world: MetaWorld,
) -> None:
    from code_indexer.server.services.memory_io import atomic_write_memory_file

    meta_world.seed("memories/m.md", "---\na: 1\n---\n")
    meta_world.delegated()
    with meta_world.forbid_local_io():
        with pytest.raises(cmc.CidxMetaConflictError):
            atomic_write_memory_file(
                meta_world.pod_meta / "memories" / "m.md", {"a": 2}
            )


def test_memory_factory_does_not_create_the_local_dir(
    meta_world: MetaWorld, tmp_path: Path
) -> None:
    from code_indexer.server.services.memory_store_service_factory import (
        build_memory_store_service,
    )

    meta_world.delegated()
    with meta_world.forbid_local_io():
        bundle = build_memory_store_service(
            golden_repos_dir=meta_world.pod_golden,
            server_data_dir=tmp_path / "data",
            refresh_scheduler=MagicMock(),
            refresh_debouncer=MagicMock(),
        )
    assert bundle.memories_dir == meta_world.pod_meta / "memories"
    assert not bundle.memories_dir.exists()
    meta_world.local_mode()
    build_memory_store_service(
        golden_repos_dir=meta_world.local_golden,
        server_data_dir=tmp_path / "data",
        refresh_scheduler=MagicMock(),
        refresh_debouncer=MagicMock(),
    )
    assert (meta_world.local_meta / "memories").is_dir()


# ── README fallbacks (ClaudeCliManager catch-up) ─────────────────────────


def test_fallback_catchup_on_shared_content(
    meta_world: MetaWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.services import claude_cli_manager as ccm

    meta_world.seed("alpha_README.md", "readme a\n")
    meta_world.seed("beta_README.md", "readme b\n")
    meta_world.seed("beta.md", "stale\n")
    git_calls: List[List[str]] = []
    spawned: List[Tuple[List[str], str]] = []
    monkeypatch.setattr(
        ccm, "run_git_command", lambda argv, **kw: git_calls.append(argv)
    )
    monkeypatch.setattr(
        ccm.subprocess,
        "run",
        lambda argv, **kw: (
            spawned.append((argv, kw["cwd"]))
            or SimpleNamespace(returncode=0, stderr=b"")
        ),
    )
    monkeypatch.setattr(
        "code_indexer.server.utils.index_command_layout.configured_content_backend_name",
        lambda: "delegated" if meta_world.backend is _parked() else "local",
    )

    def _manager(meta: Path) -> Any:
        manager = object.__new__(ccm.ClaudeCliManager)
        manager._meta_dir = meta
        manager.sync_api_key = lambda: None
        return manager

    def run(_golden: Path, meta: Path) -> List[str]:
        manager = _manager(meta)
        found = manager.scan_for_fallbacks()
        for alias, path in found:
            assert manager._process_single_fallback(alias, path)
        return sorted(alias for alias, _ in found)

    local, delegated = _both(meta_world, run)
    assert local == delegated == ["alpha", "beta"]
    assert meta_world.remote_tree() == {
        "alpha.md": b"readme a\n",
        "beta.md": b"readme b\n",
    }

    meta_world.delegated()
    with meta_world.forbid_local_io():
        _manager(meta_world.pod_meta)._commit_and_reindex(["alpha"])
    assert git_calls == []  # no git of shared content on this pod
    argv, cwd = spawned[-1]
    assert cwd == str(meta_world.pod_meta)
    assert "--content-backend=delegated" in argv
    assert "--git-backend=delegated" not in argv


def _parked() -> Any:
    from code_indexer.server.startup import content_backend_wiring

    return content_backend_wiring.server_content_backend()


def test_memory_readers_read_the_shared_content(meta_world: MetaWorld) -> None:
    """The metadata cache (access filtering) and body hydration (retrieval)
    read through read_memory_file, so they see the shared memory -- the pod
    holds none."""
    from code_indexer.server.mcp.memory_retrieval_pipeline import (
        _hydrate_memory_bodies,
    )
    from code_indexer.server.services.memory_io import serialize_memory
    from code_indexer.server.services.memory_metadata_cache import (
        MemoryMetadataCache,
    )

    memory_id = "a" * 32
    meta_world.seed(
        f"memories/{memory_id}.md", serialize_memory({"scope": "global"}, "the body\n")
    )
    meta_world.delegated()
    cache = MemoryMetadataCache(meta_world.pod_meta / "memories")
    assert cache.get(memory_id) == {"scope": "global"}
    hydrated = _hydrate_memory_bodies(
        [{"memory_id": memory_id}], str(meta_world.pod_meta)
    )
    assert hydrated == [{"memory_id": memory_id, "body": "the body"}]


def test_memory_io_refuses_bytes_other_than_the_callers(
    meta_world: MetaWorld,
) -> None:
    """The service checked hash H, then another pod rewrote the file before
    memory_io read it again: the write is refused, never conditional on the
    other pod's bytes."""
    from code_indexer.server.services.memory_io import (
        atomic_delete_memory_file,
        atomic_write_memory_file,
        compute_content_hash,
    )

    meta_world.seed("memories/m.md", "---\na: 1\n---\n")
    checked = compute_content_hash(b"---\na: 0\n---\n")  # what the caller saw
    path = meta_world.pod_meta / "memories" / "m.md"
    meta_world.delegated()
    with meta_world.forbid_local_io():
        with pytest.raises(cmc.CidxMetaConflictError):
            atomic_write_memory_file(path, {"a": 2}, expected_content_hash=checked)
        with pytest.raises(cmc.CidxMetaConflictError):
            atomic_delete_memory_file(path, expected_content_hash=checked)
    assert meta_world.remote_tree() == {"memories/m.md": b"---\na: 1\n---\n"}


# ── diagnostics + repository health (description routes) ────────────────


def test_diagnostics_generates_missing_descriptions_into_shared_content(
    meta_world: MetaWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.routers import diagnostics

    meta_world.seed("has.md", "described\n")
    monkeypatch.setattr(diagnostics, "get_claude_cli_manager", lambda: None)

    def run(golden: Path, meta: Path) -> Tuple[int, int, List[str]]:
        queued: List[str] = []

        def fake_queue(**kwargs: Any) -> bool:
            queued.append(kwargs["alias"])
            diagnostics._make_description_callback(
                kwargs["alias"],
                kwargs["md_file"],
                kwargs["repo_url"],
                kwargs["clone_path"],
                lambda alias, url, path: f"# {alias}\n",
            )(True, "")
            return True

        monkeypatch.setattr(diagnostics, "_queue_repo_description", fake_queue)
        manager = SimpleNamespace(
            list_golden_repos=lambda: [
                {"alias": "has"},
                {"alias": "missing"},
                {"alias": "../evil"},
                {"alias": "cidx-meta"},
            ],
            get_actual_repo_path=lambda alias: f"/clone/{alias}",
        )
        state = SimpleNamespace(
            golden_repos_dir=str(golden), golden_repo_manager=manager
        )
        result = diagnostics._generate_descriptions_sync(state)
        return result.repos_with_descriptions, result.repos_queued, queued

    local, delegated = _both(meta_world, run)
    assert local == delegated == (1, 1, ["missing"])
    assert meta_world.remote_tree() == {
        "has.md": b"described\n",
        "missing.md": b"# missing\n",
    }


@pytest.mark.parametrize(
    "alias, status", [("repo", None), ("absent", 404), ("../x", 404)]
)
def test_repository_description_route_reads_shared_content(
    meta_world: MetaWorld,
    monkeypatch: pytest.MonkeyPatch,
    alias: str,
    status: Any,
) -> None:
    import asyncio

    from fastapi import HTTPException

    from code_indexer.server.routers import repository_health as rh

    meta_world.seed("repo.md", "---\nname: repo\n---\nThe body.\n")
    monkeypatch.setattr(rh, "_enforce_direct_repo_access", lambda alias, user: None)
    user = SimpleNamespace(username="u")

    def run(golden: Path, _meta: Path) -> Any:
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(golden_repos_dir=str(golden)))
        )
        try:
            return asyncio.run(
                rh.get_repository_description(alias, request, current_user=user)
            ).description
        except HTTPException as exc:
            return exc.status_code

    local, delegated = _both(meta_world, run)
    assert local == delegated
    if status:
        assert local == status
    else:
        assert local.strip() == "The body."


def test_repository_description_route_fails_loud_when_backend_is_down(
    meta_world: MetaWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from code_indexer.server.routers import repository_health as rh

    monkeypatch.setattr(rh, "_enforce_direct_repo_access", lambda alias, user: None)
    meta_world.delegated()
    meta_world.backend.fail = ConnectionError("down")
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(golden_repos_dir=str(meta_world.pod_golden))
        )
    )
    with pytest.raises(cmc.CidxMetaUnavailableError):
        asyncio.run(
            rh.get_repository_description(
                "repo", request, current_user=SimpleNamespace(username="u")
            )
        )
