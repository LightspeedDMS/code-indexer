"""EVO-64616: the rename's satellites (activated repos, group grants, the
cidx-meta description, dependency tracking), the job submission, the
registry reconcile's guard and the snapshot reclaim's guard."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple, cast

import pytest

from code_indexer.global_repos import meta_description_hook as hook
from code_indexer.server.services import golden_repo_rename as rename_module
from code_indexer.server.services.golden_repo_rename import (
    RENAMING_TO,
    RenameConflictError,
    move_group_grants,
    relink_activated_repos,
    reset_dependency_tracking,
)
from code_indexer.server.services.versioned_snapshot_reconciler import (
    namespace_paths_referenced,
)

# -- activated repos and composites ---------------------------------------------


class FakeActivated:
    def __init__(self) -> None:
        self.meta: Dict[Tuple[str, str], Dict[str, Any]] = {
            ("ann", "mine"): {"golden_repo_alias": "old", "user_alias": "mine"},
            ("bob", "combo"): {
                "is_composite": True,
                "golden_repo_aliases": ["a", "old", "b"],
                "user_alias": "combo",
            },
            ("cy", "other"): {"golden_repo_alias": "else", "user_alias": "other"},
        }
        self.saved: List[Tuple[str, str]] = []

    def find_repos_by_golden_alias(self, alias: str) -> List[Dict[str, Any]]:
        found = []
        for (user, user_alias), meta in self.meta.items():
            if meta.get("golden_repo_alias") == alias or alias in meta.get(
                "golden_repo_aliases", []
            ):
                found.append({"username": user, "user_alias": user_alias})
        return found

    def _load_metadata(self, user: str, user_alias: str) -> Optional[Dict[str, Any]]:
        return dict(self.meta[(user, user_alias)])

    def _save_metadata(self, user: str, user_alias: str, meta: Dict[str, Any]) -> None:
        self.meta[(user, user_alias)] = meta
        self.saved.append((user, user_alias))


def test_activated_repos_and_composite_members_are_relinked() -> None:
    activated = FakeActivated()
    assert relink_activated_repos(activated)("old", "new", None) == "2 relinked"
    assert activated.meta[("ann", "mine")]["golden_repo_alias"] == "new"
    assert activated.meta[("bob", "combo")]["golden_repo_aliases"] == ["a", "new", "b"]
    assert activated.meta[("cy", "other")]["golden_repo_alias"] == "else"
    # idempotent: a re-run finds nothing left to relink
    assert relink_activated_repos(activated)("old", "new", None) == "0 relinked"


# -- group grants ------------------------------------------------------------------


class FakeGroups:
    def __init__(self) -> None:
        self.grants: Set[Tuple[str, int]] = {("old", 1), ("old", 2), ("x", 1)}

    def get_repo_groups(self, repo: str) -> List[Any]:
        return [SimpleNamespace(id=g) for r, g in sorted(self.grants) if r == repo]

    def grant_repo_access(self, repo: str, group_id: int, granted_by: str) -> bool:
        self.grants.add((repo, group_id))
        return True

    def revoke_repo_access(self, repo: str, group_id: int) -> bool:
        self.grants.discard((repo, group_id))
        return True


def test_group_grants_move_to_the_new_alias() -> None:
    groups = FakeGroups()
    assert move_group_grants(groups)("old", "new", None) == "2 group(s)"
    assert groups.grants == {("new", 1), ("new", 2), ("x", 1)}
    assert move_group_grants(groups)("old", "new", None) == "0 group(s)"


def test_dependency_tracking_is_reset_for_both_names() -> None:
    calls: List[str] = []
    metadata = SimpleNamespace(invalidate_dependency_map_tracking=calls.append)
    reset_dependency_tracking(metadata)("old", "new", None)
    assert calls == ["old", "new"]


# -- the cidx-meta description ------------------------------------------------------

DESCRIPTION = """---
name: evolution-2
url: https://gitlab.example.com/ideas/innovation-week/evolution.git
technologies: [python]
---

# evolution-2

The evolution service.

