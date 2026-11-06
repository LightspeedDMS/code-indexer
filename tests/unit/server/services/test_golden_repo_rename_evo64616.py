"""EVO-64616: renaming a golden repository's alias moves everything that
names it, without re-cloning or re-embedding, survives a crash at any step
(a re-run finishes it) and racing pods (one wins, the rest fail loud).

Fakes stand in for the stores; the clone directories are real (local) or a
recorded workspace-mcp (delegated).
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import pytest

from code_indexer.server.services import golden_repo_rename as rename_module
from code_indexer.server.services.golden_repo_rename import (
    RENAMED_FROM,
    RENAMING_TO,
    GoldenRepoRenamer,
    LocalCloneDirs,
    RenameConflictError,
    RenameDeps,
    RenameError,
    RenameInvalidError,
    RenameNotFoundError,
    names_in_rename,
    validate_alias,
)
from code_indexer.utils.index_store import (
    index_store_alias,
    temporal_index_store_alias,
)

OLD, NEW = "evolution-2", "conversion-evolution"
URL = "https://gitlab.example.com/ideas/innovation-week/evolution.git"
OTHER_URL = "https://gitlab.example.com/conversion/evolution.git"


class Boom(RuntimeError):
    """An injected crash."""


class Crashes:
    """Raises Boom once at the named step."""

    def __init__(self) -> None:
        self.at: Optional[str] = None

    def hit(self, step: str) -> None:
        if self.at == step:
            self.at = None
            raise Boom(step)


class FakeMetadata:
    def __init__(self, crashes: Crashes) -> None:
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.dep_invalidated: List[str] = []
        self.crashes = crashes

    def get_repo(self, alias: str) -> Optional[Dict[str, Any]]:
        row = self.rows.get(alias)
        return dict(row) if row else None

    def add_repo(self, **row: Any) -> None:
        self.crashes.hit("add_repo")
        assert row["alias"] not in self.rows
        row.setdefault("category_id", None)
        row.setdefault("wiki_enabled", False)
        self.rows[row["alias"]] = row

    def update_category(self, alias: str, category: Any, auto: bool) -> bool:
        self.rows[alias]["category_id"] = category
        self.rows[alias]["category_auto_assigned"] = auto
        return True

    def update_wiki_enabled(self, alias: str, enabled: bool) -> None:
        self.rows[alias]["wiki_enabled"] = enabled

    def remove_repo(self, alias: str) -> bool:
        self.crashes.hit("remove_repo")
        return self.rows.pop(alias, None) is not None

    def invalidate_dependency_map_tracking(self, alias: str) -> None:
        self.dep_invalidated.append(alias)


class FakeRegistry:
    def __init__(self, crashes: Crashes) -> None:
        self.repos: Dict[str, Dict[str, Any]] = {}
        self.crashes = crashes

    def get_global_repo(self, alias: str) -> Optional[Dict[str, Any]]:
        return self.repos.get(alias)

    def register_global_repo(self, **repo: Any) -> None:
        self.crashes.hit("register_global_repo")
        self.repos[repo["alias_name"]] = repo

    def unregister_global_repo(self, alias: str) -> None:
        self.repos.pop(alias, None)

    def update_enable_scip(self, alias: str, enabled: bool) -> None:
        self.repos[alias]["enable_scip"] = enabled


class FakeAliases:
    def __init__(self, crashes: Crashes) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.tombstones: Set[str] = set()
        self.crashes = crashes
        self.lock = threading.Lock()

    def read_record(self, name: str) -> Optional[Dict[str, Any]]:
        doc = self.docs.get(name)
        return dict(doc) if doc else None

    def update_fields_if(
        self, name: str, expected: Dict[str, Any], updates: Dict[str, Any]
    ) -> bool:
        with self.lock:
            doc = self.docs.get(name)
            if doc is None:
                return False
            doc.update(updates)
            return True

    def create_alias(
        self,
        name: str,
        target: str,
        repo_name: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.crashes.hit("create_alias")
        doc = {"target_path": target, "repo_name": repo_name}
        doc.update(extra or {})
        self.docs[name] = doc

    def delete_alias(self, name: str) -> None:
        # Like AliasManager.delete_alias: a failed delete is logged, not raised.
        if self.crashes.at == "delete_alias":
            self.crashes.at = None
            return
        self.docs.pop(name, None)
        self.tombstones.add(name)

    def list_records(self) -> Dict[str, Dict[str, Any]]:
        return {k: dict(v) for k, v in self.docs.items()}


class FakeStore:
    """A delegated IndexStore: versions per store alias."""

    is_local = False

    def __init__(self, crashes: Crashes) -> None:
        self.versions: Dict[str, List[str]] = {}
        self.crashes = crashes
        self.published: List[Tuple[str, Optional[str]]] = []
        self.busy: Set[str] = set()

    def current_version_as(self, alias: str) -> Optional[str]:
        versions = self.versions.get(alias)
        return versions[-1] if versions else None

    def checkout_as(
        self, alias: str, into: Path, version: Optional[str] = None
    ) -> Path:
        assert version in self.versions[alias]
        dest = Path(into) / ".code-indexer"
        dest.mkdir(parents=True)
        (dest / "version").write_text(f"{alias}@{version}")
        return dest

    def checkout_dir(
        self, alias: str, dest: Path, version: Optional[str] = None
    ) -> Path:
        assert version in self.versions[alias]
        Path(dest).mkdir(parents=True)
        (Path(dest) / "version").write_text(f"{alias}@{version}")
        return Path(dest)

    def publish_on(self, alias: str, built: Path, base: Optional[str]) -> str:
        self.crashes.hit("publish_on")
        assert (Path(built) / "version").exists(), "publishes the checked-out copy"
        assert base == self.current_version_as(alias)
        version = f"v{len(self.versions.get(alias, [])) + 1}-{alias}"
        self.versions.setdefault(alias, []).append(version)
        self.published.append((alias, (Path(built) / "version").read_text()))
        return version

    def delete_as(self, alias: str) -> int:
        if alias in self.busy:
            raise RuntimeError("ECONFLICT: checkout lease live")
        return len(self.versions.pop(alias, []))


class FakeDelegatedDirs:
    """workspace-mcp's clone-path: a copy may be interrupted half-way."""

    is_delegated = True

    def __init__(self, crashes: Crashes) -> None:
        self.dirs: Dict[str, str] = {}  # path -> content
        self.crashes = crashes
        self.copies = 0

    def exists(self, path: str) -> bool:
        return path in self.dirs

    def move(self, source: str, dest: str) -> None:
        assert dest not in self.dirs, "/clone-path refuses an existing dest"
        self.copies += 1
        if self.crashes.at == "copy":
            self.crashes.at = None
            self.dirs[dest] = "PARTIAL"
            raise Boom("copy")
        self.dirs[dest] = self.dirs[source]

    def remove(self, path: str) -> bool:
        return self.dirs.pop(path, None) is not None


