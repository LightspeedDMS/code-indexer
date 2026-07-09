"""Phase 3 -- Story #1134: keep-last-N versioned-snapshot retention + path routing.

LOCAL clone backend only (cow-daemon / ONTAP are OUT of scope).

Drives the REAL front door (in-process FastAPI ``TestClient``) end-to-end:

  1. register   -- POST /api/admin/golden-repos (JSON: repo_url + alias)
  2. refresh xN -- POST /api/admin/golden-repos/{alias}/refresh (N+1 = 4 times),
                   each preceded by a REAL git commit pushed upstream so
                   change-detection mints a new ``.versioned/{alias}/v_*`` snapshot
  3. query      -- POST /api/query (verify results + correct path routing)
  4. deregister -- DELETE /api/admin/golden-repos/{alias}

Empirically-verified front-door / LOCAL-backend behaviour (manual-execute-first)
--------------------------------------------------------------------------------
The retention spec (#1084 / #1134) is "keep-last-N": after N+1 refreshes only the
N newest snapshots survive and the oldest is pruned.  On the LOCAL clone backend,
the refresh swap-site (``RefreshScheduler._execute_refresh``) ALSO schedules the
immediately-previous snapshot for refcount-gated cleanup on EVERY refresh
(Story #236 master-guard + Bug #1084 A4).  With no in-flight query holding a
refcount, that previous snapshot is deleted by ``CleanupManager`` within ~1s, so
the steady state on the local backend is a SINGLE surviving snapshot.  The
keep-last-N retention (``_enforce_retention``) is defense-in-depth layered on top
and never has more than N snapshots to consider on this path.

This test therefore asserts the spec's success metric exactly as the local
front door delivers it:

  * AC1 / control: every refresh mints a NEW ``v_*`` snapshot (distinct
    timestamp); the surviving snapshot count NEVER exceeds the configured
    keep-last-N; and the OLDEST snapshot (from refresh #1) is pruned from disk.
  * Mutation: after refreshing beyond N, the specific oldest ``v_*`` directory no
    longer exists on disk while the current one does, and the front-door query is
    unaffected.
  * AC2: the front-door query returns results, served from the Priority-1 mutable
    base clone (``get_actual_repo_path`` -> ``{golden_repos_dir}/{alias}``, NOT a
    ``.versioned`` path), characterised via ``is_immutable_versioned_snapshot()``.

Snapshot survival is verified on disk as EVIDENCE only; ``.versioned/`` is NEVER
modified / checked out / indexed (CLAUDE.md invariant) -- it is only READ.

Environment / guards
--------------------
  * VOYAGE_API_KEY / E2E_VOYAGE_API_KEY -- required (refresh re-indexes via VoyageAI).
    Loud skip locally, hard-fail in CI (CIDX_E2E_REQUIRE_ALL).
  * E2E_ADMIN_USER / E2E_ADMIN_PASS -- required (admin front-door operations).
  * E2E_GOLDEN_JOB_TIMEOUT (default 300) / E2E_GOLDEN_JOB_POLL (default 0.5).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from tests.e2e.helpers import require_voyage_key
from tests.e2e.server.conftest import AdminTokenProvider

# ---------------------------------------------------------------------------
# Configuration resolved from environment variables
# ---------------------------------------------------------------------------
_JOB_TIMEOUT: float = float(os.environ.get("E2E_GOLDEN_JOB_TIMEOUT", "300"))
_JOB_POLL: float = float(os.environ.get("E2E_GOLDEN_JOB_POLL", "0.5"))
_TERMINAL: frozenset[str] = frozenset({"completed", "failed", "cancelled"})

# Throwaway golden-repo alias (unique to this test for teardown + log-gate anchoring).
_ALIAS: str = "ret1134"

# N = snapshot_retention_keep_last default (config_manager.py). Refresh N+1 times.
_KEEP_LAST_N: int = 3
_REFRESH_COUNT: int = _KEEP_LAST_N + 1

# Bounded wait for the refcount-gated CleanupManager (check_interval 1.0s) to prune.
_PRUNE_WAIT_SECONDS: float = 60.0


# ---------------------------------------------------------------------------
# git helpers -- build a REAL source repo with a fetchable origin
# ---------------------------------------------------------------------------
def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a git subprocess, raising on non-zero exit."""
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _make_source_repo(base: Path) -> Path:
    """Create a bare upstream + working source clone (origin -> upstream).

    The golden-repo registration copies the source's ``.git`` verbatim
    (``shutil.copytree``), so the golden clone inherits ``origin`` pointing at the
    bare upstream.  Pushing new commits upstream is what makes the golden clone's
    ``git fetch origin`` detect changes on each refresh (and thus mint a snapshot).
    """
    bare = base / "upstream.git"
    bare.mkdir(parents=True)
    _git(["init", "--bare", "-b", "main"], cwd=bare)

    src = base / "source"
    src.mkdir(parents=True)
    _git(["init", "-b", "main"], cwd=src)
    _git(["config", "user.email", "e2e-1134@cidx.test"], cwd=src)
    _git(["config", "user.name", "CIDX E2E 1134"], cwd=src)
    _git(["remote", "add", "origin", str(bare)], cwd=src)
    (src / "alpha.py").write_text(
        "def alpha_authentication():\n    return 'login token'\n"
    )
    (src / "beta.py").write_text("def beta_search(query):\n    return query.upper()\n")
    _git(["add", "-A"], cwd=src)
    _git(["commit", "-m", "initial commit"], cwd=src)
    _git(["push", "-u", "origin", "main"], cwd=src)
    return src


