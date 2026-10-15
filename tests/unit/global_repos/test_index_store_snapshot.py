"""IndexStore step 2 in refresh: publish the built index, snapshot a checked-out one.

Under ``index_store_backend='delegated'`` the index a refresh builds on a pod
without the repository is scratch; the version published to the artifact
service is the source of truth. Pinned, against a fake artifact service that
stores real directory trees:

* the snapshot readers are pointed at holds the PUBLISHED version, fetched back
  (not a copy of the scratch), in the canonical ``.versioned/<repo>/v_<ts>``
  layout, with ``cidx fix-config`` run on it;
* each publish builds on the store's current version, and the checkout asks
  for exactly the version this refresh published;
* any failure -- publish, checkout, fix-config, a missing index -- raises and
  leaves no snapshot behind;
* the local store keeps today's path (the snapshot manager's CoW copy);
* the store alias is safe for the artifact service for any cidx alias.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from code_indexer.global_repos import refresh_scheduler as rs
from code_indexer.utils.index_store import (
    INDEX_DIRNAME,
    DelegatedIndexStore,
    LocalIndexStore,
    index_store_alias,
)


class _NoRepoRegistry:
    def get_global_repo(self, alias: str) -> None:
        return None


class _Artifacts:
    """An artifact service holding real trees: alias -> [(version, dir)]."""

    is_local = False

    def __init__(self, root: Path) -> None:
        self.root = root
        self.versions: Dict[str, List[str]] = {}
        self.calls: List[tuple] = []
        self.fail_publish = False
        self.fail_checkout = False

    def current_version(self, alias: str) -> Optional[str]:
        history = self.versions.get(alias)
        return history[-1] if history else None

    def publish(self, alias: str, base_version: Optional[str], source_dir: str) -> str:
        self.calls.append(("publish", alias, base_version, source_dir))
        if self.fail_publish:
            raise OSError("artifact service unreachable")
        if base_version != self.current_version(alias):
            raise RuntimeError("ECONFLICT")
        version = f"v{len(self.versions.get(alias, [])) + 1:032d}"
        shutil.copytree(source_dir, self.root / alias / version)
        self.versions.setdefault(alias, []).append(version)
        return version

    def checkout(self, alias: str, dest_dir: str, version: Optional[str] = None) -> str:
        self.calls.append(("checkout", alias, dest_dir, version))
        if self.fail_checkout:
            raise RuntimeError("EDIGEST")
        chosen = version or self.current_version(alias)
        assert chosen is not None
        if Path(dest_dir).exists():
            raise RuntimeError("EINVAL: dest exists")
        shutil.copytree(self.root / alias / chosen, dest_dir)
        return chosen


class _SnapshotManager:
    uses_flexclone = False

    def __init__(self, base: Path) -> None:
        self.base = base

    def get_snapshot_path(self, alias: str, timestamp: str) -> str:
        return str(self.base / ".versioned" / alias / f"v_{timestamp}")

    def create_snapshot(self, alias: str, source_path: str) -> str:
        raise AssertionError("the delegated store must not CoW-copy the scratch index")


@pytest.fixture
def world(tmp_path: Path) -> Dict[str, Any]:
    golden = tmp_path / "golden-repos"
    source = golden / "agent-router"
    built = source / INDEX_DIRNAME
    (built / "index").mkdir(parents=True)
    (built / "index" / "hnsw.bin").write_bytes(b"built here")
    (built / "config.json").write_text("{}")
    artifacts = _Artifacts(tmp_path / "store")
    scheduler = rs.RefreshScheduler.__new__(rs.RefreshScheduler)
    scheduler._index_store = DelegatedIndexStore(artifacts)
    scheduler._snapshot_manager = _SnapshotManager(golden)
    scheduler.resource_config = None
    # fix-config is stamped from the repo's registry entry; none here.
    scheduler._registry = _NoRepoRegistry()
    fixes: List[Path] = []

    def fake_run(cmd: List[str], cwd: str, **kw: Any) -> None:
        assert cmd == ["cidx", "fix-config", "--force"]
        fixes.append(Path(cwd))

    scheduler._run_subprocess = fake_run
    return {
        "scheduler": scheduler,
        "artifacts": artifacts,
        "source": source,
        "built": built,
        "golden": golden,
        "fixes": fixes,
    }


def _snapshot(world: Dict[str, Any]) -> Path:
    return Path(
        world["scheduler"]._create_snapshot("agent-router-global", str(world["source"]))
    )


def test_the_snapshot_is_the_published_version_checked_out(
    world: Dict[str, Any],
) -> None:
    snapshot = _snapshot(world)

    assert snapshot.parent == world["golden"] / ".versioned" / "agent-router"
    assert re.fullmatch(r"v_\d+", snapshot.name)
    assert (
        snapshot / INDEX_DIRNAME / "index" / "hnsw.bin"
    ).read_bytes() == b"built here"
    assert world["fixes"] == [snapshot]

    publish, checkout = world["artifacts"].calls
    assert publish == ("publish", "agent-router", None, str(world["built"]))
    assert checkout[0] == "checkout" and checkout[1] == "agent-router"
    assert checkout[2] == str(snapshot / INDEX_DIRNAME)
    assert checkout[3] == world["artifacts"].current_version("agent-router"), (
        "checked out something other than the version this refresh published"
    )


def test_it_is_fetched_back_not_copied_from_the_scratch(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corrupt the scratch AFTER publishing: the snapshot must not see it."""
    artifacts: _Artifacts = world["artifacts"]
    real_publish = artifacts.publish

    def publish_then_clobber(alias: str, base: Optional[str], source_dir: str) -> str:
        version = real_publish(alias, base, source_dir)
        (Path(source_dir) / "index" / "hnsw.bin").write_bytes(b"scratch changed")
        return version

    monkeypatch.setattr(artifacts, "publish", publish_then_clobber)
    snapshot = _snapshot(world)
    assert (
        snapshot / INDEX_DIRNAME / "index" / "hnsw.bin"
    ).read_bytes() == b"built here"