class FakeGit:
    def __init__(self, origin: str = URL, branch: str = "development", ahead: int = 0):
        self.origin, self.branch, self.ahead = origin, branch, ahead
        self.paths: List[str] = []

    def remote_get_url(self, path: str, name: str, timeout: Any = None) -> str:
        self.paths.append(path)
        return self.origin

    def symbolic_ref(self, path: str, short: bool = True, timeout: Any = None) -> str:
        return self.branch

    def rev_parse(self, path: str, *args: str, timeout: Any = None) -> str:
        return "0" * 40

    def rev_list_count(self, path: str, spec: str, timeout: Any = None) -> int:
        assert spec == f"refs/remotes/origin/{self.branch}..HEAD"
        return self.ahead


class FakeScheduler:
    """Write locks shared by every 'pod' of a test."""

    def __init__(self) -> None:
        self.locks: Dict[str, str] = {}
        self.guard = threading.Lock()
        self.refreshing: Set[str] = set()

    def acquire_write_lock(self, alias: str, owner_name: str) -> bool:
        with self.guard:
            if alias in self.locks:
                return False
            self.locks[alias] = owner_name
            return True

    def release_write_lock(self, alias: str, owner_name: str) -> None:
        with self.guard:
            self.locks.pop(alias, None)

    def check_refresh_not_in_progress(self, alias: str) -> None:
        if alias in self.refreshing:
            raise RuntimeError(f"refresh of {alias} running")


class FakeTracker:
    def __init__(self) -> None:
        self.active: Dict[Tuple[str, str], str] = {}

    def active_job_id(self, operation: str, alias: str) -> Optional[str]:
        return self.active.get((operation, alias))