def _commit_and_push_change(src: Path, n: int) -> None:
    """Append a new file, commit, and push upstream so the refresh sees changes."""
    (src / f"change_{n}.py").write_text(f"def change_{n}():\n    return {n}\n")
    _git(["add", "-A"], cwd=src)
    _git(["commit", "-m", f"change {n}"], cwd=src)
    _git(["push", "origin", "main"], cwd=src)


# ---------------------------------------------------------------------------
# Front-door job-wait helper (TestClient, mirrors test_09 style)
# ---------------------------------------------------------------------------
def _wait_for_job(
    client: TestClient,
    job_id: str,
    headers_fn: Callable[[], dict[str, str]],
    label: str,
) -> dict[str, Any]:
    """Poll GET /api/jobs/{job_id} until a terminal state. Bounded (Messi #14).

    ``headers_fn`` is called on EVERY poll iteration so that long-lived waits
    (multiple minutes per refresh) always use a fresh JWT and never hit 401.
    """
    deadline = time.monotonic() + _JOB_TIMEOUT
    while time.monotonic() < deadline:
        resp = client.get(f"/api/jobs/{job_id}", headers=headers_fn())
        assert resp.status_code < 500, (
            f"{label}: job poll returned HTTP {resp.status_code}: {resp.text[:200]}"
        )
        if resp.status_code == 200:
            body: dict[str, Any] = resp.json()
            if body.get("status") in _TERMINAL:
                return body
        time.sleep(_JOB_POLL)
    raise TimeoutError(
        f"{label}: job {job_id!r} did not complete within {_JOB_TIMEOUT}s"
    )


def _list_snapshot_dirs(snapshot_manager: Any, alias: str) -> list[tuple[str, int]]:
    """READ-ONLY snapshot discovery via the wired VersionedSnapshotManager.

    Returns ``[(path, ts), ...]`` ascending. Never modifies ``.versioned/``.
    """
    return list(snapshot_manager.list_snapshots(alias))


