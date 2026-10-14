"""Lifecycle/description analysis on a pod without the repository.

Under ``content_backend: delegated`` a golden repo's directory on this pod holds
index state only. The orchestrator invoker plugin used to resolve the repository
to provision from that directory's LOCAL git origin -- there is none -- so the
agent got no repository, and cidx refused the analysis (scheduler backfills and
refresh-queueing included). Now:

* ``golden_repo_source`` names the repository from the golden REGISTRY (remote,
  branch) and the commit cidx indexed from git-mcp, registry first;
* the plugin hands those to the orchestrator (the agent opens repoUrl@branch,
  pinned to the commit) and declares it resolves golden repositories itself;
* the analysis, the scheduler backfills and refresh-queueing run under
  delegated exactly when the configured invoker does that -- the built-in
  local CLI (it reads this pod's path) still refuses.

Pinned: parity of the resolved repository with the real clone, fail-loud
(registry or git-mcp failure is an error, never "no repository"), no pod-disk
or local git access when delegated, and the local default byte-identical.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos import lifecycle_claude_cli_invoker as lifecycle
from code_indexer.global_repos import refresh_scheduler
from code_indexer.server.repositories import golden_repo_manager
from code_indexer.server.services import cli_invoker_plugin
from code_indexer.server.services import description_refresh_scheduler as drs
from code_indexer.server.services.delegated_refusal import (
    DelegatedRepositoryUnsupportedError,
)
from code_indexer.server.services.intelligence_cli_invoker import InvocationResult
from code_indexer.server.startup import content_backend_wiring, git_backend_wiring
from code_indexer.server.startup.content_backend_wiring import (
    GoldenRepoSourceError,
    golden_repo_source,
)

_PLUGIN_NAME = "cidx_orchestrator_invoker_under_test"
_PLUGIN = (
    Path(__file__).resolve().parents[4]
    / "plugins"
    / "cidx_orchestrator_invoker"
    / "cidx_orchestrator_invoker"
    / "__init__.py"
)


def _load_plugin() -> ModuleType:
    spec = importlib.util.spec_from_file_location(_PLUGIN_NAME, _PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PLUGIN_NAME] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


# ── a golden registry and git-mcp over a real clone ────────────────────────


@pytest.fixture
def real_clone(tmp_path: Path) -> Path:
    clone = tmp_path / "real" / "my-repo"
    clone.mkdir(parents=True)
    _git(clone, "init", "-q", "-b", "develop")
    (clone / "README.md").write_text("# my-repo\n")
    _git(clone, "add", "-A")
    _git(clone, "-c", "user.name=a", "-c", "user.email=a@b", "commit", "-qm", "one")
    return clone


@pytest.fixture
def golden_root(tmp_path: Path) -> Path:
    """The pod's golden-repos directory (index state only; no repository)."""
    root = tmp_path / "pod" / "golden-repos"
    return root


class GitMcp:
    """git-mcp's rev_parse, answered from the real clone the pod lacks."""

    def __init__(self, pod_clone: Path, real: Path) -> None:
        self.pod_clone, self.real = str(pod_clone), real
        self.calls: List[Any] = []
        self.fail: Optional[Exception] = None

    def rev_parse(self, repo_path: str, *args: str, timeout: Any = None) -> str:
        self.calls.append((repo_path, args))
        if self.fail is not None:
            raise self.fail
        assert repo_path == self.pod_clone, repo_path
        return _git(self.real, "rev-parse", *args)


class Registry:
    def __init__(self, golden_root: Path, repos: Dict[str, Any]) -> None:
        self.golden_repos_dir = str(golden_root)
        self.repos = repos
        self.lookups: List[str] = []
        self.fail: Optional[Exception] = None

    def get_golden_repo(self, alias: str) -> Any:
        self.lookups.append(alias)
        if self.fail is not None:
            raise self.fail
        return self.repos.get(alias)