class World:
    """One golden repo OLD, globally active, with satellites recorded."""

    def __init__(self, tmp_path: Path, delegated: bool) -> None:
        self.crashes = Crashes()
        self.golden = str(tmp_path / "golden-repos")
        os.makedirs(self.golden)
        self.metadata = FakeMetadata(self.crashes)
        self.registry = FakeRegistry(self.crashes)
        self.aliases = FakeAliases(self.crashes)
        self.scheduler = FakeScheduler()
        self.tracker = FakeTracker()
        self.git = FakeGit()
        self.satellite_calls: List[Tuple[str, str, str]] = []
        self.write_mode: Set[str] = set()
        old_dir = os.path.join(self.golden, OLD)
        snapshot = os.path.join(self.golden, ".versioned", OLD, "v_100")
        self.store: Optional[FakeStore] = None
        if delegated:
            self.dirs: Any = FakeDelegatedDirs(self.crashes)
            self.dirs.dirs[old_dir] = "CLONE"
            self.store = FakeStore(self.crashes)
            self.store.versions[index_store_alias(OLD)] = ["v7"]
            self.store.versions[temporal_index_store_alias(OLD)] = ["t3"]
            target = old_dir
            index_fields = {
                "index_store_alias": index_store_alias(OLD),
                "index_version": "v7",
                "temporal_index_store_alias": temporal_index_store_alias(OLD),
                "temporal_index_version": "t3",
            }
        else:
            self.dirs = LocalCloneDirs()
            os.makedirs(os.path.join(old_dir, ".code-indexer"))
            Path(old_dir, "marker").write_text("CLONE")
            target = snapshot
            index_fields = {}
        self.metadata.rows[OLD] = {
            "alias": OLD,
            "repo_url": URL,
            "default_branch": "development",
            "clone_path": old_dir,
            "created_at": "2026-01-01T00:00:00Z",
            "enable_temporal": True,
            "temporal_options": {"max_commits": 10},
            "category_id": 4,
            "category_auto_assigned": False,
            "wiki_enabled": True,
            "workspace_id": None,
        }
        self.registry.repos[f"{OLD}-global"] = {
            "alias_name": f"{OLD}-global",
            "repo_name": OLD,
            "repo_url": URL,
            "index_path": target,
            "enable_temporal": True,
            "temporal_options": {"max_commits": 10},
            "enable_scip": True,
        }
        self.aliases.docs[f"{OLD}-global"] = {
            "target_path": target,
            "previous_path": os.path.join(self.golden, ".versioned", OLD, "v_50"),
            "repo_name": OLD,
            "last_refresh": "2026-02-02",
            **index_fields,
        }

    def satellite(self, name: str) -> Callable[[str, str, Optional[str]], str]:
        def move(old: str, new: str, url: Optional[str]) -> str:
            self.crashes.hit(f"satellite:{name}")
            self.satellite_calls.append((name, old, new))
            return "ok"

        return move

    def deps(self) -> RenameDeps:
        return RenameDeps(
            golden_repos_dir=self.golden,
            metadata=self.metadata,
            registry=self.registry,
            aliases=self.aliases,
            clone_dirs=self.dirs,
            git=self.git,
            index_store=self.store,
            scheduler=self.scheduler,
            job_tracker=self.tracker,
            write_mode_active=lambda alias: alias in self.write_mode,
            satellites=[
                ("activated_repos", self.satellite("activated_repos")),
                ("group_access", self.satellite("group_access")),
            ],
            cache={OLD: object()},
        )

    def rename(
        self,
        new: str = NEW,
        repo_url: Optional[str] = None,
        branch: Optional[str] = None,
    ) -> Dict[str, Any]:
        renamer = GoldenRepoRenamer(self.deps())
        plan = renamer.plan(OLD, new, repo_url, branch)
        with renamer.locked(plan):
            plan = renamer.plan(OLD, new, repo_url, branch)
            renamer.check_not_busy(plan, own_job=None)
            return renamer.execute(plan)

    def dir(self, alias: str) -> str:
        return os.path.join(self.golden, alias)

    def assert_renamed(self) -> None:
        old_dir, new_dir = self.dir(OLD), self.dir(NEW)
        assert OLD not in self.metadata.rows
        row = self.metadata.rows[NEW]
        assert row["clone_path"] == new_dir
        assert (row["category_id"], row["wiki_enabled"]) == (4, True)
        assert row["temporal_options"] == {"max_commits": 10}
        assert f"{OLD}-global" not in self.registry.repos
        assert self.registry.repos[f"{NEW}-global"]["enable_scip"] is True
        assert f"{OLD}-global" not in self.aliases.docs
        assert f"{OLD}-global" in self.aliases.tombstones
        pointer = self.aliases.docs[f"{NEW}-global"]
        assert pointer[RENAMED_FROM] == OLD and RENAMING_TO not in pointer
        assert pointer["repo_name"] == NEW and pointer["last_refresh"] == "2026-02-02"
        assert not self.dirs.exists(old_dir) and self.dirs.exists(new_dir)
        assert names_in_rename(self.aliases) == set()
        assert self.scheduler.locks == {}
        assert {(n, o, w) for n, o, w in self.satellite_calls} == {
            ("activated_repos", OLD, NEW),
            ("group_access", OLD, NEW),
        }


