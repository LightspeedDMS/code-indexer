"""EVO-66445: unit tests for the reliable, non-silent FTS index builder."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typing import List


from code_indexer.server.services.fts_index_builder import (
    FTS_INDEX_DIRNAME,
    FtsBuildStatus,
    ensure_fts_index,
    fts_backfill_skip_present,
    fts_index_present,
)

# The exact stdout/stderr split the real cidx CLI emits on a file-less repo: the
# "local mode" banner goes to STDERR, the actual error to STDOUT.
_NO_FILES_STDOUT = "🔍 Discovering files...\n❌ No files found to index"
_LOCAL_MODE_STDERR = "🔧 Running in local mode"


def _make_repo(tmp_path: Path, *, config: bool = True, fts: bool = False) -> Path:
    ci = tmp_path / ".code-indexer"
    ci.mkdir(parents=True)
    if config:
        (ci / "config.json").write_text("{}")
    if fts:
        d = ci / FTS_INDEX_DIRNAME
        d.mkdir()
        (d / "meta.json").write_text("{}")
    return tmp_path


class _FakeRunner:
    """Records ``cidx`` subprocess calls; can materialize the FTS marker on rebuild.

    ``build_on_rebuild`` writes the tantivy meta.json marker when it sees the
    ``--rebuild-fts-index`` command, simulating a successful build. ``returncodes``
    maps a matching command token -> return code (default 0).
    """

    def __init__(
        self,
        repo_path: Path,
        *,
        build_on_rebuild: bool = True,
        init_rc: int = 0,
        rebuild_rc: int = 0,
        rebuild_stdout: str = "",
        rebuild_stderr: str = "",
    ) -> None:
        self.repo_path = repo_path
        self.build_on_rebuild = build_on_rebuild
        self.init_rc = init_rc
        self.rebuild_rc = rebuild_rc
        self.rebuild_stdout = rebuild_stdout
        self.rebuild_stderr = rebuild_stderr
        self.calls: List[List[str]] = []

    def __call__(self, cmd, **kwargs) -> "subprocess.CompletedProcess[str]":
        self.calls.append(list(cmd))
        rc = 0
        out, err = "", ""
        if "init" in cmd:
            rc = self.init_rc
            if rc != 0:
                err = "init boom"
        elif "--rebuild-fts-index" in cmd:
            rc = self.rebuild_rc
            if rc != 0:
                # Explicit stdout/stderr override lets a test reproduce the real CLI
                # split (banner on stderr, error on stdout); else a generic message.
                out = self.rebuild_stdout
                err = self.rebuild_stderr or (
                    "rebuild boom" if not self.rebuild_stdout else ""
                )
            elif self.build_on_rebuild:
                d = self.repo_path / ".code-indexer" / FTS_INDEX_DIRNAME
                d.mkdir(parents=True, exist_ok=True)
                (d / "meta.json").write_text("{}")
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=err)

    def ran(self, token: str) -> bool:
        return any(token in c for c in self.calls)


def test_present_index_is_a_noop(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, fts=True)
    runner = _FakeRunner(repo)
    outcome = ensure_fts_index(repo, alias="a", runner=runner)
    assert outcome.status is FtsBuildStatus.PRESENT
    assert outcome.ok
    assert runner.calls == []  # never shells out when the index is already built


def test_missing_index_with_config_rebuilds(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, config=True, fts=False)
    runner = _FakeRunner(repo, build_on_rebuild=True)
    outcome = ensure_fts_index(repo, alias="a", runner=runner)
    assert outcome.status is FtsBuildStatus.REBUILT
    assert outcome.ok
    assert fts_index_present(repo)
    assert runner.ran("--rebuild-fts-index")
    assert not runner.ran("init")  # config present -> no init-guard needed


def test_missing_config_triggers_init_then_rebuild(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, config=False, fts=False)
    runner = _FakeRunner(repo, build_on_rebuild=True)
    outcome = ensure_fts_index(repo, alias="a", runner=runner)
    assert outcome.status is FtsBuildStatus.REBUILT
    assert runner.ran("init")
    assert runner.ran("--rebuild-fts-index")


def test_rebuild_nonzero_is_failed_and_loud(tmp_path: Path, caplog) -> None:
    repo = _make_repo(tmp_path, config=True, fts=False)
    runner = _FakeRunner(repo, rebuild_rc=1)
    with caplog.at_level("ERROR"):
        outcome = ensure_fts_index(repo, alias="evolution", runner=runner)
    assert outcome.status is FtsBuildStatus.FAILED
    assert not outcome.ok
    assert "evolution" in caplog.text  # surfaced loudly with the alias


def test_exit_zero_but_no_index_is_failed_not_silent(tmp_path: Path, caplog) -> None:
    """The core anti-silent guard: rebuild 'succeeds' but writes no index."""
    repo = _make_repo(tmp_path, config=True, fts=False)
    runner = _FakeRunner(repo, build_on_rebuild=False, rebuild_rc=0)
    with caplog.at_level("ERROR"):
        outcome = ensure_fts_index(repo, alias="a", runner=runner)
    assert outcome.status is FtsBuildStatus.FAILED
    assert "no tantivy_index" in outcome.detail
    assert "despite exit 0" in caplog.text


def test_init_failure_is_failed_without_rebuild(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, config=False, fts=False)
    runner = _FakeRunner(repo, init_rc=1)
    outcome = ensure_fts_index(repo, alias="a", runner=runner)
    assert outcome.status is FtsBuildStatus.FAILED
    assert "init failed" in outcome.detail
    assert not runner.ran("--rebuild-fts-index")  # never attempts rebuild if init fails


def test_subprocess_launch_failure_is_failed_not_raised(tmp_path: Path, caplog) -> None:
    """CRITICAL (MR !69): a subprocess that fails to LAUNCH (cidx not on PATH,
    permission error) must become a FAILED outcome, never propagate -- else the
    golden-repo registration's except clauses roll back the whole registration."""
    repo = _make_repo(tmp_path, config=True, fts=False)

    def boom_runner(cmd, **kwargs):
        raise FileNotFoundError("cidx not found on PATH")

    with caplog.at_level("ERROR"):
        outcome = ensure_fts_index(repo, alias="evolution", runner=boom_runner)
    assert outcome.status is FtsBuildStatus.FAILED
    assert "subprocess error" in outcome.detail
    assert "evolution" in caplog.text


