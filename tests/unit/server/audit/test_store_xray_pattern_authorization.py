"""Who may write the shared X-Ray pattern library, and that every write is attributed.

Driven through the MCP front door's two checks -- the tool gate
(``required_permission`` of the registered tool, as the dispatcher applies
it) and the ``store_xray_pattern`` handler itself -- with real collaborators:
a real group store and access filter, a real git cidx-meta, and a real audit
store bound as the process audit sink.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator
from unittest.mock import patch

import pytest

from _audit_accounts_support import AuditStore, bound_audit_store
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers.xray import handle_store_xray_pattern
from code_indexer.server.mcp.tools import TOOL_REGISTRY
from code_indexer.server.services.access_filtering_service import (
    AccessFilteringService,
)
from code_indexer.server.services.group_access_manager import GroupAccessManager
from code_indexer.server.services.xray_pattern_service import XrayPatternService

GRANTED_REPO = "granted-repo"
UNGRANTED_REPO = "ungranted-repo"


def _pattern(name: str, marker: str = "a") -> str:
    return (
        f"name: {name}\n"
        f'description: "pattern {marker}"\n'
        "language: java\n"
        "evaluator_code: |\n"
        "  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }\n"
    )


def _user(name: str, role: UserRole) -> User:
    return User(
        username=name,
        password_hash="$2b$12$x",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


NORMAL = _user("example-reader", UserRole.NORMAL_USER)
POWER = _user("example-writer", UserRole.POWER_USER)
ADMIN = _user("example-admin", UserRole.ADMIN)


def _body(result: Dict[str, Any]) -> Dict[str, Any]:
    parsed: Dict[str, Any] = json.loads(result["content"][0]["text"])
    return parsed


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[AuditStore]:
    (tmp_path / "audit").mkdir()
    yield from bound_audit_store(tmp_path / "audit" / "groups.db")


@pytest.fixture()
def cidx_meta(tmp_path: Path) -> Path:
    meta = tmp_path / "golden-repos" / "cidx-meta"
    meta.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=meta, check=True)
    return meta


@pytest.fixture()
def server(tmp_path: Path, cidx_meta: Path) -> Iterator[SimpleNamespace]:
    """Yields the app state, so a test can install a refresh scheduler."""
    groups = GroupAccessManager(tmp_path / "groups.db")
    by_name = {g.name: g for g in groups.get_all_groups()}
    groups.assign_user_to_group(NORMAL.username, by_name["users"].id, "test")
    groups.assign_user_to_group(POWER.username, by_name["powerusers"].id, "test")
    groups.assign_user_to_group(ADMIN.username, by_name["admins"].id, "test")
    groups.grant_repo_access(GRANTED_REPO, by_name["powerusers"].id, "test")
    groups.grant_repo_access(UNGRANTED_REPO, by_name["admins"].id, "test")
    state = SimpleNamespace(access_filtering_service=AccessFilteringService(groups))
    app_module = SimpleNamespace(app=SimpleNamespace(state=state))
    with (
        patch("code_indexer.server.mcp.handlers._utils.app_module", app_module),
        patch(
            "code_indexer.server.mcp.handlers.xray._store_pattern._get_cidx_meta_path",
            return_value=cidx_meta,
        ),
    ):
        yield state


class _LockAcquiredAfter:
    """Refresh-scheduler double: another writer finishes while this store
    waits for the cidx-meta write lock (``during`` runs inside acquire)."""

    def __init__(self, during: Callable[[], None]) -> None:
        self._during = during

    def is_write_locked(self, alias: str) -> bool:
        return False

    def acquire_write_lock(self, alias: str, owner: str) -> bool:
        self._during()
        return True

    def release_write_lock(self, alias: str, owner: str) -> None:
        return None


def _install_scheduler(state: SimpleNamespace, scheduler: Any) -> None:
    state.global_lifecycle_manager = SimpleNamespace(refresh_scheduler=scheduler)


def _store(
    user: User, scope: str, yaml_text: str, overwrite: bool = False
) -> Dict[str, Any]:
    """The MCP front door: tool gate first, then the handler."""
    required = TOOL_REGISTRY["store_xray_pattern"]["required_permission"]
    if not user.has_permission(required):
        return {"error": "tool_gate_refused", "required": required}
    return _body(
        handle_store_xray_pattern(
            {"scope": scope, "pattern_yaml": yaml_text, "overwrite": overwrite}, user
        )
    )


def _last_commit_message(cidx_meta: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--format=%B"],
        cwd=cidx_meta,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _outcomes(store: AuditStore) -> list:
    return [(r.actor, r.outcome) for r in store.rows("xray_pattern")]


def test_tool_gate_requires_repository_write() -> None:
    assert (
        TOOL_REGISTRY["store_xray_pattern"]["required_permission"] == "repository:write"
    )


def test_the_unaudited_store_method_is_gone() -> None:
    assert not hasattr(XrayPatternService, "store_xray_pattern")


def test_normal_user_is_refused_by_the_tool_gate_and_the_handler(server, store) -> None:
    assert _store(NORMAL, "__any__", _pattern("p1"))["error"] == "tool_gate_refused"
    body = _body(
        handle_store_xray_pattern(
            {"scope": "__any__", "pattern_yaml": _pattern("p1")}, NORMAL
        )
    )
    assert body.get("success") is not True
    assert body["error"] == "auth_required"


def test_power_user_store_is_admitted_and_audited(server, store, cidx_meta) -> None:
    body = _store(POWER, "__any__", _pattern("p2"))
    assert body.get("success") is True, body
    (row,) = store.rows("xray_pattern")
    assert (row.action_type, row.actor, row.target_id, row.outcome) == (
        "xray_pattern_stored",
        POWER.username,
        "p2",
        "success",
    )
    assert row.details == {"overwrite": False, "global_scope": True}
    assert POWER.username in _last_commit_message(cidx_meta)


def test_granted_repository_scope_is_admitted(server, store) -> None:
    body = _store(POWER, f"{GRANTED_REPO}-global", _pattern("p3"))
    assert body.get("success") is True, body
    (row,) = store.rows("xray_pattern")
    assert row.details == {
        "overwrite": False,
        "global_scope": False,
        "repo": f"{GRANTED_REPO}-global",
    }


def test_ungranted_and_unknown_repository_scopes_get_the_same_refusal(
    server, store, cidx_meta
) -> None:
    ungranted = _store(POWER, f"{UNGRANTED_REPO}-global", _pattern("p4"))
    unknown = _store(POWER, "no-such-repo-global", _pattern("p4"))
    assert ungranted.get("success") is not True
    assert ungranted["error"] == unknown["error"] == "access_denied"
    assert ungranted["message"] == unknown["message"]
    for identifier in (UNGRANTED_REPO, "no-such-repo", POWER.username):
        assert identifier not in ungranted["message"]
    assert not (cidx_meta / "xray-patterns" / f"{UNGRANTED_REPO}-global").exists()
    assert _outcomes(store) == [(POWER.username, "failure")] * 2


def test_overwrite_of_a_repository_pattern_is_audited_with_the_actor(
    server, store, cidx_meta
) -> None:
    scope = f"{GRANTED_REPO}-global"
    assert _store(POWER, scope, _pattern("p5", "a")).get("success") is True
    body = _store(POWER, scope, _pattern("p5", "b"), overwrite=True)
    assert body.get("success") is True, body
    rows = store.rows("xray_pattern")
    assert [(r.actor, r.details["overwrite"]) for r in rows] == [
        (POWER.username, False),
        (POWER.username, True),
    ]
    message = _last_commit_message(cidx_meta)
    assert "update" in message and POWER.username in message


def test_non_admin_cannot_overwrite_a_global_pattern(server, store, cidx_meta) -> None:
    assert _store(ADMIN, "__any__", _pattern("p6", "a")).get("success") is True
    body = _store(POWER, "__any__", _pattern("p6", "b"), overwrite=True)
    assert body.get("success") is not True
    assert body["error"] == "global_pattern_overwrite_forbidden"
    stored = (cidx_meta / "xray-patterns" / "__any__" / "p6.yaml").read_text()
    assert "pattern a" in stored
    assert _outcomes(store) == [
        (ADMIN.username, "success"),
        (POWER.username, "failure"),
    ]


def test_admin_may_overwrite_a_global_pattern(server, store, cidx_meta) -> None:
    assert _store(POWER, "__any__", _pattern("p7", "a")).get("success") is True
    body = _store(ADMIN, "__any__", _pattern("p7", "b"), overwrite=True)
    assert body.get("success") is True, body
    stored = (cidx_meta / "xray-patterns" / "__any__" / "p7.yaml").read_text()
    assert "pattern b" in stored
    last = store.rows("xray_pattern")[-1]
    assert (last.actor, last.details["overwrite"]) == (ADMIN.username, True)


def test_an_invalid_pattern_is_recorded_as_a_failure(server, store) -> None:
    body = _store(POWER, "__any__", "name: [unterminated")
    assert body.get("success") is not True
    assert _outcomes(store) == [(POWER.username, "failure")]


def test_a_failed_commit_is_never_success(server, store, cidx_meta) -> None:
    hook = cidx_meta / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    body = _store(POWER, "__any__", _pattern("p8"))
    assert body.get("success") is not True
    assert body["error"] == "commit_failed"
    assert not (cidx_meta / "xray-patterns" / "__any__" / "p8.yaml").exists()
    assert _outcomes(store) == [(POWER.username, "failure")]


@pytest.mark.parametrize("replacing", [False, True])
def test_a_failed_commit_never_rolls_back_a_peers_file(
    server, store, cidx_meta, caplog, replacing
) -> None:
    relative = "xray-patterns/__any__/p13.yaml"
    target = cidx_meta / relative
    if replacing:
        assert _store(ADMIN, "__any__", _pattern("p13", "old")).get("success")
    hook = cidx_meta / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        f"printf 'name: p13\\ndescription: peer\\n' > {relative}.peer\n"
        f"mv {relative}.peer {relative}\n"
        "exit 1\n"
    )
    hook.chmod(0o755)
    caller = ADMIN if replacing else POWER

    with caplog.at_level("WARNING"):
        body = _store(caller, "__any__", _pattern("p13", "mine"), overwrite=replacing)

    assert body["error"] == "commit_failed"
    assert target.read_text() == "name: p13\ndescription: peer\n"
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any(relative in message for message in warnings), warnings
    assert not any(caller.username in message for message in warnings)


def test_a_pattern_created_while_waiting_for_the_lock_is_not_overwritten(
    server, store, cidx_meta
) -> None:
    target = cidx_meta / "xray-patterns" / "__any__" / "p9.yaml"

    def _peer_stores() -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_pattern("p9", "peer"))

    _install_scheduler(server, _LockAcquiredAfter(_peer_stores))
    body = _store(POWER, "__any__", _pattern("p9", "mine"))
    assert body.get("error") == "pattern_already_exists"
    assert "pattern peer" in target.read_text()
    assert _outcomes(store) == [(POWER.username, "failure")]


def test_a_global_pattern_appearing_while_waiting_for_the_lock_needs_admin(
    server, store, cidx_meta
) -> None:
    target = cidx_meta / "xray-patterns" / "__any__" / "p10.yaml"

    def _peer_stores() -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_pattern("p10", "peer"))

    _install_scheduler(server, _LockAcquiredAfter(_peer_stores))
    body = _store(POWER, "__any__", _pattern("p10", "mine"), overwrite=True)
    assert body.get("error") == "global_pattern_overwrite_forbidden"
    assert "pattern peer" in target.read_text()
    assert _outcomes(store) == [(POWER.username, "failure")]


def _peer_publishes_during(
    monkeypatch: pytest.MonkeyPatch, target: Path, marker: str
) -> None:
    """Another pattern writer publishes *target* after this store's locked
    absence check and before its own publish."""
    from code_indexer.global_repos import catalog_staged_write

    real_sweep = catalog_staged_write._sweep_stale_staging

    def sweep_then_peer_publishes(staging_dir: Path) -> None:
        real_sweep(staging_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_pattern(target.stem, marker))

    monkeypatch.setattr(
        catalog_staged_write, "_sweep_stale_staging", sweep_then_peer_publishes
    )


@pytest.mark.parametrize("overwrite", [False, True])
def test_a_pattern_created_during_the_publish_is_not_overwritten(
    server, store, cidx_meta, monkeypatch, overwrite
) -> None:
    target = cidx_meta / "xray-patterns" / "__any__" / "p11.yaml"
    _peer_publishes_during(monkeypatch, target, "peer")

    body = _store(POWER, "__any__", _pattern("p11", "mine"), overwrite=overwrite)

    assert body.get("error") == "pattern_already_exists", body
    if overwrite:
        assert "overwrite=true" not in body["message"]
        assert "concurrently" in body["message"]
    assert "pattern peer" in target.read_text()
    assert _outcomes(store) == [(POWER.username, "failure")]


def test_a_pattern_appearing_between_the_locked_check_and_the_read_is_not_overwritten(
    server, store, cidx_meta, monkeypatch
) -> None:
    target = cidx_meta / "xray-patterns" / "__any__" / "p14.yaml"
    real_refusal = XrayPatternService._replace_refusal
    calls = []

    def refusal_then_peer_publishes(self, *args, **kwargs):
        result = real_refusal(self, *args, **kwargs)
        calls.append(1)
        if len(calls) == 2:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_pattern("p14", "peer"))
        return result

    monkeypatch.setattr(
        XrayPatternService, "_replace_refusal", refusal_then_peer_publishes
    )

    body = _store(POWER, "__any__", _pattern("p14", "mine"))

    assert body.get("error") == "pattern_already_exists", body
    assert "pattern peer" in target.read_text()


def test_a_peer_replacing_right_after_the_publish_is_never_taken_for_ours(
    server, store, cidx_meta, monkeypatch
) -> None:
    target = cidx_meta / "xray-patterns" / "__any__" / "p15.yaml"
    real_put = XrayPatternService._put_catalog_file
    calls = []

    def put_then_peer_replaces(self, *args, **kwargs):
        identity = real_put(self, *args, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            peer = target.with_name("p15.peer")
            peer.write_text(_pattern("p15", "peer"))
            peer.replace(target)
        return identity

    monkeypatch.setattr(XrayPatternService, "_put_catalog_file", put_then_peer_replaces)
    hook = cidx_meta / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    body = _store(POWER, "__any__", _pattern("p15", "mine"))

    assert body["error"] == "commit_failed"
    assert "pattern peer" in target.read_text()


def test_a_permitted_overwrite_still_replaces(
    server, store, cidx_meta, monkeypatch
) -> None:
    assert _store(ADMIN, "__any__", _pattern("p12", "a")).get("success") is True
    target = cidx_meta / "xray-patterns" / "__any__" / "p12.yaml"
    _peer_publishes_during(monkeypatch, target, "peer")

    body = _store(ADMIN, "__any__", _pattern("p12", "b"), overwrite=True)

    assert body.get("success") is True, body
    assert "pattern b" in target.read_text()


def _git(cidx_meta: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cidx_meta, check=True, capture_output=True, text=True
    ).stdout


def test_an_identical_re_store_succeeds_without_a_commit(
    server, store, cidx_meta
) -> None:
    assert _store(POWER, "__any__", _pattern("p11")).get("success") is True
    commits = _git(cidx_meta, "rev-list", "--count", "HEAD")
    again = _store(POWER, "__any__", _pattern("p11"))
    assert again.get("success") is True, again
    assert _git(cidx_meta, "rev-list", "--count", "HEAD") == commits
    assert (POWER.username, "failure") not in _outcomes(store)


def test_a_commit_failing_after_git_add_leaves_nothing_staged(
    server, store, cidx_meta
) -> None:
    assert _store(POWER, "__any__", _pattern("p12-base")).get("success") is True
    hook = cidx_meta / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    body = _store(POWER, "__any__", _pattern("p12"))
    assert body.get("error") == "commit_failed", body
    assert not (cidx_meta / "xray-patterns" / "__any__" / "p12.yaml").exists()
    assert _git(cidx_meta, "status", "--porcelain") == ""

    hook.unlink()
    assert _store(POWER, "__any__", _pattern("p13")).get("success") is True
    committed = _git(cidx_meta, "show", "--name-only", "--format=", "HEAD")
    assert "p12.yaml" not in committed and "p13.yaml" in committed


def test_a_failed_unstage_after_a_failed_commit_is_logged_as_an_error(
    server, store, cidx_meta, caplog
) -> None:
    git_dir = cidx_meta / ".git"
    hook = git_dir / "hooks" / "pre-commit"
    hook.write_text(
        f"#!/bin/sh\ncp {git_dir}/HEAD {git_dir}/HEAD.saved\n"
        f"echo unreadable > {git_dir}/HEAD\nexit 1\n"
    )
    hook.chmod(0o755)
    try:
        body = _store(POWER, "__any__", _pattern("p14"))
    finally:
        saved = git_dir / "HEAD.saved"
        if saved.exists():
            saved.replace(git_dir / "HEAD")
        hook.unlink()
    assert body.get("error") == "commit_failed", body
    assert any(
        r.levelname == "ERROR" and "xray-patterns/__any__/p14.yaml" in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def _noisy(caplog: pytest.LogCaptureFixture) -> list:
    import logging

    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_store_into_a_non_git_cidx_meta_succeeds_and_is_audited(
    server, store, cidx_meta, caplog
) -> None:
    shutil.rmtree(cidx_meta / ".git")
    caplog.clear()
    body = _store(POWER, "__any__", _pattern("p20"))
    assert body.get("success") is True, body
    assert (cidx_meta / "xray-patterns" / "__any__" / "p20.yaml").exists()
    assert (POWER.username, "success") in _outcomes(store)
    assert not _noisy(caplog), _noisy(caplog)


def test_seed_in_a_non_git_cidx_meta_logs_no_warning(cidx_meta, caplog) -> None:
    shutil.rmtree(cidx_meta / ".git")
    service = XrayPatternService(cidx_meta)
    caplog.clear()
    service.ensure_seed_patterns()
    seed = cidx_meta / "xray-patterns" / "__any__" / "catch-rethrow.yaml"
    assert seed.exists()
    assert not _noisy(caplog), _noisy(caplog)