# -- local -----------------------------------------------------------------


def test_local_rename_moves_everything(tmp_path: Path) -> None:
    world = World(tmp_path, delegated=False)
    result = world.rename()

    world.assert_renamed()
    assert Path(world.dir(NEW), "marker").read_text() == "CLONE"
    pointer = world.aliases.docs[f"{NEW}-global"]
    # a published snapshot path is opaque: kept
    assert pointer["target_path"].endswith(f".versioned/{OLD}/v_100")
    assert world.registry.repos[f"{NEW}-global"]["index_path"] == pointer["target_path"]
    assert result["leftovers"] == []
    assert "index_store_alias" not in pointer


# -- delegated ---------------------------------------------------------------


def test_delegated_rename_republishes_and_copies(tmp_path: Path) -> None:
    world = World(tmp_path, delegated=True)
    assert world.store is not None
    world.rename()

    world.assert_renamed()
    pointer = world.aliases.docs[f"{NEW}-global"]
    assert pointer["target_path"] == world.dir(NEW), "base clone target rewritten"
    assert pointer["index_store_alias"] == index_store_alias(NEW)
    assert pointer["temporal_index_store_alias"] == temporal_index_store_alias(NEW)
    assert pointer["index_version"] == world.store.current_version_as(
        index_store_alias(NEW)
    )
    assert sorted(world.store.published) == sorted(
        [
            (index_store_alias(NEW), f"{index_store_alias(OLD)}@v7"),
            (temporal_index_store_alias(NEW), f"{temporal_index_store_alias(OLD)}@t3"),
        ]
    ), "the existing versions are copied, never rebuilt"
    assert index_store_alias(OLD) not in world.store.versions
    assert temporal_index_store_alias(OLD) not in world.store.versions
    assert world.dirs.dirs[world.dir(NEW)] == "CLONE"


def test_a_busy_old_index_alias_is_a_reported_leftover(tmp_path: Path) -> None:
    world = World(tmp_path, delegated=True)
    assert world.store is not None
    world.store.busy.add(index_store_alias(OLD))
    result = world.rename()
    world.assert_renamed()
    assert len(result["leftovers"]) == 1 and "ECONFLICT" in result["leftovers"][0]


# -- crash at any step: a re-run finishes it ---------------------------------

STEPS = [
    "publish_on",
    "copy",
    "create_alias",
    "add_repo",
    "register_global_repo",
    "remove_repo",
    "delete_alias",
]


@pytest.mark.parametrize("step", STEPS)
def test_a_crash_midway_is_finished_by_a_rerun(tmp_path: Path, step: str) -> None:
    world = World(tmp_path, delegated=True)
    world.crashes.at = step
    # a pointer delete that fails is caught by reading it back
    with pytest.raises(RenameError if step == "delete_alias" else Boom):
        world.rename()
    world.scheduler.locks.clear()  # the crashed pod's locks expire
    world.rename()
    world.assert_renamed()
    assert world.dirs.dirs[world.dir(NEW)] == "CLONE", "never a partial copy"
    if step in ("register_global_repo", "remove_repo", "delete_alias"):
        # the new row names the copy: it is never copied again
        assert world.dirs.copies == 1


@pytest.mark.parametrize("step", ["create_alias", "add_repo", "remove_repo"])
def test_a_local_crash_midway_is_finished_by_a_rerun(tmp_path: Path, step: str) -> None:
    world = World(tmp_path, delegated=False)
    world.crashes.at = step
    with pytest.raises(Boom):
        world.rename()
    world.scheduler.locks.clear()
    world.rename()
    world.assert_renamed()