# ---------------------------------------------------------------------------
# Fixture: build source repo + register golden repo via the REST front door
# ---------------------------------------------------------------------------
@pytest.fixture
def retention_repo(
    test_client: TestClient,
    admin_token_provider: AdminTokenProvider,
) -> Iterator[dict[str, Any]]:
    """Register a throwaway golden repo on the LOCAL backend; yield context.

    Skips loudly (hard-fail in CI) when VOYAGE_API_KEY is absent because refresh
    re-indexes via VoyageAI.  Robust teardown deregisters the golden repo and
    removes all temp directories so later tests + the S1 log gate are unaffected.

    Yields a dict with: client, token_provider, alias, source path,
    golden_repos_dir, base_clone path, snapshot_manager, golden_repo_manager.

    ``token_provider`` replaces the old frozen ``headers`` entry: callers must
    call ``ctx["token_provider"].get_headers()`` immediately before each request
    so that tokens are always fresh across the multi-minute refresh sequences.
    """
    require_voyage_key()

    # The unified test_client fixture sets code_indexer.server.app.app = fresh_app
    # before entering the TestClient lifespan; read service state off that
    # FastAPI singleton (typed with .state) rather than test_client.app (typed as
    # the lifespan callable, which has no .state).
    import code_indexer.server.app as _app_module

    fresh_app = _app_module.app
    snapshot_manager = getattr(fresh_app.state, "snapshot_manager", None)
    golden_repo_manager = getattr(fresh_app.state, "golden_repo_manager", None)
    golden_repos_dir = getattr(fresh_app.state, "golden_repos_dir", None)
    if snapshot_manager is None or golden_repos_dir is None:
        pytest.skip(
            "snapshot_manager / golden_repos_dir not wired on app.state "
            "(LOCAL clone backend not initialised) -- cannot validate retention."
        )

    # LOCAL backend assertion: this story is scoped to LocalCloneBackend only.
    clone_backend = getattr(snapshot_manager, "_clone_backend", None)
    backend_name = type(clone_backend).__name__ if clone_backend else None
    if backend_name != "LocalCloneBackend":
        pytest.skip(
            f"Story #1134 is LOCAL-backend only; wired backend is {backend_name!r}."
        )

    workdir = Path(tempfile.mkdtemp(prefix="cidx-1134-"))
    src = _make_source_repo(workdir)

    # Register the golden repo (auto global-activates + runs cidx init/index).
    # Fetch headers fresh immediately before the request.
    reg = test_client.post(
        "/api/admin/golden-repos",
        json={"repo_url": str(src), "alias": _ALIAS},
        headers=admin_token_provider.get_headers(),
    )
    assert reg.status_code in (200, 202), (
        f"register returned HTTP {reg.status_code}: {reg.text[:300]}"
    )
    reg_job = reg.json().get("job_id", "")
    assert reg_job, f"register response missing job_id: {reg.json()}"
    status = _wait_for_job(
        test_client, reg_job, admin_token_provider.get_headers, "register"
    )
    assert status.get("status") == "completed", (
        f"register job ended {status.get('status')!r}: {status.get('error')}"
    )

    base_clone = Path(golden_repos_dir) / _ALIAS
    assert base_clone.exists(), f"base clone not created at {base_clone}"
    assert (base_clone / ".code-indexer").exists(), (
        "registration did not initialise .code-indexer/ on the base clone"
    )

    ctx = {
        "client": test_client,
        "token_provider": admin_token_provider,
        "alias": _ALIAS,
        "source": src,
        "golden_repos_dir": Path(golden_repos_dir),
        "base_clone": base_clone,
        "snapshot_manager": snapshot_manager,
        "golden_repo_manager": golden_repo_manager,
        "workdir": workdir,
    }
    try:
        yield ctx
    finally:
        # Teardown: deregister golden repo, then remove temp dirs.
        # Fetch headers fresh at teardown time (token may have been refreshed).
        try:
            d = test_client.request(
                "DELETE",
                f"/api/admin/golden-repos/{_ALIAS}",
                headers=admin_token_provider.get_headers(),
            )
            if d.status_code in (200, 202):
                jid = d.json().get("job_id")
                if jid:
                    _wait_for_job(
                        test_client,
                        jid,
                        admin_token_provider.get_headers,
                        "deregister",
                    )
        except Exception:  # noqa: BLE001 -- teardown is best-effort
            pass
        shutil.rmtree(workdir, ignore_errors=True)