def test_timeout_expired_is_failed_not_raised(tmp_path: Path) -> None:
    """A hung build that hits the bounded sweep timeout is FAILED, not an exception."""
    repo = _make_repo(tmp_path, config=True, fts=False)

    def slow_runner(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 30)

    outcome = ensure_fts_index(repo, alias="a", runner=slow_runner, timeout=30)
    assert outcome.status is FtsBuildStatus.FAILED
    assert "subprocess error" in outcome.detail


# ── EVO-66445 follow-up: no-files repos are SKIPPED (marked), and _detail is honest ──


def _skip_marker(repo: Path) -> Path:
    return repo / ".code-indexer" / "fts_backfill_no_files.skip"


def test_no_files_repo_is_skipped_and_marked(tmp_path: Path, caplog) -> None:
    """A file-less repo (rebuild exits 1 with 'No files found') is SKIPPED, not FAILED,
    and gets a skip marker so discovery stops re-attempting it."""
    repo = _make_repo(tmp_path, config=True, fts=False)
    runner = _FakeRunner(
        repo,
        rebuild_rc=1,
        rebuild_stdout=_NO_FILES_STDOUT,
        rebuild_stderr=_LOCAL_MODE_STDERR,
    )
    with caplog.at_level("ERROR"):
        outcome = ensure_fts_index(repo, alias="cow-storage-daemon-2", runner=runner)
    assert outcome.status is FtsBuildStatus.SKIPPED
    assert not outcome.ok  # no usable index, but explicitly not a failure
    assert _skip_marker(repo).exists()
    assert fts_backfill_skip_present(repo)
    # A skip is expected, not an error: nothing logged at ERROR.
    assert caplog.text == ""