def test_a_crash_leaves_both_names_off_limits_to_the_reconcile(tmp_path: Path) -> None:
    world = World(tmp_path, delegated=True)
    world.crashes.at = "create_alias"
    with pytest.raises(Boom):
        world.rename()
    assert names_in_rename(world.aliases) == {OLD, NEW}


def test_a_satellite_failure_is_reported_and_rerun_retries_it(tmp_path: Path) -> None:
    world = World(tmp_path, delegated=False)
    deps = world.deps()
    deps.satellites.append(("broken", lambda o, n, u: (_ for _ in ()).throw(Boom())))
    renamer = GoldenRepoRenamer(deps)
    result = renamer.execute(renamer.plan(OLD, NEW))
    assert result["satellites"]["broken"].startswith("failed: Boom")
    # the rename is done; a re-run (the resume form) runs the satellites again
    plan = renamer.plan(OLD, NEW)
    assert plan.resume is True
    assert renamer.execute(plan)["satellites"]["broken"].startswith("failed")


# -- racing pods ----------------------------------------------------------------


def test_six_racing_pods_one_rename_wins_and_the_rest_fail_loud(
    tmp_path: Path,
) -> None:
    world = World(tmp_path, delegated=True)
    barrier = threading.Barrier(6)
    outcomes: List[str] = []
    guard = threading.Lock()

    def pod() -> None:
        barrier.wait()
        try:
            world.rename()
            outcome = "renamed"
        except RenameError as exc:
            outcome = type(exc).__name__
        with guard:
            outcomes.append(outcome)

    threads = [threading.Thread(target=pod) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("renamed") >= 1
    assert all(
        o in ("renamed", "RenameConflictError", "RenameNotFoundError") for o in outcomes
    )
    world.assert_renamed()
    assert world.dirs.copies == 1, "the clone was copied once"


def test_two_renames_to_one_new_alias_cannot_both_win(tmp_path: Path) -> None:
    world = World(tmp_path, delegated=False)
    world.scheduler.locks[NEW] = "rename_repo"  # another rename holds the target
    with pytest.raises(RenameConflictError, match="write lock"):
        world.rename()
    assert OLD in world.metadata.rows and world.scheduler.locks == {NEW: "rename_repo"}


# -- refusals -----------------------------------------------------------------


@pytest.mark.parametrize(
    "arrange, error, text",
    [
        (lambda w: w.metadata.rows.pop(OLD), RenameNotFoundError, "not found"),
        (
            lambda w: w.metadata.rows.__setitem__(NEW, {"alias": NEW}),
            RenameConflictError,
            "already exists",
        ),
        (
            lambda w: w.aliases.docs.__setitem__(f"{NEW}-global", {"target_path": "x"}),
            RenameConflictError,
            "already exists",
        ),
        (
            lambda w: os.makedirs(w.dir(NEW)),
            RenameConflictError,
            "is not this rename's",
        ),
        (
            lambda w: w.aliases.docs.pop(f"{OLD}-global"),
            RenameConflictError,
            "not globally active",
        ),
        (
            lambda w: w.metadata.rows[OLD].__setitem__("repo_url", "local://x"),
            RenameInvalidError,
            "local://",
        ),
        (
            lambda w: w.metadata.rows[OLD].__setitem__("clone_path", "/elsewhere"),
            RenameInvalidError,
            "own golden-repos entry",
        ),
    ],
)
def test_refusals(
    tmp_path: Path, arrange: Callable[[World], Any], error: type, text: str
) -> None:
    world = World(tmp_path, delegated=False)
    arrange(world)
    with pytest.raises(error, match=text):
        world.rename()


@pytest.mark.parametrize("alias", ["", "a/b", "..", "x-global", "-x", "a b"])
def test_invalid_aliases_are_refused(alias: str) -> None:
    with pytest.raises(RenameInvalidError):
        validate_alias(alias)


def test_the_same_alias_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RenameInvalidError):
        World(tmp_path, delegated=False).rename(new=OLD)