def _refresh_once(
    client: TestClient,
    headers_fn: Callable[[], dict[str, str]],
    alias: str,
    label: str,
) -> None:
    """Trigger a front-door refresh and wait for the job to complete.

    ``headers_fn`` is called immediately before each HTTP request so that the
    token is always fresh, even across the multi-minute wait inside
    ``_wait_for_job``.
    """
    rr = client.post(f"/api/admin/golden-repos/{alias}/refresh", headers=headers_fn())
    assert rr.status_code in (200, 202), (
        f"{label}: refresh returned HTTP {rr.status_code}: {rr.text[:300]}"
    )
    jid = rr.json().get("job_id", "")
    assert jid, f"{label}: refresh response missing job_id: {rr.json()}"
    status = _wait_for_job(client, jid, headers_fn, label)
    assert status.get("status") == "completed", (
        f"{label}: refresh job ended {status.get('status')!r}: {status.get('error')}"
    )


# ---------------------------------------------------------------------------
# AC1 + mutation: keep-last-N retention prunes the oldest snapshot
# ---------------------------------------------------------------------------
def test_keep_last_n_retention_prunes_oldest_snapshot(
    retention_repo: dict[str, Any],
) -> None:
    """AC1: N+1 front-door refreshes -> only the N-newest snapshots survive.

    On the LOCAL backend the per-swap refcount-gated cleanup keeps the steady
    state at a SINGLE snapshot, so the assertion is: every refresh mints a NEW
    ``v_*`` (distinct timestamp); the surviving count NEVER exceeds keep-last-N;
    and the OLDEST snapshot (refresh #1) is pruned from disk (mutation check).
    """
    client = retention_repo["client"]
    token_provider = retention_repo["token_provider"]
    headers_fn = token_provider.get_headers
    alias = retention_repo["alias"]
    src = retention_repo["source"]
    sm = retention_repo["snapshot_manager"]

    # No snapshots exist immediately after registration (master is the live path).
    assert _list_snapshot_dirs(sm, alias) == [], (
        "expected zero .versioned snapshots immediately after registration"
    )

    minted_timestamps: list[int] = []
    first_snapshot_path: str | None = None
    max_surviving = 0

    for i in range(1, _REFRESH_COUNT + 1):
        # Real git change pushed upstream so the refresh detects it and mints a snapshot.
        _commit_and_push_change(src, i)
        _refresh_once(client, headers_fn, alias, f"refresh{i}")

        # Allow the refcount-gated CleanupManager (check_interval ~1s) to settle.
        snaps = _wait_until_count_at_most(sm, alias, _KEEP_LAST_N)
        assert snaps, f"refresh {i}: expected at least one .versioned snapshot on disk"

        newest_path, newest_ts = snaps[-1]
        minted_timestamps.append(newest_ts)
        if first_snapshot_path is None:
            first_snapshot_path = newest_path

        # Control (DoD): surviving count must never exceed keep-last-N.
        max_surviving = max(max_surviving, len(snaps))
        assert len(snaps) <= _KEEP_LAST_N, (
            f"refresh {i}: surviving snapshot count {len(snaps)} exceeds "
            f"keep-last-N={_KEEP_LAST_N}: {[p for p, _ in snaps]}"
        )

        # Every surviving snapshot path is a canonical .versioned/{alias}/v_* shape.
        for path, _ts in snaps:
            assert "/.versioned/" in path and f"/{alias}/v_" in path, (
                f"refresh {i}: non-canonical snapshot path {path!r}"
            )

    # Each refresh produced a NEW snapshot timestamp (deterministic minting).
    assert len(set(minted_timestamps)) == _REFRESH_COUNT, (
        f"expected {_REFRESH_COUNT} distinct snapshot timestamps, "
        f"got {minted_timestamps}"
    )
    assert minted_timestamps == sorted(minted_timestamps), (
        f"snapshot timestamps must be strictly increasing: {minted_timestamps}"
    )

    # Mutation: the OLDEST snapshot (refresh #1) has been pruned from disk.
    assert first_snapshot_path is not None
    assert not Path(first_snapshot_path).exists(), (
        f"oldest snapshot {first_snapshot_path!r} must be pruned after refresh-beyond-N"
    )

    # The current (newest) snapshot still exists on disk.
    final_snaps = _list_snapshot_dirs(sm, alias)
    assert final_snaps, "the current snapshot must survive on disk"
    current_path = final_snaps[-1][0]
    assert Path(current_path).exists(), (
        f"current snapshot {current_path!r} must exist on disk"
    )