def test_detail_surfaces_stdout_error_not_stderr_banner(tmp_path: Path) -> None:
    """A genuine failure must report the real ❌ error (stdout), not the
    '🔧 Running in local mode' banner (stderr) that _detail used to prefer."""
    repo = _make_repo(tmp_path, config=True, fts=False)
    runner = _FakeRunner(
        repo,
        rebuild_rc=1,
        rebuild_stdout="🔍 Discovering files...\n❌ Tantivy write failed: disk full",
        rebuild_stderr=_LOCAL_MODE_STDERR,
    )
    outcome = ensure_fts_index(repo, alias="a", runner=runner)
    assert outcome.status is FtsBuildStatus.FAILED  # not the no-files signal
    assert "disk full" in outcome.detail
    assert "Running in local mode" not in outcome.detail


def test_successful_rebuild_clears_a_stale_skip_marker(tmp_path: Path) -> None:
    """A repo previously marked file-less that now builds must have the mark cleared,
    so discovery picks it up normally."""
    repo = _make_repo(tmp_path, config=True, fts=False)
    _skip_marker(repo).write_text("")  # stale mark from a prior empty state
    assert fts_backfill_skip_present(repo)
    runner = _FakeRunner(repo, build_on_rebuild=True)
    outcome = ensure_fts_index(repo, alias="a", runner=runner)
    assert outcome.status is FtsBuildStatus.REBUILT
    assert not _skip_marker(repo).exists()
    assert not fts_backfill_skip_present(repo)


# ── pod-local repositories under a delegated server (!148) ─────────────────


@pytest.fixture
def delegated_server(monkeypatch: pytest.MonkeyPatch) -> None:
    from code_indexer.server.utils import index_command_layout as icl

    monkeypatch.setattr(icl, "configured_content_backend_name", lambda: "delegated")
    monkeypatch.setattr(icl, "configured_git_backend_name", lambda: "delegated")


def _rebuild_cmd(runner: "_FakeRunner") -> List[str]:
    return next(c for c in runner.calls if "--rebuild-fts-index" in c)


@pytest.mark.parametrize(
    "alias, local_content, delegated",
    [
        ("notes", True, False),  # a local:// repo: the caller says so
        ("cidx-meta", False, False),  # cidx-meta: the alias says so
        ("agent-router", False, True),  # with workspace-mcp: delegated
    ],
)
def test_the_rebuild_reads_the_pod_for_a_pod_local_repo(
    tmp_path: Path,
    delegated_server: None,
    alias: str,
    local_content: bool,
    delegated: bool,
) -> None:
    repo = _make_repo(tmp_path, config=True, fts=False)
    runner = _FakeRunner(repo)

    ensure_fts_index(repo, alias=alias, runner=runner, local_content=local_content)

    cmd = _rebuild_cmd(runner)
    assert ("--content-backend=delegated" in cmd) is delegated
    assert ("--git-backend=delegated" in cmd) is delegated


def test_the_backfill_sweep_asks_the_registry_whether_a_repo_is_pod_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from code_indexer.server.repositories import golden_repo_manager as grm
    from code_indexer.server.services.fts_backfill import policy

    monkeypatch.setattr(
        grm,
        "get_golden_repo_manager",
        lambda: SimpleNamespace(
            get_golden_repo=lambda alias: SimpleNamespace(repo_url="local://notes")
        ),
    )
    seen = {}

    def fake_ensure(repo_path, **kw):
        seen.update(kw)
        return SimpleNamespace(
            status=FtsBuildStatus.PRESENT, alias=kw["alias"], detail="", ok=True
        )

    monkeypatch.setattr(policy, "ensure_fts_index", fake_ensure)

    policy.fts_backfill_build_executor(
        {"repo_path": str(tmp_path / "notes"), "alias": "notes"}
    )

    assert seen["local_content"] is True
