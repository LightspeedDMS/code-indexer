"""Confirmation tokens for destructive git operations (hard reset, clean,
branch delete) live in the cluster-shared PayloadCache.

Invariants, each proven on SQLite and on PostgreSQL:
  - a token issued by one GitOperationsService instance, or one process, is
    accepted by any other instance or process sharing the same store;
  - a token is redeemed at most once, also under concurrent confirmations;
  - a token is bound to (user, repository alias, operation, parameters) and
    is rejected for any other binding;
  - a rejected token yields a fresh token (`requires_confirmation`), and the
    destructive operation does not run.

Real git repositories and a real PayloadCache per backend; the only double
is the alias-to-path lookup.
"""

from __future__ import annotations

import json
import os
import re
import site
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from code_indexer.server.storage.postgres import (
    payload_cache_backend as postgres_payload_backend,
)
from code_indexer.server.storage.sqlite_backends import (
    payload_cache_backend as sqlite_payload_backend,
)
from tests.unit.server.services._git_confirm_helpers import (
    ALICE,
    BOB,
    BRANCH,
    COMMITTED_TEXT,
    REPO_A,
    REPO_B,
    TRACKED,
    UNTRACKED,
    SharedStore,
    git,
    make_repo,
    make_service,
    shared_store_fixture,  # noqa: F401 -- registers the `shared_store` fixture
)

_CHILD = Path(__file__).with_name("_git_confirm_child.py")
_SRC = Path(__file__).resolve().parents[4] / "src"
_THREADS = 8
_PROCESSES = 4
_CHILD_TIMEOUT_SECONDS = 120
_BARRIER_POLL_SECONDS = 0.05
# The token lifetime is 300 s: well inside it, and just past it.
_AGE_INSIDE_LIFETIME = 290
_AGE_PAST_LIFETIME = 310
_NODE_CLOCK_SKEW_SECONDS = 3600
_CLOCK_TOLERANCE = 60


def _clean(svc: Any, alias: str, user: str, token: Optional[str]) -> Dict:
    return svc.clean_repository(alias, user, confirmation_token=token)  # type: ignore[no-any-return]


def _reset_hard(svc: Any, alias: str, user: str, token: Optional[str]) -> Dict:
    return svc.reset_repository(  # type: ignore[no-any-return]
        alias, user, mode="hard", commit_hash="HEAD", confirmation_token=token
    )


def _branch_delete(svc: Any, alias: str, user: str, token: Optional[str]) -> Dict:
    return svc.delete_branch(  # type: ignore[no-any-return]
        alias, user, branch_name=BRANCH, confirmation_token=token
    )


def _cleaned(repo: Path) -> bool:
    return not (repo / UNTRACKED).exists()


def _reset(repo: Path) -> bool:
    return (repo / TRACKED).read_text() == COMMITTED_TEXT


def _branch_gone(repo: Path) -> bool:
    return git(["branch", "--list", BRANCH], repo).strip() == ""


_OPERATIONS = {
    "clean": (_clean, _cleaned),
    "reset_hard": (_reset_hard, _reset),
    "branch_delete": (_branch_delete, _branch_gone),
}

Op = Callable[[Any, str, str, Optional[str]], Dict]


def _issue(op: Op, svc: Any, alias: str, user: str) -> str:
    first = op(svc, alias, user, None)
    assert first["requires_confirmation"] is True, first
    token = first["token"]
    assert isinstance(token, str) and token
    return token


def _assert_rejected(result: Dict, presented: str) -> None:
    assert result.get("success") is not True, result
    assert result.get("requires_confirmation") is True, result
    assert isinstance(result.get("token"), str) and result["token"] != presented


@pytest.fixture
def repos(tmp_path: Path) -> Dict[str, Path]:
    return {
        REPO_A: make_repo(tmp_path / "repos", REPO_A),
        REPO_B: make_repo(tmp_path / "repos", REPO_B),
    }


