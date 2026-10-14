"""Reindex an activated copy after cidx's own writes on a mount-less pod.

Local content backend: nothing changes -- auto-watch re-indexes, no reindex is
enqueued and responses are exactly what they were. Delegated: a successful
write enqueues (or coalesces into) an incremental reindex of the copy, gated
on the registry, never touching the pod's disk, and a failure is loud.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import files as files_handlers
from code_indexer.server.mcp.handlers import git_write
from code_indexer.server.services import activated_write_reindex as mod
from code_indexer.server.services.activated_write_reindex import (
    REINDEX_TYPES_AFTER_WRITE,
    ActivatedWriteReindexer,
)
from code_indexer.server.startup import content_backend_wiring

USER = "alice"
ALIAS = "my-copy"
#: An activated copy's pod path that does not exist: the pod has no repo volume.
POD_COPY = "/nonexistent-pod/data/activated-repos/alice/my-copy"


class FakeJobs:
    def __init__(self, jobs: Optional[List[Dict[str, Any]]] = None) -> None:
        self.jobs = jobs or []

    def list_jobs(
        self, username: str, status_filter: str, limit: int
    ) -> Dict[str, Any]:
        return {
            "jobs": [
                j
                for j in self.jobs
                if j["username"] == username and j["status"] == status_filter
            ]
        }


class FakeIndexManager:
    def __init__(self, jobs: FakeJobs, error: Optional[Exception] = None) -> None:
        self.background_job_manager = jobs
        self.error = error
        self.calls: List[Dict[str, Any]] = []

    def trigger_reindex(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return f"job-{len(self.calls)}"


class FakeRepos:
    def __init__(self, registered: bool = True) -> None:
        self.registered = registered
        self.asked: List[Tuple[str, str]] = []

    def user_has_activated_repo(self, username: str, alias: str) -> bool:
        self.asked.append((username, alias))
        return self.registered

    def get_activated_repo_path(self, username: str, user_alias: str) -> str:
        return POD_COPY


class Clock:
    """A scheduler that runs callbacks only when told to."""

    def __init__(self) -> None:
        self.pending: List[Callable[[], None]] = []

    def __call__(self, delay: float, func: Callable[[], None]) -> None:
        self.pending.append(func)

    def run_next(self) -> None:
        self.pending.pop(0)()


def _job(status: str, alias: str = ALIAS, job_id: str = "j1") -> Dict[str, Any]:
    return {
        "job_id": job_id,
        "username": USER,
        "status": status,
        "repo_alias": alias,
        "operation_type": "reindex",
    }


def _reindexer(
    *,
    delegated: bool = True,
    manager: Optional[FakeIndexManager] = None,
    repos: Optional[FakeRepos] = None,
    clock: Optional[Clock] = None,
    max_attempts: int = 3,
) -> Tuple[ActivatedWriteReindexer, FakeIndexManager, FakeRepos, Clock]:
    manager = manager or FakeIndexManager(FakeJobs())
    repos = repos or FakeRepos()
    clock = clock or Clock()
    reindexer = ActivatedWriteReindexer(
        delegated=lambda: delegated,
        activated_repo_manager=lambda: repos,
        index_manager=lambda: manager,
        scheduler=clock,
        retry_delay=0.0,
        max_attempts=max_attempts,
    )
    return reindexer, manager, repos, clock


@pytest.fixture(autouse=True)
def _no_write_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ActivatedWriteReindexer, "_is_write_exception", staticmethod(lambda a: False)
    )


@pytest.fixture
def no_pod_disk(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Any local filesystem probe of the copy's pod path raises."""
    assert not os.path.exists(POD_COPY)

    def guard(original: Callable[..., Any]) -> Callable[..., Any]:
        def checked(path: Any, *a: Any, **k: Any) -> Any:
            if str(path).startswith("/nonexistent-pod"):
                raise AssertionError(f"local access to pod path {path}")
            return original(path, *a, **k)

        return checked

    monkeypatch.setattr(os.path, "exists", guard(os.path.exists))
    monkeypatch.setattr(os, "stat", guard(os.stat))
    monkeypatch.setattr(Path, "exists", guard(Path.exists))
    yield