def test_each_publish_builds_on_the_current_version(world: Dict[str, Any]) -> None:
    _snapshot(world)
    first = world["artifacts"].current_version("agent-router")
    _snapshot(world)
    publishes = [c for c in world["artifacts"].calls if c[0] == "publish"]
    assert [p[2] for p in publishes] == [None, first]


@pytest.mark.parametrize("failure", ["publish", "checkout", "fix-config", "no index"])
def test_a_failure_raises_and_leaves_no_snapshot(
    world: Dict[str, Any], failure: str
) -> None:
    if failure == "publish":
        world["artifacts"].fail_publish = True
    elif failure == "checkout":
        world["artifacts"].fail_checkout = True
    elif failure == "fix-config":

        def failing(cmd: List[str], cwd: str, **kw: Any) -> None:
            raise subprocess.CalledProcessError(1, cmd, stderr="bad config")

        world["scheduler"]._run_subprocess = failing
    else:
        shutil.rmtree(world["built"] / "index")

    with pytest.raises(RuntimeError):
        _snapshot(world)
    versioned = world["golden"] / ".versioned" / "agent-router"
    assert not versioned.exists() or list(versioned.iterdir()) == []


def test_a_flexclone_snapshot_manager_is_refused(world: Dict[str, Any]) -> None:
    world["scheduler"]._snapshot_manager.uses_flexclone = True
    with pytest.raises(RuntimeError, match="FlexClone"):
        _snapshot(world)
    assert world["artifacts"].calls == [], "published before refusing"


def test_two_snapshots_in_one_second_get_distinct_paths(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rs.time, "time", lambda: 1_760_000_000.0)
    assert _snapshot(world) != _snapshot(world)


def test_the_local_store_keeps_todays_cow_snapshot(world: Dict[str, Any]) -> None:
    scheduler = world["scheduler"]
    scheduler._index_store = LocalIndexStore()
    with pytest.raises(RuntimeError, match="must not CoW-copy"):
        _snapshot(world)  # reached the snapshot manager's create_snapshot
    assert world["artifacts"].calls == []


# ── the store alias ─────────────────────────────────────────────────────────

_STORE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


@pytest.mark.parametrize("name", ["agent-router", "a", "repo.v2", "x_y-z"])
def test_a_name_the_store_accepts_is_used_as_is(name: str) -> None:
    assert index_store_alias(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "MyRepo",
        "_leading",
        "-dash",
        "a..b",
        "Ünïcode",
        "x" * 200,
        "UPPER_" * 40,
        "..",
        "a/b",
    ],
)
def test_any_other_name_becomes_a_valid_store_alias(name: str) -> None:
    alias = index_store_alias(name)
    assert _STORE_RE.match(alias) and ".." not in alias, alias
    assert index_store_alias(name) == alias, "not deterministic"


def test_names_that_differ_only_in_case_get_distinct_aliases() -> None:
    assert (
        len({index_store_alias(n) for n in ["MyRepo", "myrepo", "MYREPO", "myRepo"]})
        == 4
    )