@pytest.mark.parametrize("busy_alias", [OLD, NEW])
@pytest.mark.parametrize("operation", rename_module.CONFLICTING_OPERATIONS)
def test_an_in_flight_job_of_either_alias_is_refused(
    tmp_path: Path, busy_alias: str, operation: str
) -> None:
    world = World(tmp_path, delegated=False)
    world.tracker.active[(operation, busy_alias)] = "job-1"
    with pytest.raises(RenameConflictError, match=operation):
        world.rename()
    assert OLD in world.metadata.rows


@pytest.mark.parametrize("busy_alias", [OLD, NEW])
def test_a_running_refresh_of_either_alias_is_refused(
    tmp_path: Path, busy_alias: str
) -> None:
    world = World(tmp_path, delegated=False)
    world.scheduler.refreshing.add(busy_alias)
    with pytest.raises(RenameConflictError, match="refresh"):
        world.rename()


def test_write_mode_is_refused(tmp_path: Path) -> None:
    world = World(tmp_path, delegated=False)
    world.write_mode.add(OLD)
    with pytest.raises(RenameConflictError, match="write-mode"):
        world.rename()


# -- correcting a mis-registered row -------------------------------------------


def test_a_corrected_url_and_branch_matching_the_clone_are_recorded(
    tmp_path: Path,
) -> None:
    world = World(tmp_path, delegated=False)
    world.git = FakeGit(origin=OTHER_URL, branch="master")
    world.rename(repo_url=OTHER_URL, branch="master")
    row = world.metadata.rows[NEW]
    assert (row["repo_url"], row["default_branch"]) == (OTHER_URL, "master")
    assert world.registry.repos[f"{NEW}-global"]["repo_url"] == OTHER_URL


@pytest.mark.parametrize(
    "git, url, branch, text",
    [
        (FakeGit(origin=URL, branch="master"), OTHER_URL, "master", "fetches from"),
        (FakeGit(origin=OTHER_URL, branch="development"), OTHER_URL, "master", "is on"),
        (
            FakeGit(origin=OTHER_URL, branch="master", ahead=2),
            OTHER_URL,
            "master",
            "2 commit",
        ),
        (FakeGit(), "not a url", None, "not a git remote URL"),
        (FakeGit(), "file:///somewhere/else", None, "must name the renamed"),
    ],
)
def test_a_correction_that_does_not_match_the_clone_is_refused(
    tmp_path: Path, git: FakeGit, url: str, branch: Optional[str], text: str
) -> None:
    world = World(tmp_path, delegated=False)
    world.git = git
    with pytest.raises(RenameInvalidError, match=text):
        world.rename(repo_url=url, branch=branch)
    assert OLD in world.metadata.rows


def test_a_file_url_naming_the_new_directory_is_accepted(tmp_path: Path) -> None:
    world = World(tmp_path, delegated=False)
    url = f"file://{world.dir(NEW)}"
    world.rename(repo_url=url)
    assert world.metadata.rows[NEW]["repo_url"] == url


def test_a_detached_head_is_refused(tmp_path: Path) -> None:
    world = World(tmp_path, delegated=False)

    class Detached(FakeGit):
        def symbolic_ref(
            self, path: str, short: bool = True, timeout: Any = None
        ) -> str:
            raise subprocess.CalledProcessError(
                128, ["git"], stderr="fatal: ref HEAD is not a symbolic ref"
            )

    world.git = Detached()
    with pytest.raises(RenameInvalidError, match="detached"):
        world.rename(branch="development")


# -- reconcile guard -------------------------------------------------------------


def test_names_in_rename_lists_both_ends_of_a_marked_pointer() -> None:
    aliases = FakeAliases(Crashes())
    aliases.docs = {
        "a-global": {RENAMING_TO: "b"},
        "c-global": {"target_path": "x"},
    }
    assert names_in_rename(aliases) == {"a", "b"}


class ProcessDied(BaseException):
    """The pod dies: not an Exception, so no step catches it."""


def test_a_crash_after_the_pointer_is_gone_still_cleans_the_old_store_aliases(
    tmp_path: Path,
) -> None:
    world = World(tmp_path, delegated=True)
    assert world.store is not None
    deps = world.deps()
    died = {"once": True}

    def dies(old: str, new: str, url: Optional[str]) -> str:
        if died.pop("once", False):
            raise ProcessDied()
        return "ok"

    deps.satellites.insert(0, ("dies", dies))
    renamer = GoldenRepoRenamer(deps)
    with pytest.raises(ProcessDied):
        renamer.execute(renamer.plan(OLD, NEW))
    assert f"{OLD}-global" not in world.aliases.docs, "pointer already deleted"
    assert index_store_alias(OLD) in world.store.versions, "not yet cleaned"

    plan = renamer.plan(OLD, NEW)
    assert plan.resume is True
    renamer.execute(plan)
    world.assert_renamed()
    assert index_store_alias(OLD) not in world.store.versions
    assert temporal_index_store_alias(OLD) not in world.store.versions
    assert world.store.current_version_as(index_store_alias(NEW)) is not None