# -- local: unchanged --------------------------------------------------------


class TestLocalUnchanged:
    def test_local_backend_enqueues_nothing(self) -> None:
        reindexer, manager, repos, clock = _reindexer(delegated=False)
        assert reindexer.after_write(USER, ALIAS, ["a.py"]) is None
        assert manager.calls == [] and repos.asked == [] and clock.pending == []

    def test_attach_under_local_leaves_result_identical(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reindexer, *_ = _reindexer(delegated=False)
        monkeypatch.setattr(mod, "activated_write_reindexer", lambda: reindexer)
        result = {"success": True, "file_path": "a.py"}
        before = json.dumps(result, sort_keys=True)
        assert mod.attach_reindex_outcome(result, USER, ALIAS, ["a.py"]) is result
        assert json.dumps(result, sort_keys=True) == before

    def test_default_predicate_is_local_outside_a_server(self) -> None:
        # No server built a content backend: the default reindexer is a no-op.
        reindexer = ActivatedWriteReindexer(
            activated_repo_manager=MagicMock(side_effect=AssertionError),
            index_manager=MagicMock(side_effect=AssertionError),
        )
        assert reindexer.after_write(USER, ALIAS, ["a.py"]) is None

    def test_mcp_create_file_response_and_auto_watch_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            content_backend_wiring, "server_content_backend", lambda: None
        )
        monkeypatch.setattr(mod, "_REINDEXER", None)
        watch = MagicMock()
        monkeypatch.setattr(files_handlers, "_start_auto_watch_if_needed", watch)
        result = {"success": True, "file_path": "a.py", "content_hash": "h"}
        with patch(
            "code_indexer.server.services.file_crud_service.file_crud_service"
        ) as crud:
            crud.create_file.return_value = dict(result)
            response = files_handlers.handle_create_file(
                {"repository_alias": ALIAS, "file_path": "a.py", "content": "x"},
                _user(),
            )
        assert json.loads(response["content"][0]["text"]) == result
        watch.assert_called_once()


# -- delegated: enqueue, coalesce, defer -------------------------------------