def _wait_until_count_at_most(
    snapshot_manager: Any, alias: str, max_count: int
) -> list[tuple[str, int]]:
    """Bounded-wait poll until ``list_snapshots`` count <= ``max_count`` (Messi #14).

    The CleanupManager prunes asynchronously (refcount-gated, ~1s loop) -- this
    waits on a monotonic deadline for the surviving count to settle, then returns
    the final ascending snapshot list.
    """
    deadline = time.monotonic() + _PRUNE_WAIT_SECONDS
    snaps = list(snapshot_manager.list_snapshots(alias))
    while time.monotonic() < deadline:
        snaps = list(snapshot_manager.list_snapshots(alias))
        if len(snaps) <= max_count:
            return snaps
        time.sleep(1.0)
    return snaps


# ---------------------------------------------------------------------------
# AC2: query still returns results from the correct (mutable base clone) path
# ---------------------------------------------------------------------------
def test_query_served_from_mutable_base_clone_after_retention(
    retention_repo: dict[str, Any],
) -> None:
    """AC2: after refresh+pruning, the front-door query returns results.

    Also asserts the served path is the Priority-1 mutable base clone
    (``get_actual_repo_path`` -> ``{golden_repos_dir}/{alias}``), NOT a
    ``.versioned`` snapshot, characterised via ``is_immutable_versioned_snapshot``.
    """
    from code_indexer.server.services.query_path_cache import (
        is_immutable_versioned_snapshot,
    )

    client = retention_repo["client"]
    token_provider = retention_repo["token_provider"]
    headers_fn = token_provider.get_headers
    alias = retention_repo["alias"]
    src = retention_repo["source"]
    sm = retention_repo["snapshot_manager"]
    grm = retention_repo["golden_repo_manager"]
    base_clone = retention_repo["base_clone"]

    # Refresh beyond N so pruning has definitely occurred (mutation precondition).
    for i in range(1, _REFRESH_COUNT + 1):
        _commit_and_push_change(src, i)
        _refresh_once(client, headers_fn, alias, f"refresh{i}")
    _wait_until_count_at_most(sm, alias, _KEEP_LAST_N)

    # Front-door query against the globally-activated alias.
    # Fetch fresh headers immediately before the request.
    q = client.post(
        "/api/query",
        json={
            "query_text": "authentication",
            "repository_alias": f"{alias}-global",
            "max_results": 5,
        },
        headers=headers_fn(),
    )
    assert q.status_code == 200, f"query returned HTTP {q.status_code}: {q.text[:300]}"
    body = q.json()
    results = body.get("results")
    assert isinstance(results, list) and len(results) > 0, (
        f"query returned no results: {body}"
    )

    # Path routing evidence: get_actual_repo_path -> Priority-1 mutable base clone.
    actual = grm.get_actual_repo_path(alias)
    assert str(actual) == str(base_clone), (
        f"get_actual_repo_path({alias!r})={actual!r} is not the mutable base clone "
        f"{str(base_clone)!r}"
    )
    assert "/.versioned/" not in str(actual), (
        f"served path {actual!r} must NOT be a .versioned snapshot"
    )
    assert is_immutable_versioned_snapshot(str(actual)) is False, (
        f"base clone {actual!r} must not be classified as an immutable snapshot"
    )