**Repository URL**: https://gitlab.example.com/ideas/innovation-week/evolution.git
"""


def test_the_description_names_the_new_alias_and_url() -> None:
    text = hook.renamed_description(
        DESCRIPTION,
        "evolution-2",
        "conversion-evolution",
        "https://user:secret@gitlab.example.com/conversion/evolution.git",
    )
    assert "name: conversion-evolution" in text
    assert "\n# conversion-evolution\n" in text
    assert "url: https://***@gitlab.example.com/conversion/evolution.git" in text or (
        "url: https://gitlab.example.com/conversion/evolution.git" in text
    )
    assert "secret" not in text
    assert "The evolution service." in text
    assert "innovation-week" not in text


def test_without_a_new_url_only_the_name_changes() -> None:
    text = hook.renamed_description(DESCRIPTION, "evolution-2", "c-e", None)
    assert text.count("innovation-week") == 2
    assert "name: c-e" in text and "# c-e" in text


class FakeTracking:
    def __init__(self) -> None:
        self.records: Dict[str, Dict[str, Any]] = {
            "old": {"repo_alias": "old", "last_run": "t1", "status": "done"}
        }

    def get_tracking_record(self, alias: str) -> Optional[Dict[str, Any]]:
        return self.records.get(alias)

    def upsert_tracking(self, alias: str, **fields: Any) -> None:
        self.records[alias] = {"repo_alias": alias, **fields}

    def delete_tracking(self, alias: str) -> bool:
        return self.records.pop(alias, None) is not None


def test_on_repo_renamed_moves_the_file_and_its_tracking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = tmp_path / "cidx-meta"
    meta.mkdir()
    (meta / "old.md").write_text(DESCRIPTION.replace("evolution-2", "old"))
    tracking = FakeTracking()
    monkeypatch.setattr(hook, "_tracking_backend", tracking)
    monkeypatch.setattr(hook, "_refresh_scheduler", None)
    monkeypatch.setattr(hook.cidx_meta_content, "shared_cidx_meta", lambda p: None)

    assert hook.on_repo_renamed("old", "new", str(tmp_path)) == "moved to new.md"
    assert not (meta / "old.md").exists()
    assert "name: new" in (meta / "new.md").read_text()
    assert tracking.records == {
        "new": {"repo_alias": "new", "last_run": "t1", "status": "done"}
    }
    # idempotent
    assert hook.on_repo_renamed("old", "new", str(tmp_path)) == "no description"


def test_on_repo_renamed_refuses_without_the_cidx_meta_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = tmp_path / "cidx-meta"
    meta.mkdir()
    (meta / "old.md").write_text(DESCRIPTION)
    scheduler = SimpleNamespace(acquire_write_lock=lambda *a, **k: False)
    monkeypatch.setattr(hook, "_refresh_scheduler", scheduler)
    monkeypatch.setattr(hook, "_LOCK_ACQUIRE_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(hook, "_describe_cidx_meta_holder", lambda s: "held")
    monkeypatch.setattr(hook.cidx_meta_content, "shared_cidx_meta", lambda p: None)
    with pytest.raises(RuntimeError, match="lock"):
        hook.on_repo_renamed("old", "new", str(tmp_path))
    assert (meta / "old.md").exists()


# -- the job submission -----------------------------------------------------------


class FakeJobs:
    def __init__(self) -> None:
        self.submitted: List[Dict[str, Any]] = []

    def submit_job(self, **job: Any) -> str:
        self.submitted.append(job)
        return "job-9"


def test_submit_checks_first_then_runs_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    events: List[str] = []

    class Renamer:
        def __init__(self, deps: Any) -> None:
            pass

        def plan(self, old: str, new: str, url: Any, branch: Any) -> Any:
            events.append("plan")
            return rename_module.RenamePlan(old, new, "u", "b")

        def check_not_busy(self, plan: Any, own_job: Any) -> None:
            events.append(f"busy:{own_job}")

        def locked(self, plan: Any) -> Any:
            class Locks:
                def __enter__(self) -> None:
                    events.append("lock")

                def __exit__(self, *a: Any) -> None:
                    events.append("unlock")

            return Locks()

        def execute(self, plan: Any) -> Dict[str, Any]:
            events.append("execute")
            return {"done": True}

    monkeypatch.setattr(rename_module, "GoldenRepoRenamer", Renamer)
    monkeypatch.setattr(rename_module, "deps_from_manager", lambda m: None)
    jobs = FakeJobs()
    manager = SimpleNamespace(
        background_job_manager=jobs,
        job_tracker=SimpleNamespace(active_job_id=lambda op, alias: "job-9"),
    )
    job_id = rename_module.submit_rename(
        manager, "old", "new", repo_url=None, branch=None, submitter_username="adm"
    )
    assert job_id == "job-9"
    assert events == ["plan", "busy:None"], "refusals happen before the job"
    job = jobs.submitted[0]
    assert (job["operation_type"], job["repo_alias"]) == ("rename_golden_repo", "old")
    result = job["func"]()
    assert events[2:] == ["lock", "plan", "busy:job-9", "execute", "unlock"]
    assert result["success"] is True and result["alias"] == "new"


def test_submit_refuses_in_maintenance_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from code_indexer.server.jobs.exceptions import MaintenanceModeError
    from code_indexer.server.services import maintenance_service

    monkeypatch.setattr(
        maintenance_service,
        "get_maintenance_state",
        lambda: SimpleNamespace(is_maintenance_mode=lambda: True),
    )
    with pytest.raises(MaintenanceModeError):
        rename_module.submit_rename(
            SimpleNamespace(),
            "a",
            "b",
            repo_url=None,
            branch=None,
            submitter_username="x",
        )


def test_a_locked_rename_releases_what_it_took() -> None:
    held: Dict[str, str] = {"b": "other"}
    released: List[str] = []

    class Scheduler:
        def acquire_write_lock(self, alias: str, owner_name: str) -> bool:
            if alias in held:
                return False
            held[alias] = owner_name
            return True

        def release_write_lock(self, alias: str, owner_name: str) -> None:
            released.append(alias)
            held.pop(alias, None)

    with pytest.raises(RenameConflictError):
        with rename_module._Locks(Scheduler(), ["a", "b"]):
            pass
    assert released == ["a"] and held == {"b": "other"}


# -- the registry reconcile leaves a rename's directories alone ------------------------


def test_the_reconcile_skips_both_ends_of_a_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import code_indexer.global_repos.alias_manager as alias_module
    from code_indexer.server.repositories.golden_repo_manager import (
        GoldenRepoManager,
    )

    class Aliases:
        def __init__(self, path: str) -> None:
            pass

        def list_records(self) -> Dict[str, Dict[str, Any]]:
            return {"old-global": {RENAMING_TO: "new"}}

    monkeypatch.setattr(alias_module, "AliasManager", Aliases)
    manager = object.__new__(GoldenRepoManager)
    manager.golden_repos_dir = str(tmp_path)
    counts = {"errors": 0, "skipped_in_flight": 0}
    assert manager._directory_in_rename("new", counts) is True
    assert manager._directory_in_rename("old", counts) is True
    assert manager._directory_in_rename("other", counts) is False
    assert counts == {"errors": 0, "skipped_in_flight": 2}


# -- the snapshot reclaim never deletes a renamed alias's served snapshots ---------------


def test_a_namespace_named_by_another_pointer_is_referenced(tmp_path: Path) -> None:
    golden = tmp_path / "golden-repos"
    served = str(golden / ".versioned" / "old" / "v_100")
    referenced = {served, str(golden / ".versioned" / "older" / "v_1")}
    assert namespace_paths_referenced("old", golden, referenced) == [served]
    assert namespace_paths_referenced("gone", golden, referenced) == []


def test_the_reclaim_keeps_a_namespace_another_pointer_serves(tmp_path: Path) -> None:
    from code_indexer.server.services import versioned_snapshot_reconciler as rec

    golden = tmp_path / "golden-repos"
    served = golden / ".versioned" / "old" / "v_100"
    served.mkdir(parents=True)
    result = rec.VersionedSnapshotReconcileResult()
    rec._reconcile_one_namespace_body(
        "old",
        golden_repos_path=golden,
        snapshot_manager=cast(Any, None),
        cleanup_manager=cast(Any, None),
        pointers={"new-global": (str(served), None)},
        referenced_paths={str(served)},
        keep_last=1,
        min_absolute_age_seconds=0,
        registered_aliases=set(),
        result=result,
    )
    assert "old" in result.skipped_namespaces
    assert result.scheduled_paths == [] and result.reclaimed_namespaces == []


# -- the delegated move: /clone-path/move, else copy then delete ---------------------


class FakeWorkingCopies:
    def __init__(self, has_move: bool) -> None:
        self.has_move = has_move
        self.calls: List[Tuple[str, str, str]] = []

    def move(self, source: str, dest: str, *, timeout: Any = None) -> bool:
        self.calls.append(("move", source, dest))
        return self.has_move

    def clone(self, source: str, dest: str, *, timeout: Any, cancel_check: Any) -> str:
        self.calls.append(("clone", source, dest))
        return dest


def test_the_delegated_move_uses_clone_path_move_when_available() -> None:
    copies = FakeWorkingCopies(has_move=True)
    rename_module.DelegatedCloneDirs(copies).move("/g/a", "/g/b")
    assert copies.calls == [("move", "/g/a", "/g/b")]


def test_without_clone_path_move_it_copies_and_stops_asking() -> None:
    copies = FakeWorkingCopies(has_move=False)
    dirs = rename_module.DelegatedCloneDirs(copies)
    dirs.move("/g/a", "/g/b")
    dirs.move("/g/c", "/g/d")
    assert copies.calls == [
        ("move", "/g/a", "/g/b"),
        ("clone", "/g/a", "/g/b"),
        ("clone", "/g/c", "/g/d"),
    ]