class TestDelegatedEnqueue:
    def test_write_queues_incremental_reindex(self, no_pod_disk: None) -> None:
        reindexer, manager, repos, _ = _reindexer()
        outcome = reindexer.after_write(USER, ALIAS, ["src/b.py", "a.py"])
        assert outcome == {
            "status": "queued",
            "paths": ["a.py", "src/b.py"],
            "job_id": "job-1",
        }
        assert manager.calls == [
            {
                "repo_alias": ALIAS,
                "index_types": list(REINDEX_TYPES_AFTER_WRITE),
                "clear": False,
                "username": USER,
            }
        ]
        assert repos.asked == [(USER, ALIAS)]

    def test_git_operation_marks_whole_copy(self) -> None:
        reindexer, *_ = _reindexer()
        outcome = reindexer.after_write(USER, ALIAS, None)
        assert outcome is not None and outcome["paths"] == ["*"]

    def test_pending_job_for_copy_coalesces(self) -> None:
        manager = FakeIndexManager(FakeJobs([_job("pending", job_id="p9")]))
        reindexer, manager, _, clock = _reindexer(manager=manager)
        outcome = reindexer.after_write(USER, ALIAS, ["a.py"])
        assert outcome == {"status": "coalesced", "paths": ["a.py"], "job_id": "p9"}
        assert manager.calls == [] and clock.pending == []

    def test_other_job_kinds_do_not_coalesce_or_defer(self) -> None:
        other = [
            dict(_job("pending"), operation_type="sync_repository"),
            dict(_job("running", job_id="j2"), operation_type="activate_repository"),
        ]
        reindexer, manager, _, clock = _reindexer(
            manager=FakeIndexManager(FakeJobs(other))
        )
        outcome = reindexer.after_write(USER, ALIAS, ["a.py"])
        assert outcome is not None and outcome["status"] == "queued"
        assert len(manager.calls) == 1 and clock.pending == []

    def test_pending_job_for_another_copy_does_not_coalesce(self) -> None:
        manager = FakeIndexManager(
            FakeJobs([_job("pending", alias="other")]),
            error=ValueError("Another reindex job is already running/pending (x)"),
        )
        reindexer, manager, _, clock = _reindexer(manager=manager)
        outcome = reindexer.after_write(USER, ALIAS, ["a.py"])
        assert outcome == {"status": "deferred", "paths": ["a.py"]}
        assert len(manager.calls) == 1 and len(clock.pending) == 1

    def test_running_job_defers_then_follow_up_queues(self) -> None:
        jobs = FakeJobs([_job("running")])
        reindexer, manager, _, clock = _reindexer(manager=FakeIndexManager(jobs))
        assert reindexer.after_write(USER, ALIAS, ["a.py"]) == {
            "status": "deferred",
            "paths": ["a.py"],
        }
        # a second write while deferred: absorbed, still ONE follow-up
        assert reindexer.after_write(USER, ALIAS, ["b.py"]) == {
            "status": "deferred",
            "paths": ["b.py"],
        }
        assert manager.calls == [] and len(clock.pending) == 1
        clock.run_next()  # still running -> retry again
        assert manager.calls == [] and len(clock.pending) == 1
        jobs.jobs = []
        clock.run_next()
        assert len(manager.calls) == 1 and clock.pending == []
        assert manager.calls[0]["clear"] is False
        # settled: the next deferral schedules again
        jobs.jobs = [_job("running")]
        reindexer.after_write(USER, ALIAS, ["c.py"])
        assert len(clock.pending) == 1

    def test_follow_up_settles_into_a_pending_job(self) -> None:
        jobs = FakeJobs([_job("running")])
        reindexer, manager, _, clock = _reindexer(manager=FakeIndexManager(jobs))
        reindexer.after_write(USER, ALIAS, ["a.py"])
        jobs.jobs = [_job("pending", job_id="later")]
        clock.run_next()
        assert manager.calls == [] and clock.pending == []

    def test_busy_refusal_from_trigger_defers(self) -> None:
        manager = FakeIndexManager(
            FakeJobs(),
            error=ValueError("Another reindex job is already running/pending (j)"),
        )
        reindexer, *_, clock = _reindexer(manager=manager)
        assert reindexer.after_write(USER, ALIAS, ["a.py"]) == {
            "status": "deferred",
            "paths": ["a.py"],
        }
        assert len(clock.pending) == 1

    def test_write_exception_and_global_are_not_activated_copies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reindexer, manager, repos, _ = _reindexer()
        assert reindexer.after_write(USER, "repo-global", ["a"]) is None
        monkeypatch.setattr(
            ActivatedWriteReindexer,
            "_is_write_exception",
            staticmethod(lambda a: a == "cidx-meta"),
        )
        assert reindexer.after_write(USER, "cidx-meta", ["a"]) is None
        assert manager.calls == [] and repos.asked == []


# -- delegated: fail loud ------------------------------------------------------