@pytest.fixture
def registry(golden_root: Path, monkeypatch: pytest.MonkeyPatch) -> Registry:
    reg = Registry(
        golden_root,
        {
            "my-repo": SimpleNamespace(
                alias="my-repo",
                repo_url="https://user:s3cret@gitlab.example.com/g/my-repo.git",
                default_branch="develop",
                clone_path=str(golden_root / "my-repo"),
            ),
            "traces": SimpleNamespace(
                alias="traces",
                repo_url="local://traces",
                default_branch="main",
                clone_path=str(golden_root / "traces"),
            ),
        },
    )
    monkeypatch.setattr(golden_repo_manager, "get_golden_repo_manager", lambda: reg)
    return reg


@pytest.fixture
def git_mcp(
    golden_root: Path, real_clone: Path, monkeypatch: pytest.MonkeyPatch
) -> GitMcp:
    seam = GitMcp(golden_root / "my-repo", real_clone)
    monkeypatch.setattr(
        git_backend_wiring, "server_delegated_git_backend", lambda: seam
    )
    return seam


def _park(monkeypatch: pytest.MonkeyPatch, *, local: Optional[bool]) -> None:
    backend = None if local is None else SimpleNamespace(is_local=local)
    monkeypatch.setattr(
        content_backend_wiring, "server_content_backend", lambda: backend
    )


@pytest.fixture
def delegated(monkeypatch: pytest.MonkeyPatch) -> None:
    _park(monkeypatch, local=False)


def _no_local_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """The plugin's local-clone resolution (local git at cwd) must not run."""

    def forbidden(cwd: str) -> Any:
        raise AssertionError(f"local git on a delegated pod at {cwd}")

    monkeypatch.setattr(
        plugin.OrchestratorInvoker, "_resolve_local_repo", staticmethod(forbidden)
    )


# ── golden_repo_source: registry first, then git-mcp ───────────────────────


@pytest.mark.parametrize(
    "relative", ["my-repo", ".versioned/my-repo/v_1700000000"], ids=["base", "snap"]
)
def test_the_repository_is_the_registry_entry_at_the_indexed_commit(
    golden_root: Path,
    registry: Registry,
    git_mcp: GitMcp,
    real_clone: Path,
    relative: str,
) -> None:
    source = golden_repo_source(golden_root / relative)
    assert source == {
        "alias": "my-repo",
        "repoUrl": "https://user:s3cret@gitlab.example.com/g/my-repo.git",
        "branch": "develop",
        "commit": _git(real_clone, "rev-parse", "HEAD"),
    }
    assert git_mcp.calls == [(str(golden_root / "my-repo"), ("HEAD",))]
    assert not (golden_root / relative).exists()


@pytest.mark.parametrize(
    "path",
    ["/tmp/scratch-dir", "golden-repos-elsewhere/my-repo", "pod/golden-repos"],
)
def test_a_path_that_is_no_golden_repo_names_no_repository(
    tmp_path: Path, registry: Registry, git_mcp: GitMcp, path: str
) -> None:
    target = Path(path) if path.startswith("/") else tmp_path / path
    assert golden_repo_source(target) is None
    assert registry.lookups == [] and git_mcp.calls == []


def test_an_unregistered_golden_repo_fails_loud_before_git_mcp(
    golden_root: Path, registry: Registry, git_mcp: GitMcp
) -> None:
    with pytest.raises(GoldenRepoSourceError, match="not a registered golden repo"):
        golden_repo_source(golden_root / "gone")
    assert git_mcp.calls == []


def test_a_registry_failure_fails_loud(
    golden_root: Path, registry: Registry, git_mcp: GitMcp
) -> None:
    registry.fail = OSError("db locked")
    with pytest.raises(GoldenRepoSourceError, match="db locked"):
        golden_repo_source(golden_root / "my-repo")
    assert git_mcp.calls == []


def test_a_git_mcp_failure_fails_loud(
    golden_root: Path, registry: Registry, git_mcp: GitMcp
) -> None:
    git_mcp.fail = subprocess.CalledProcessError(128, ["git-mcp"], stderr="boom")
    with pytest.raises(GoldenRepoSourceError, match="could not resolve HEAD"):
        golden_repo_source(golden_root / "my-repo")