@pytest.mark.parametrize("op_name", sorted(_OPERATIONS))
def test_token_issued_by_one_instance_is_accepted_by_another(
    shared_store: SharedStore, repos: Dict[str, Path], op_name: str
) -> None:
    op, happened = _OPERATIONS[op_name]
    issuer = make_service(shared_store.new_cache(), repos)
    confirmer = make_service(shared_store.new_cache(), repos)

    token = _issue(op, issuer, REPO_A, ALICE)
    assert not happened(repos[REPO_A])

    result = op(confirmer, REPO_A, ALICE, token)

    assert result.get("success") is True, result
    assert happened(repos[REPO_A])


def test_second_use_is_rejected(
    shared_store: SharedStore, repos: Dict[str, Path]
) -> None:
    svc = make_service(shared_store.new_cache(), repos)
    token = _issue(_clean, svc, REPO_A, ALICE)
    assert _clean(svc, REPO_A, ALICE, token)["success"] is True

    (repos[REPO_A] / UNTRACKED).write_text("again\n")
    second = _clean(make_service(shared_store.new_cache(), repos), REPO_A, ALICE, token)

    _assert_rejected(second, token)
    assert (repos[REPO_A] / UNTRACKED).exists()


def test_concurrent_confirmations_succeed_exactly_once(
    shared_store: SharedStore, repos: Dict[str, Path]
) -> None:
    token = _issue(_clean, make_service(shared_store.new_cache(), repos), REPO_A, ALICE)
    services = [make_service(shared_store.new_cache(), repos) for _ in range(_THREADS)]
    barrier = threading.Barrier(_THREADS)
    results: List[Dict] = []
    lock = threading.Lock()

    def _confirm(svc: Any) -> None:
        barrier.wait()
        try:
            outcome = _clean(svc, REPO_A, ALICE, token)
        except Exception as exc:  # recorded, asserted below
            outcome = {"exception": repr(exc)}
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=_confirm, args=(s,)) for s in services]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=_CHILD_TIMEOUT_SECONDS)

    assert len(results) == _THREADS
    successes = [r for r in results if r.get("success") is True]
    assert len(successes) == 1, results
    for other in (r for r in results if r.get("success") is not True):
        _assert_rejected(other, token)


def _child_env(tmp_path: Path, barrier: Optional[Path]) -> Dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_SRC)
    env["PYTHONUSERBASE"] = site.getuserbase()
    env["HOME"] = str(tmp_path / "child-home")
    (tmp_path / "child-home").mkdir(exist_ok=True)
    if barrier is not None:
        env["GIT_CONFIRM_BARRIER_DIR"] = str(barrier)
    return env