class TestDelegatedFailLoud:
    def test_unregistered_copy_is_refused_loudly(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        reindexer, manager, _, _ = _reindexer(repos=FakeRepos(registered=False))
        with caplog.at_level(logging.ERROR):
            outcome = reindexer.after_write(USER, ALIAS, ["a.py"])
        assert outcome is not None and outcome["status"] == "failed"
        assert "not an activated repository" in outcome["error"]
        assert manager.calls == []
        assert any("SVC-REINDEX-001" in r.getMessage() for r in caplog.records)

    def test_enqueue_error_is_failed_not_dropped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        manager = FakeIndexManager(FakeJobs(), error=FileNotFoundError("gone"))
        reindexer, *_ = _reindexer(manager=manager)
        with caplog.at_level(logging.ERROR):
            outcome = reindexer.after_write(USER, ALIAS, ["a.py"])
        assert outcome == {
            "status": "failed",
            "paths": ["a.py"],
            "error": "FileNotFoundError: gone",
        }
        assert any(
            r.levelno == logging.ERROR and "SVC-REINDEX-001" in r.getMessage()
            for r in caplog.records
        )

    def test_other_value_error_is_failed(self) -> None:
        manager = FakeIndexManager(FakeJobs(), error=ValueError("bad types"))
        reindexer, *_ = _reindexer(manager=manager)
        outcome = reindexer.after_write(USER, ALIAS, ["a.py"])
        assert outcome is not None and outcome["status"] == "failed"

    def test_job_listing_error_is_failed(self) -> None:
        class Broken(FakeJobs):
            def list_jobs(
                self, username: str, status_filter: str, limit: int
            ) -> Dict[str, Any]:
                raise RuntimeError("db down")

        reindexer, *_ = _reindexer(manager=FakeIndexManager(Broken()))
        outcome = reindexer.after_write(USER, ALIAS, ["a.py"])
        assert outcome is not None and outcome["status"] == "failed"
        assert "db down" in outcome["error"]

    def test_follow_up_gives_up_loudly(self, caplog: pytest.LogCaptureFixture) -> None:
        jobs = FakeJobs([_job("running")])
        reindexer, manager, _, clock = _reindexer(
            manager=FakeIndexManager(jobs), max_attempts=2
        )
        reindexer.after_write(USER, ALIAS, ["a.py"])
        with caplog.at_level(logging.ERROR):
            clock.run_next()
            clock.run_next()
        assert clock.pending == [] and manager.calls == []
        assert any("SVC-REINDEX-002" in r.getMessage() for r in caplog.records)

    def test_follow_up_error_is_logged_and_settled(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        jobs = FakeJobs([_job("running")])
        manager = FakeIndexManager(jobs)
        reindexer, _, _, clock = _reindexer(manager=manager)
        reindexer.after_write(USER, ALIAS, ["a.py"])
        jobs.jobs = []
        manager.error = FileNotFoundError("copy gone")
        with caplog.at_level(logging.ERROR):
            clock.run_next()
        assert clock.pending == []
        assert any("SVC-REINDEX-001" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize(
        "result",
        [
            {"success": False, "error": "network"},
            {"success": False, "conflicts": []},
            {"success": "yes"},
        ],
    )
    def test_failed_operation_attaches_nothing(
        self, monkeypatch: pytest.MonkeyPatch, result: Dict[str, Any]
    ) -> None:
        reindexer, manager, *_ = _reindexer()
        monkeypatch.setattr(mod, "activated_write_reindexer", lambda: reindexer)
        mod.attach_reindex_outcome(result, USER, ALIAS, None)
        assert "reindex" not in result and manager.calls == []

    def test_conflicted_operation_moved_the_tree(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reindexer, manager, *_ = _reindexer()
        monkeypatch.setattr(mod, "activated_write_reindexer", lambda: reindexer)
        result: Dict[str, Any] = {"success": False, "conflicts": ["a.py"]}
        mod.attach_reindex_outcome(result, USER, ALIAS, None)
        assert result["reindex"]["status"] == "queued" and len(manager.calls) == 1

    @pytest.mark.parametrize(
        "result, changed, expected_calls",
        [
            ({"success": False, "error": "stash pop failed"}, True, 1),
            ({"success": True}, False, 0),
        ],
    )
    def test_explicit_changed_overrides_the_result(
        self,
        monkeypatch: pytest.MonkeyPatch,
        result: Dict[str, Any],
        changed: bool,
        expected_calls: int,
    ) -> None:
        reindexer, manager, *_ = _reindexer()
        monkeypatch.setattr(mod, "activated_write_reindexer", lambda: reindexer)
        mod.attach_reindex_outcome(result, USER, ALIAS, None, changed=changed)
        assert len(manager.calls) == expected_calls
        assert ("reindex" in result) is bool(expected_calls)

    def test_non_dict_result_is_returned_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reindexer, manager, *_ = _reindexer()
        monkeypatch.setattr(mod, "activated_write_reindexer", lambda: reindexer)
        assert mod.attach_reindex_outcome(None, USER, ALIAS, None, changed=True) is None
        assert manager.calls == []

    @pytest.mark.parametrize(
        "result, expected",
        [
            ({"success": True}, True),
            ({"success": False, "conflicts": ["a.py"]}, True),
            ({"success": False, "conflicts": [{"file": "a.py"}]}, True),
            ({"success": False, "conflicts": []}, False),
            ({"success": False}, False),
            ({"success": 1}, False),
            ("success", False),
        ],
    )
    def test_tree_changed(self, result: Any, expected: bool) -> None:
        assert mod.tree_changed(result) is expected


# -- the handlers, delegated -------------------------------------------------


def _user(role: UserRole = UserRole.NORMAL_USER) -> User:
    return User(
        username=USER,
        password_hash="x",
        role=role,
        created_at=datetime.now(timezone.utc),
    )


def _body(response: Dict[str, Any]) -> Dict[str, Any]:
    return dict(json.loads(response["content"][0]["text"]))


@pytest.fixture
def delegated_reindexer(
    monkeypatch: pytest.MonkeyPatch, no_pod_disk: None
) -> FakeIndexManager:
    reindexer, manager, _, _ = _reindexer()
    monkeypatch.setattr(mod, "activated_write_reindexer", lambda: reindexer)
    return manager


class TestHandlersDelegated:
    @pytest.mark.parametrize(
        "handler, method, params",
        [
            (
                files_handlers.handle_create_file,
                "create_file",
                {"content": "x"},
            ),
            (
                files_handlers.handle_edit_file,
                "edit_file",
                {"old_string": "a", "new_string": "b", "content_hash": "h"},
            ),
            (files_handlers.handle_delete_file, "delete_file", {}),
        ],
    )
    def test_file_tools_report_the_reindex(
        self,
        monkeypatch: pytest.MonkeyPatch,
        delegated_reindexer: FakeIndexManager,
        handler: Callable[..., Dict[str, Any]],
        method: str,
        params: Dict[str, Any],
    ) -> None:
        monkeypatch.setattr(files_handlers, "_start_auto_watch_if_needed", MagicMock())
        with patch(
            "code_indexer.server.services.file_crud_service.file_crud_service"
        ) as crud:
            getattr(crud, method).return_value = {"success": True}
            body = _body(
                handler(
                    {"repository_alias": ALIAS, "file_path": "src/a.py", **params},
                    _user(),
                )
            )
        assert body["reindex"] == {
            "status": "queued",
            "paths": ["src/a.py"],
            "job_id": "job-1",
        }

    def test_file_tool_error_enqueues_nothing(
        self, monkeypatch: pytest.MonkeyPatch, delegated_reindexer: FakeIndexManager
    ) -> None:
        monkeypatch.setattr(files_handlers, "_start_auto_watch_if_needed", MagicMock())
        with patch(
            "code_indexer.server.services.file_crud_service.file_crud_service"
        ) as crud:
            crud.create_file.side_effect = FileExistsError("exists")
            body = _body(
                files_handlers.handle_create_file(
                    {"repository_alias": ALIAS, "file_path": "a", "content": "x"},
                    _user(),
                )
            )
        assert body["success"] is False and delegated_reindexer.calls == []

    def _git(
        self,
        monkeypatch: pytest.MonkeyPatch,
        service_method: str,
        result: Dict[str, Any],
    ) -> MagicMock:
        import code_indexer.server.mcp.handlers._legacy as legacy

        monkeypatch.setattr(
            legacy, "_resolve_git_repo_path", lambda alias, user: (POD_COPY, None)
        )
        monkeypatch.setattr(git_write, "_invalidate_wiki_cache", lambda *a: None)
        service = MagicMock()
        getattr(service, service_method).return_value = result
        monkeypatch.setattr(git_write, "git_operations_service", service)
        return service

    def test_git_checkout_file_reports_the_reindex(
        self, monkeypatch: pytest.MonkeyPatch, delegated_reindexer: FakeIndexManager
    ) -> None:
        self._git(monkeypatch, "git_checkout_file", {"success": True})
        body = _body(
            git_write.git_checkout_file(
                {"repository_alias": ALIAS, "file_path": "a.py"}, _user()
            )
        )
        assert body["reindex"]["paths"] == ["a.py"]
        assert body["reindex"]["status"] == "queued"

    def test_git_merge_abort_reports_the_reindex(
        self, monkeypatch: pytest.MonkeyPatch, delegated_reindexer: FakeIndexManager
    ) -> None:
        self._git(monkeypatch, "git_merge_abort", {"success": True})
        body = _body(git_write.git_merge_abort({"repository_alias": ALIAS}, _user()))
        assert body["reindex"]["paths"] == ["*"]

    @pytest.mark.parametrize(
        "handler, service_method, args, role",
        [
            (
                git_write.git_merge,
                "merge_branch",
                {"source_branch": "feature"},
                UserRole.NORMAL_USER,
            ),
            (
                git_write.git_branch_switch,
                "git_branch_switch",
                {"branch_name": "feature"},
                UserRole.NORMAL_USER,
            ),
            (git_write.git_reset, "git_reset", {"mode": "mixed"}, UserRole.ADMIN),
            (git_write.git_clean, "git_clean", {}, UserRole.ADMIN),
            (git_write.git_pull, "pull_from_remote", {}, UserRole.NORMAL_USER),
        ],
        ids=["merge", "branch_switch", "reset", "clean", "pull"],
    )
    def test_tree_moving_git_tool_reports_the_reindex(
        self,
        monkeypatch: pytest.MonkeyPatch,
        delegated_reindexer: FakeIndexManager,
        handler: Callable[..., Dict[str, Any]],
        service_method: str,
        args: Dict[str, Any],
        role: UserRole,
    ) -> None:
        monkeypatch.setattr(git_write, "_check_writable", lambda *a: None)
        monkeypatch.setattr(git_write.BranchService, "invalidate", lambda p: None)
        self._git(monkeypatch, service_method, {"success": True})
        body = _body(handler({"repository_alias": ALIAS, **args}, _user(role)))
        assert body["reindex"] == {
            "status": "queued",
            "paths": ["*"],
            "job_id": "job-1",
        }
        assert [c["repo_alias"] for c in delegated_reindexer.calls] == [ALIAS]

    @pytest.mark.parametrize(
        "handler, service_method, args",
        [
            (git_write.git_merge, "merge_branch", {"source_branch": "feature"}),
            (git_write.git_pull, "pull_from_remote", {}),
        ],
        ids=["merge", "pull"],
    )
    def test_conflicted_merge_and_pull_report_the_reindex(
        self,
        monkeypatch: pytest.MonkeyPatch,
        delegated_reindexer: FakeIndexManager,
        handler: Callable[..., Dict[str, Any]],
        service_method: str,
        args: Dict[str, Any],
    ) -> None:
        monkeypatch.setattr(git_write, "_check_writable", lambda *a: None)
        self._git(
            monkeypatch, service_method, {"success": False, "conflicts": ["a.py"]}
        )
        body = _body(handler({"repository_alias": ALIAS, **args}, _user()))
        assert body["success"] is False and body["conflicts"] == ["a.py"]
        assert body["reindex"]["status"] == "queued"
        assert len(delegated_reindexer.calls) == 1

    def test_failed_pull_without_conflicts_enqueues_nothing(
        self, monkeypatch: pytest.MonkeyPatch, delegated_reindexer: FakeIndexManager
    ) -> None:
        self._git(
            monkeypatch,
            "pull_from_remote",
            {"success": False, "updated_files": 0, "conflicts": []},
        )
        body = _body(git_write.git_pull({"repository_alias": ALIAS}, _user()))
        assert "reindex" not in body and delegated_reindexer.calls == []

    @pytest.mark.parametrize(
        "handler, service_method",
        [(git_write.git_reset, "git_reset"), (git_write.git_clean, "git_clean")],
        ids=["reset", "clean"],
    )
    def test_confirmation_request_enqueues_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        delegated_reindexer: FakeIndexManager,
        handler: Callable[..., Dict[str, Any]],
        service_method: str,
    ) -> None:
        self._git(
            monkeypatch,
            service_method,
            {"success": True, "requires_confirmation": True, "token": "tok"},
        )
        body = _body(handler({"repository_alias": ALIAS}, _user(UserRole.ADMIN)))
        assert body["confirmation_token_required"]["token"] == "tok"
        assert "reindex" not in body and delegated_reindexer.calls == []

    @pytest.mark.parametrize("action", ["pop", "apply"])
    def test_failed_stash_pop_or_apply_reports_the_reindex(
        self,
        monkeypatch: pytest.MonkeyPatch,
        delegated_reindexer: FakeIndexManager,
        action: str,
    ) -> None:
        from code_indexer.server.services.git_operations_service import (
            GitCommandError,
        )

        service = self._git(monkeypatch, f"git_stash_{action}", {})
        getattr(service, f"git_stash_{action}").side_effect = GitCommandError(
            f"git stash {action} failed", stderr="CONFLICT"
        )
        body = _body(
            git_write.git_stash({"repository_alias": ALIAS, "action": action}, _user())
        )
        assert body["success"] is False and body["error_type"] == "GitCommandError"
        assert body["reindex"]["status"] == "queued"

    @pytest.mark.parametrize("action", ["push", "list", "drop"])
    def test_failed_stash_push_list_drop_enqueue_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        delegated_reindexer: FakeIndexManager,
        action: str,
    ) -> None:
        from code_indexer.server.services.git_operations_service import (
            GitCommandError,
        )

        service = self._git(monkeypatch, f"git_stash_{action}", {})
        getattr(service, f"git_stash_{action}").side_effect = GitCommandError(
            "failed", stderr=""
        )
        body = _body(
            git_write.git_stash({"repository_alias": ALIAS, "action": action}, _user())
        )
        assert "reindex" not in body and delegated_reindexer.calls == []

    @pytest.mark.parametrize("action", ["pop", "apply", "push"])
    def test_tree_changing_stash_reports_the_reindex(
        self,
        monkeypatch: pytest.MonkeyPatch,
        delegated_reindexer: FakeIndexManager,
        action: str,
    ) -> None:
        self._git(monkeypatch, f"git_stash_{action}", {"success": True})
        body = _body(
            git_write.git_stash({"repository_alias": ALIAS, "action": action}, _user())
        )
        assert body["reindex"]["status"] == "queued"

    @pytest.mark.parametrize("action", ["list", "drop"])
    def test_stash_list_and_drop_do_not_reindex(
        self,
        monkeypatch: pytest.MonkeyPatch,
        delegated_reindexer: FakeIndexManager,
        action: str,
    ) -> None:
        self._git(monkeypatch, f"git_stash_{action}", {"success": True})
        body = _body(
            git_write.git_stash({"repository_alias": ALIAS, "action": action}, _user())
        )
        assert "reindex" not in body and delegated_reindexer.calls == []


class TestRestRoutesDelegated:
    def test_rest_create_file_enqueues(
        self, monkeypatch: pytest.MonkeyPatch, delegated_reindexer: FakeIndexManager
    ) -> None:
        from code_indexer.server.routers import files as files_router

        crud = MagicMock()
        crud.create_file.return_value = {
            "success": True,
            "file_path": "a.py",
            "content_hash": "h",
            "size_bytes": 1,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        monkeypatch.setattr(files_router, "file_crud_service", crud)
        response = files_router.create_file(
            ALIAS,
            files_router.CreateFileRequest(file_path="a.py", content="x"),
            _user(),
        )
        assert response.file_path == "a.py"
        assert delegated_reindexer.calls[0]["repo_alias"] == ALIAS


def _rest_git_cases() -> List[
    Tuple[str, str, Tuple[Any, ...], Dict[str, Any], List[str]]
]:
    from code_indexer.server.routers import git_models as m

    return [
        (
            "git_pull",
            "pull_from_remote",
            (m.GitPullRequest(remote="origin", branch=None),),
            {"success": True, "updated_files": 1, "conflicts": []},
            ["*"],
        ),
        (
            "git_reset",
            "reset_repository",
            (
                m.GitResetRequest(
                    mode="mixed", commit_hash=None, confirmation_token=None
                ),
            ),
            {"success": True},
            ["*"],
        ),
        (
            "git_clean",
            "clean_repository",
            (m.GitCleanRequest(confirmation_token=None),),
            {"success": True},
            ["*"],
        ),
        (
            "git_merge_abort",
            "abort_merge",
            (),
            {"success": True, "aborted": True},
            ["*"],
        ),
        (
            "git_checkout_file",
            "checkout_file",
            (m.GitCheckoutFileRequest(file_path="a.py"),),
            {"success": True, "restored_file": "a.py"},
            ["a.py"],
        ),
        (
            "git_branch_switch",
            "switch_branch",
            ("feature",),
            {"success": True, "current_branch": "feature", "previous_branch": "main"},
            ["*"],
        ),
    ]


class TestRestGitRoutesDelegated:
    @pytest.mark.parametrize(
        "route, method, args, result, paths",
        _rest_git_cases(),
        ids=[c[0] for c in _rest_git_cases()],
    )
    def test_tree_moving_route_enqueues(
        self,
        monkeypatch: pytest.MonkeyPatch,
        no_pod_disk: None,
        route: str,
        method: str,
        args: Tuple[Any, ...],
        result: Dict[str, Any],
        paths: List[str],
    ) -> None:
        from code_indexer.server.routers import git as git_router

        reindexer, manager, _, _ = _reindexer()
        seen: List[Optional[Dict[str, Any]]] = []
        original = reindexer.after_write

        def spy(username: str, alias: str, p: Any = None) -> Any:
            outcome = original(username, alias, p)
            seen.append(outcome)
            return outcome

        monkeypatch.setattr(reindexer, "after_write", spy)
        monkeypatch.setattr(mod, "activated_write_reindexer", lambda: reindexer)
        service = MagicMock()
        getattr(service, method).return_value = dict(result)
        monkeypatch.setattr(git_router, "git_operations_service", service)
        getattr(git_router, route)(ALIAS, *args, user=_user())
        assert len(manager.calls) == 1 and manager.calls[0]["repo_alias"] == ALIAS
        assert seen[0] is not None and seen[0]["paths"] == paths


class TestRestConflictedPull:
    def test_conflicted_pull_enqueues(
        self, monkeypatch: pytest.MonkeyPatch, delegated_reindexer: FakeIndexManager
    ) -> None:
        from code_indexer.server.routers import git as git_router
        from code_indexer.server.routers import git_models as m

        service = MagicMock()
        service.pull_from_remote.return_value = {
            "success": False,
            "updated_files": 0,
            "conflicts": ["a.py"],
        }
        monkeypatch.setattr(git_router, "git_operations_service", service)
        response = git_router.git_pull(
            ALIAS, m.GitPullRequest(remote="origin", branch=None), user=_user()
        )
        assert response.conflicts == ["a.py"]
        assert [c["repo_alias"] for c in delegated_reindexer.calls] == [ALIAS]


class TestParity:
    @pytest.mark.parametrize("delegated", [False, True])
    def test_edit_file_body_equal_but_for_reindex(
        self, monkeypatch: pytest.MonkeyPatch, delegated: bool
    ) -> None:
        reindexer, manager, _, _ = _reindexer(delegated=delegated)
        monkeypatch.setattr(mod, "activated_write_reindexer", lambda: reindexer)
        monkeypatch.setattr(files_handlers, "_start_auto_watch_if_needed", MagicMock())
        result = {"success": True, "file_path": "a.py", "content_hash": "h2"}
        with patch(
            "code_indexer.server.services.file_crud_service.file_crud_service"
        ) as crud:
            crud.edit_file.return_value = dict(result)
            body = _body(
                files_handlers.handle_edit_file(
                    {
                        "repository_alias": ALIAS,
                        "file_path": "a.py",
                        "old_string": "a",
                        "new_string": "b",
                        "content_hash": "h",
                    },
                    _user(),
                )
            )
        reindex = body.pop("reindex", None)
        assert body == result
        assert (reindex is not None) is delegated
        assert len(manager.calls) == (1 if delegated else 0)