def test_a_local_git_backend_fails_loud(
    golden_root: Path, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        git_backend_wiring, "server_delegated_git_backend", lambda: None
    )
    with pytest.raises(GoldenRepoSourceError, match="git_backend is 'local'"):
        golden_repo_source(golden_root / "my-repo")


def test_a_pod_local_repository_cannot_be_provisioned_elsewhere(
    golden_root: Path, registry: Registry, git_mcp: GitMcp
) -> None:
    with pytest.raises(GoldenRepoSourceError, match="local://traces"):
        golden_repo_source(golden_root / "traces")
    assert git_mcp.calls == []


# ── the plugin: what it hands the orchestrator ─────────────────────────────


class Orchestrator:
    """Records the submitted job and answers it as completed."""

    def __init__(self) -> None:
        self.submitted: List[Dict[str, Any]] = []

    def request(self, path: str, **kwargs: Any) -> Dict[str, Any]:
        if path == "/auth/login":
            return {"token": "t"}
        if path == "/jobs/with-capabilities":
            self.submitted.append(kwargs["body"])
            return {"jobId": kwargs["body"]["jobId"]}
        return {"status": "completed", "result": {"output": "{}", "exitCode": 0}}


@pytest.fixture
def orchestrator(monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    orch = Orchestrator()
    monkeypatch.setenv("CIDX_ORCH_PASSWORD", "pw")
    monkeypatch.setattr(
        plugin.OrchestratorInvoker,
        "_request",
        lambda self, path, **kw: orch.request(path, **kw),
    )
    return orch


def test_delegated_job_provisions_the_registered_repo_at_the_indexed_commit(
    delegated: None,
    golden_root: Path,
    registry: Registry,
    git_mcp: GitMcp,
    real_clone: Path,
    orchestrator: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_local_git(monkeypatch)
    cwd = str(golden_root / "my-repo")
    result = plugin.make_invoker().invoke("repo_lifecycle", cwd, "analyze", 60)
    assert result.success, result.error
    (body,) = orchestrator.submitted
    head = _git(real_clone, "rev-parse", "HEAD")
    assert body["repoUrl"] == "https://gitlab.example.com/g/my-repo.git"
    assert body["branch"] == "develop"
    assert body["options"]["gitAware"] is True
    assert body["metadata"] == {"source": "cidx", "cwd": cwd, "commit": head}
    assert head in body["systemPrompt"] and "develop" in body["systemPrompt"]
    assert "s3cret" not in json.dumps(body)


def test_delegated_job_about_no_golden_repo_carries_no_repository(
    delegated: None,
    registry: Registry,
    git_mcp: GitMcp,
    orchestrator: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_local_git(monkeypatch)
    result = plugin.make_invoker().invoke("self_monitoring", "/tmp/x", "scan", 60)
    assert result.success
    (body,) = orchestrator.submitted
    assert "repoUrl" not in body and "systemPrompt" not in body
    assert body["options"]["gitAware"] is False


@pytest.mark.parametrize("failure", ["registry", "git-mcp"])
def test_delegated_resolution_failure_fails_the_job_and_submits_nothing(
    delegated: None,
    golden_root: Path,
    registry: Registry,
    git_mcp: GitMcp,
    orchestrator: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    _no_local_git(monkeypatch)
    if failure == "registry":
        registry.fail = OSError("db locked")
    else:
        git_mcp.fail = subprocess.CalledProcessError(1, ["git-mcp"], stderr="down")
    result = plugin.make_invoker().invoke(
        "repo_lifecycle", str(golden_root / "my-repo"), "analyze", 60
    )
    assert result.success is False
    assert ("db locked" if failure == "registry" else "down") in result.error
    assert orchestrator.submitted == []


@pytest.mark.parametrize("local", [True, None], ids=["local-backend", "no-server"])
def test_local_default_body_is_unchanged(
    real_clone: Path,
    orchestrator: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
    local: Optional[bool],
) -> None:
    """The local path reads the clone's own origin and branch, as before:
    exactly these keys, no commit, no system prompt, no registry lookup."""
    _park(monkeypatch, local=local)
    monkeypatch.setattr(
        content_backend_wiring,
        "golden_repo_source",
        MagicMock(side_effect=AssertionError("registry consulted locally")),
    )
    _git(real_clone, "remote", "add", "origin", "https://u:p@h.example/r.git")
    plugin.make_invoker().invoke("repo_lifecycle", str(real_clone), "p", 60)
    (body,) = orchestrator.submitted
    assert body == {
        "jobId": body["jobId"],
        "prompt": "p",
        "options": {
            "agentEngine": "claude",
            "model": "opus",
            "timeout": 60,
            "outputMode": "result",
            "cidxAware": False,
            "gitAware": True,
        },
        "metadata": {"source": "cidx", "cwd": str(real_clone)},
        "repoUrl": "https://h.example/r.git",
        "branch": "develop",
    }


# ── the gate: analysis runs under delegated with a resolving invoker ────────


@pytest.fixture
def no_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_invoker_plugin, "_cached", cli_invoker_plugin._NO_PLUGIN)


@pytest.fixture
def orchestrator_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_invoker_plugin, "_cached", plugin.make_invoker)


def test_the_orchestrator_plugin_resolves_golden_repositories(
    orchestrator_plugin: None,
) -> None:
    assert cli_invoker_plugin.invoker_resolves_golden_repositories() is True


def test_the_builtin_cli_and_undeclared_plugins_do_not(
    no_plugin: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cli_invoker_plugin.invoker_resolves_golden_repositories() is False

    def make_other(model: str, timeout: Optional[int]) -> Any:
        return None

    make_other.__module__ = "json"  # a module that declares nothing
    monkeypatch.setattr(cli_invoker_plugin, "_cached", make_other)
    assert cli_invoker_plugin.invoker_resolves_golden_repositories() is False


def test_local_default_never_consults_the_invoker_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _park(monkeypatch, local=True)
    monkeypatch.setattr(
        cli_invoker_plugin,
        "get_invoker_factory",
        MagicMock(side_effect=AssertionError("plugin consulted locally")),
    )
    assert lifecycle.lifecycle_analysis_refusal() is None
    assert drs._lifecycle_analysis_refusal() is None
    assert refresh_scheduler._description_generation_refusal() is None


def test_delegated_refuses_with_the_builtin_cli(
    delegated: None, no_plugin: None
) -> None:
    for refusal in (
        lifecycle.lifecycle_analysis_refusal(),
        drs._lifecycle_analysis_refusal(),
        refresh_scheduler._description_generation_refusal(),
    ):
        assert refusal is not None and "cidx_orchestrator_invoker" in refusal


def test_delegated_runs_with_the_orchestrator_plugin(
    delegated: None, orchestrator_plugin: None
) -> None:
    assert lifecycle.lifecycle_analysis_refusal() is None
    assert drs._lifecycle_analysis_refusal() is None
    assert refresh_scheduler._description_generation_refusal() is None


@pytest.fixture
def dispatcher() -> Iterator[MagicMock]:
    disp = MagicMock()
    disp.dispatch.return_value = InvocationResult(
        success=True,
        output=json.dumps({"description": "A service.", "lifecycle": {}}),
        error="",
        cli_used="orchestrator",
        was_failover=False,
    )
    svc = MagicMock()
    svc.get_config.return_value.lifecycle_analysis_config.outer_timeout_seconds = 9
    with (
        patch.object(lifecycle, "get_config_service", return_value=svc),
        patch.object(lifecycle, "build_dep_map_dispatcher", return_value=disp),
        patch.object(
            lifecycle,
            "_parse_with_optional_section_fallback",
            side_effect=lambda output: output,
        ),
    ):
        yield disp


def test_lifecycle_runs_delegated_without_looking_at_the_pod_path(
    delegated: None,
    orchestrator_plugin: None,
    dispatcher: MagicMock,
    golden_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pod_path = golden_root / "my-repo"
    for name in ("exists", "is_dir", "stat"):
        original = getattr(Path, name)

        def guarded(self: Path, *a: Any, _o: Any = original, **k: Any) -> Any:
            if str(self).startswith(str(pod_path)):
                raise AssertionError(f"{self} looked at on a delegated pod")
            return _o(self, *a, **k)

        monkeypatch.setattr(Path, name, guarded)
    lifecycle.LifecycleClaudeCliInvoker()("my-repo", pod_path)
    assert dispatcher.dispatch.call_args.kwargs["cwd"] == str(pod_path)


def test_lifecycle_still_refuses_delegated_with_the_builtin_cli(
    delegated: None, no_plugin: None, dispatcher: MagicMock, golden_root: Path
) -> None:
    with pytest.raises(DelegatedRepositoryUnsupportedError):
        lifecycle.LifecycleClaudeCliInvoker()("my-repo", golden_root / "my-repo")
    dispatcher.dispatch.assert_not_called()


def test_lifecycle_local_still_checks_the_clone_exists(
    monkeypatch: pytest.MonkeyPatch, dispatcher: MagicMock, golden_root: Path
) -> None:
    from code_indexer.global_repos.orphaned_repo_error import OrphanedRepoError

    _park(monkeypatch, local=True)
    with pytest.raises(OrphanedRepoError):
        lifecycle.LifecycleClaudeCliInvoker()("my-repo", golden_root / "my-repo")
    dispatcher.dispatch.assert_not_called()


def _scheduler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    config = SimpleNamespace(
        claude_integration_config=SimpleNamespace(
            description_refresh_enabled=True,
            description_refresh_interval_hours=24,
            max_concurrent_claude_cli=1,
        )
    )
    config_manager = MagicMock()
    config_manager.load_config.return_value = config
    scheduler = drs.DescriptionRefreshScheduler(
        db_path=str(tmp_path / "db.sqlite"), config_manager=config_manager
    )
    calls = MagicMock()
    for name in (
        "_migrate_global_suffix_filenames",
        "reconcile_orphan_tracking",
        "_reconcile_stale_next_run_rows",
        "reconcile_broken_lifecycle_metadata",
        "reconcile_terse_descriptions",
        "_run_loop",
    ):
        monkeypatch.setattr(scheduler, name, getattr(calls, name))
    return scheduler, calls


def test_scheduler_backfills_start_delegated_with_the_orchestrator_plugin(
    tmp_path: Path,
    delegated: None,
    orchestrator_plugin: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler, calls = _scheduler(tmp_path, monkeypatch)
    scheduler.start()
    scheduler._thread.join(timeout=5)
    called = {c[0] for c in calls.method_calls}
    assert {
        "reconcile_broken_lifecycle_metadata",
        "reconcile_terse_descriptions",
        "_run_loop",
    } <= called


@pytest.mark.parametrize("resolving", [True, False], ids=["orchestrator", "builtin"])
def test_refresh_queues_a_missing_description_delegated_with_the_plugin(
    tmp_path: Path,
    delegated: None,
    monkeypatch: pytest.MonkeyPatch,
    resolving: bool,
) -> None:
    monkeypatch.setattr(
        cli_invoker_plugin,
        "_cached",
        plugin.make_invoker if resolving else cli_invoker_plugin._NO_PLUGIN,
    )
    monkeypatch.setattr(
        refresh_scheduler.cidx_meta_content,
        "shared_cidx_meta",
        lambda d: SimpleNamespace(exists=lambda p: False),
    )
    scheduler = refresh_scheduler.RefreshScheduler.__new__(
        refresh_scheduler.RefreshScheduler
    )
    scheduler.golden_repos_dir = tmp_path / "golden-repos"
    manager = MagicMock()
    queued = scheduler._queue_missing_description(
        "my-repo-global", tmp_path / "golden-repos" / "my-repo", manager
    )
    assert queued is resolving
    assert manager.submit_work.call_count == (1 if resolving else 0)