def _spawn_child(
    store: SharedStore,
    tmp_path: Path,
    repo: Path,
    token: str,
    barrier: Optional[Path] = None,
) -> "subprocess.Popen[str]":
    argv = (
        [sys.executable, str(_CHILD), str(tmp_path)]
        + store.child_args()
        + [REPO_A, str(repo), ALICE, token]
        + store.child_tail()
    )
    return subprocess.Popen(
        argv,
        env=_child_env(tmp_path, barrier),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _release_when_ready(barrier: Path, count: int) -> None:
    """Wait (bounded) until `count` children are set up, then release them
    together so their redemptions overlap."""
    deadline = time.monotonic() + _CHILD_TIMEOUT_SECONDS
    while len(list(barrier.glob("ready-*"))) < count:
        assert time.monotonic() < deadline, "children never became ready"
        time.sleep(_BARRIER_POLL_SECONDS)
    (barrier / "go").touch()


def _child_result(proc: "subprocess.Popen[str]") -> Dict:
    out, err = proc.communicate(timeout=_CHILD_TIMEOUT_SECONDS)
    assert proc.returncode == 0, err
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines, err
    return json.loads(lines[-1])  # type: ignore[no-any-return]


def test_token_issued_in_one_process_is_accepted_in_another(
    shared_store: SharedStore, repos: Dict[str, Path], tmp_path: Path
) -> None:
    token = _issue(_clean, make_service(shared_store.new_cache(), repos), REPO_A, ALICE)

    result = _child_result(_spawn_child(shared_store, tmp_path, repos[REPO_A], token))

    assert result.get("success") is True, result
    assert _cleaned(repos[REPO_A])


def test_concurrent_processes_confirm_exactly_once(
    shared_store: SharedStore, repos: Dict[str, Path], tmp_path: Path
) -> None:
    token = _issue(_clean, make_service(shared_store.new_cache(), repos), REPO_A, ALICE)
    barrier = tmp_path / "barrier"
    barrier.mkdir()

    procs = [
        _spawn_child(shared_store, tmp_path, repos[REPO_A], token, barrier)
        for _ in range(_PROCESSES)
    ]
    _release_when_ready(barrier, _PROCESSES)
    results = [_child_result(p) for p in procs]

    successes = [r for r in results if r.get("success") is True]
    assert len(successes) == 1, results
    for other in (r for r in results if r.get("success") is not True):
        _assert_rejected(other, token)


def test_token_bound_to_repo_a_is_rejected_for_repo_b(
    shared_store: SharedStore, repos: Dict[str, Path]
) -> None:
    svc = make_service(shared_store.new_cache(), repos)
    token = _issue(_clean, svc, REPO_A, ALICE)

    result = _clean(svc, REPO_B, ALICE, token)

    _assert_rejected(result, token)
    assert (repos[REPO_B] / UNTRACKED).exists()
    assert _clean(svc, REPO_A, ALICE, token)["success"] is True
    assert _cleaned(repos[REPO_A])


def test_token_bound_to_one_user_is_rejected_for_another(
    shared_store: SharedStore, repos: Dict[str, Path]
) -> None:
    svc = make_service(shared_store.new_cache(), repos)
    token = _issue(_clean, svc, REPO_A, ALICE)

    result = _clean(svc, REPO_A, BOB, token)

    _assert_rejected(result, token)
    assert (repos[REPO_A] / UNTRACKED).exists()
    assert _clean(svc, REPO_A, ALICE, token)["success"] is True
    assert _cleaned(repos[REPO_A])


def test_token_bound_to_one_branch_is_rejected_for_another(
    shared_store: SharedStore, repos: Dict[str, Path]
) -> None:
    git(["branch", "other"], repos[REPO_A])
    svc = make_service(shared_store.new_cache(), repos)
    token = _issue(_branch_delete, svc, REPO_A, ALICE)

    result = svc.delete_branch(
        REPO_A, ALICE, branch_name="other", confirmation_token=token
    )

    _assert_rejected(result, token)
    assert git(["branch", "--list", "other"], repos[REPO_A]).strip() != ""
    assert _branch_delete(svc, REPO_A, ALICE, token)["success"] is True
    assert _branch_gone(repos[REPO_A])


def test_token_for_one_operation_is_rejected_for_another(
    shared_store: SharedStore, repos: Dict[str, Path]
) -> None:
    svc = make_service(shared_store.new_cache(), repos)
    token = _issue(_clean, svc, REPO_A, ALICE)

    result = _reset_hard(svc, REPO_A, ALICE, token)

    _assert_rejected(result, token)
    assert not _reset(repos[REPO_A])
    assert _clean(svc, REPO_A, ALICE, token)["success"] is True


@pytest.mark.parametrize("cache_ttl_seconds", [60, 3600])
def test_token_lifetime_is_fixed_whatever_the_payload_cache_ttl(
    shared_store: SharedStore, repos: Dict[str, Path], cache_ttl_seconds: int
) -> None:
    cache = shared_store.new_cache(cache_ttl_seconds=cache_ttl_seconds)
    svc = make_service(cache, repos)

    inside = _issue(_clean, svc, REPO_A, ALICE)
    shared_store.age_rows(_AGE_INSIDE_LIFETIME)
    cache.cleanup_expired()
    assert _clean(svc, REPO_A, ALICE, inside)["success"] is True

    (repos[REPO_A] / UNTRACKED).write_text("again\n")
    outside = _issue(_clean, svc, REPO_A, ALICE)
    shared_store.age_rows(_AGE_PAST_LIFETIME)
    _assert_rejected(_clean(svc, REPO_A, ALICE, outside), outside)
    assert (repos[REPO_A] / UNTRACKED).exists()


def test_issuing_node_clock_skew_does_not_change_token_lifetime(
    shared_store: SharedStore, repos: Dict[str, Path], monkeypatch: Any
) -> None:
    svc = make_service(shared_store.new_cache(), repos)

    class _NodeClockBehind(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:  # type: ignore[override]
            return datetime.now(tz) - timedelta(seconds=_NODE_CLOCK_SKEW_SECONDS)

    with monkeypatch.context() as patched:
        patched.setattr(sqlite_payload_backend, "datetime", _NodeClockBehind)
        patched.setattr(postgres_payload_backend, "datetime", _NodeClockBehind)
        token = _issue(_clean, svc, REPO_A, ALICE)

    ((_, _, created_at),) = shared_store.rows()
    issued_epoch = datetime.fromisoformat(created_at).timestamp()
    assert abs(issued_epoch - shared_store.store_now_epoch()) < _CLOCK_TOLERANCE
    assert _clean(svc, REPO_A, ALICE, token)["success"] is True


def test_tokens_refuse_a_cache_without_a_storage_backend(
    repos: Dict[str, Path], tmp_path: Path
) -> None:
    standalone = PayloadCache(
        db_path=tmp_path / "standalone_payload_cache.db",
        config=PayloadCacheConfig(cache_ttl_seconds=60),
    )
    standalone.initialize()
    svc = make_service(standalone, repos)

    with pytest.raises(RuntimeError, match="storage backend"):
        _clean(svc, REPO_A, ALICE, None)


def test_store_never_holds_the_token_in_clear(
    shared_store: SharedStore, repos: Dict[str, Path]
) -> None:
    token = _issue(_clean, make_service(shared_store.new_cache(), repos), REPO_A, ALICE)

    ((handle, content, _),) = shared_store.rows()

    assert re.fullmatch(r"git-confirm:[0-9a-f]{64}", handle), handle
    assert token not in handle
    assert token not in content


def test_token_carries_at_least_128_bits(
    shared_store: SharedStore, repos: Dict[str, Path]
) -> None:
    token = _issue(_clean, make_service(shared_store.new_cache(), repos), REPO_A, ALICE)

    # 22 url-safe base64 characters encode 128 bits.
    assert re.fullmatch(r"[A-Za-z0-9_-]{22,}", token), token


def test_unwired_service_fails_loudly(repos: Dict[str, Path]) -> None:
    svc = make_service(None, repos)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="not configured"):
        _clean(svc, REPO_A, ALICE, None)
    with pytest.raises(RuntimeError, match="not configured"):
        _reset_hard(svc, REPO_A, ALICE, None)
    assert (repos[REPO_A] / UNTRACKED).exists()


def test_hard_reset_token_is_bound_to_its_commit(
    shared_store: SharedStore, repos: Dict[str, Path]
) -> None:
    svc = make_service(shared_store.new_cache(), repos)
    token = _issue(_reset_hard, svc, REPO_A, ALICE)

    other_commit = svc.reset_repository(
        REPO_A, ALICE, mode="hard", commit_hash="HEAD~1", confirmation_token=token
    )

    _assert_rejected(other_commit, token)
    assert not _reset(repos[REPO_A])
    assert _reset_hard(svc, REPO_A, ALICE, token)["success"] is True
    assert _reset(repos[REPO_A])