def test_a_local_rename_resumed_with_a_url_and_branch_probes_the_moved_clone(
    tmp_path: Path,
) -> None:
    world = World(tmp_path, delegated=False)
    world.git = FakeGit(origin=OTHER_URL, branch="master")
    world.crashes.at = "add_repo"  # after the local move and the new pointer
    with pytest.raises(Boom):
        world.rename(repo_url=OTHER_URL, branch="master")
    assert not os.path.exists(world.dir(OLD)) and os.path.isdir(world.dir(NEW))
    world.scheduler.locks.clear()
    world.git.paths.clear()
    world.rename(repo_url=OTHER_URL, branch="master")
    world.assert_renamed()
    assert world.metadata.rows[NEW]["repo_url"] == OTHER_URL
    assert set(world.git.paths) == {world.dir(NEW)}


def test_a_clone_that_cannot_be_read_refuses_with_a_conflict(tmp_path: Path) -> None:
    world = World(tmp_path, delegated=False)

    class Broken(FakeGit):
        def remote_get_url(self, path: str, name: str, timeout: Any = None) -> str:
            raise subprocess.CalledProcessError(128, ["git"], stderr="fatal")

    world.git = Broken()
    with pytest.raises(RenameConflictError, match="could not be read"):
        world.rename(repo_url=URL, branch="development")


def test_an_empty_clone_can_be_renamed_in_place(tmp_path: Path) -> None:
    """EVO-64616: an empty clone (nothing pushed upstream) has no commit and no
    origin tracking ref; counting commits ahead failed, refusing the rename."""
    world = World(tmp_path, delegated=False)

    class Empty(FakeGit):
        def rev_parse(self, path: str, *args: str, timeout: Any = None) -> str:
            raise subprocess.CalledProcessError(
                128,
                ["git"],
                stderr="fatal: ambiguous argument 'HEAD': unknown revision",
            )

        def rev_list_count(self, path: str, spec: str, timeout: Any = None) -> int:
            raise subprocess.CalledProcessError(
                128, ["git"], stderr=f"fatal: ambiguous argument '{spec}'"
            )

    world.git = Empty(branch="master")
    url = f"file://{world.dir(NEW)}"
    world.rename(repo_url=url, branch="master")
    world.assert_renamed()
    assert world.metadata.rows[NEW]["repo_url"] == url


def test_an_unreadable_clone_reports_and_logs_gits_own_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    world = World(tmp_path, delegated=False)

    class Broken(FakeGit):
        def remote_get_url(self, path: str, name: str, timeout: Any = None) -> str:
            raise subprocess.CalledProcessError(
                128,
                ["git"],
                stderr="fatal: unable to access 'https://u:s3cret@h/x.git': 403",
            )

    world.git = Broken()
    with caplog.at_level("WARNING"):
        with pytest.raises(RenameConflictError, match="unable to access") as raised:
            world.rename(repo_url=URL, branch="development")
    assert "s3cret" not in str(raised.value)
    logged = [
        r.getMessage() for r in caplog.records if "reading the clone" in r.getMessage()
    ]
    assert logged and "unable to access" in logged[0] and "s3cret" not in logged[0]


def test_an_unwired_clone_backend_points_at_the_startup_failure(
    tmp_path: Path,
) -> None:
    """EVO-64616: a pod whose VersionedSnapshotManager failed to build at startup
    (workspace-mcp unreachable then) refused renames with only "NoneType"."""
    from code_indexer.server.repositories.golden_repo_manager import (
        GitOperationError,
        GoldenRepoManager,
    )

    manager = GoldenRepoManager(data_dir=str(tmp_path / "data"))
    manager._snapshot_manager = None
    with pytest.raises(GitOperationError, match="APP-GENERAL-510"):
        manager._delegated_working_copies("rename of a golden repository")
