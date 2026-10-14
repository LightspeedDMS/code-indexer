"""Golden-repo lifecycle/description analysis: refused under a delegated backend.

Both analyses hand an agent THIS pod's clone path: the lifecycle invoker
dispatches with ``cwd`` there (a local Claude CLI reads it with its own file
tools; the orchestrator invoker plugin resolves the repo to provision from that
path's local git origin) and RepoAnalyzer runs the Claude CLI with
``--add-dir`` on it. A mount-less pod holds index state at most there, so the
agent would describe an empty tree and a plausible description would land in
cidx-meta. Under ``content_backend: delegated`` both refuse BEFORE the first
local access; under the local backend (or no running server) they run exactly
as before.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.lifecycle_claude_cli_invoker import (
    LIFECYCLE_ANALYSIS_FEATURE,
    LifecycleClaudeCliInvoker,
)
from code_indexer.global_repos.repo_analyzer import (
    DESCRIPTION_ANALYSIS_FEATURE,
    RepoAnalyzer,
)
from code_indexer.server.services.delegated_refusal import (
    DelegatedRepositoryUnsupportedError,
)
from code_indexer.server.services.intelligence_cli_invoker import InvocationResult
from code_indexer.server.startup import content_backend_wiring

_INVOKER = "code_indexer.global_repos.lifecycle_claude_cli_invoker"
_ANALYZER = "code_indexer.global_repos.repo_analyzer"


class LocalIOForbidden(AssertionError):
    """A delegated run touched the pod's clone path."""


def _park(monkeypatch: pytest.MonkeyPatch, backend: object) -> None:
    monkeypatch.setattr(
        content_backend_wiring, "server_content_backend", lambda: backend
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "golden-repos" / "my-repo"
    path.mkdir(parents=True)
    (path / "README.md").write_text("# my-repo\n")
    return path


def _forbid_local_io(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Every stat/listing/read under *root* raises."""
    prefix = str(root)

    for name in ("exists", "is_dir", "is_file", "iterdir", "read_text", "stat"):
        original = getattr(Path, name)

        def guarded(self: Path, *a: Any, _o: Any = original, **k: Any) -> Any:
            if str(self).startswith(prefix):
                raise LocalIOForbidden(f"{self} touched on a delegated pod")
            return _o(self, *a, **k)

        monkeypatch.setattr(Path, name, guarded)


def _lifecycle_config() -> MagicMock:
    cfg = MagicMock()
    cfg.lifecycle_analysis_config.outer_timeout_seconds = 420
    cfg.lifecycle_analysis_config.shell_timeout_seconds = 360
    return cfg


_UNIFIED_OUTPUT = json.dumps(
    {
        "description": "A Python service.",
        "lifecycle": {
            "ci_system": "github-actions",
            "deployment_target": "pypi",
            "language_ecosystem": "python/poetry",
            "build_system": "poetry",
            "testing_framework": "pytest",
            "confidence": "high",
            "branching": {
                "default_branch": "main",
                "model": "trunk-based",
                "release_branches": False,
            },
            "ci": {
                "deploy_on": ["push"],
                "environments": ["prod"],
                "required_checks": ["lint"],
            },
            "release": {
                "versioning": "semver",
                "version_source": "pyproject.toml",
                "changelog": "CHANGELOG.md",
            },
        },
    }
)


@pytest.fixture
def dispatcher() -> Iterator[MagicMock]:
    disp = MagicMock()
    disp.dispatch.return_value = InvocationResult(
        success=True,
        output=_UNIFIED_OUTPUT,
        error="",
        cli_used="claude",
        was_failover=False,
    )
    svc = MagicMock()
    svc.get_config.return_value = _lifecycle_config()
    with (
        patch(f"{_INVOKER}.get_config_service", return_value=svc),
        patch(f"{_INVOKER}.build_dep_map_dispatcher", return_value=disp),
    ):
        yield disp


# ---------------------------------------------------------------------------
# LifecycleClaudeCliInvoker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "backend", [None, SimpleNamespace(is_local=True)], ids=["no-server", "local"]
)
def test_lifecycle_local_dispatches_with_cwd_in_the_clone(
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    dispatcher: MagicMock,
    backend: object,
) -> None:
    """Parity: local behaviour unchanged -- one dispatch, cwd = the clone."""
    _park(monkeypatch, backend)
    result = LifecycleClaudeCliInvoker()("my-repo", repo)
    dispatcher.dispatch.assert_called_once()
    assert dispatcher.dispatch.call_args.kwargs["cwd"] == str(repo)
    assert dispatcher.dispatch.call_args.kwargs["flow"] == "repo_lifecycle"
    assert result.description == "A Python service."


def test_lifecycle_delegated_refuses_before_any_local_access(
    monkeypatch: pytest.MonkeyPatch, repo: Path, dispatcher: MagicMock
) -> None:
    _park(monkeypatch, SimpleNamespace(is_local=False))
    _forbid_local_io(monkeypatch, repo)
    with pytest.raises(DelegatedRepositoryUnsupportedError) as caught:
        LifecycleClaudeCliInvoker()("my-repo", repo)
    assert caught.value.feature == LIFECYCLE_ANALYSIS_FEATURE
    assert isinstance(caught.value, RuntimeError)
    dispatcher.dispatch.assert_not_called()


def test_lifecycle_delegated_refusal_is_a_per_alias_failure_not_a_skip(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    """The batch runner only SKIPS OrphanedRepoError; the refusal must not be
    one, or a delegated run would silently 'succeed' with nothing written."""
    from code_indexer.global_repos.orphaned_repo_error import OrphanedRepoError

    _park(monkeypatch, SimpleNamespace(is_local=False))
    with pytest.raises(DelegatedRepositoryUnsupportedError) as caught:
        LifecycleClaudeCliInvoker()("my-repo", repo)
    assert not isinstance(caught.value, OrphanedRepoError)


# ---------------------------------------------------------------------------
# RepoAnalyzer.extract_info (description generation on golden-repo add)
# ---------------------------------------------------------------------------


def _manager() -> MagicMock:
    manager = MagicMock()
    manager.check_cli_available.return_value = True
    manager.sync_api_key.return_value = None
    return manager


_CLAUDE_JSON = json.dumps(
    {
        "summary": "Does things.",
        "technologies": ["Python"],
        "features": ["f"],
        "use_cases": ["u"],
        "purpose": "library",
    }
)


@pytest.mark.parametrize(
    "backend", [None, SimpleNamespace(is_local=True)], ids=["no-server", "local"]
)
def test_description_local_runs_claude_on_the_clone(
    monkeypatch: pytest.MonkeyPatch, repo: Path, backend: object
) -> None:
    _park(monkeypatch, backend)
    svc = MagicMock()
    svc.get_config.return_value = _lifecycle_config()
    with (
        patch(f"{_ANALYZER}.get_config_service", return_value=svc),
        patch(
            f"{_ANALYZER}.invoke_claude_cli", return_value=(True, _CLAUDE_JSON)
        ) as invoke,
    ):
        info = RepoAnalyzer(str(repo), claude_cli_manager=_manager()).extract_info()
    invoke.assert_called_once()
    assert invoke.call_args.args[0] == str(repo)
    assert info.summary == "Does things."
    assert info.technologies == ["Python"]


def test_description_delegated_refuses_before_claude_runs(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    _park(monkeypatch, SimpleNamespace(is_local=False))
    _forbid_local_io(monkeypatch, repo)
    manager = _manager()
    with patch(f"{_ANALYZER}.invoke_claude_cli") as invoke:
        with pytest.raises(DelegatedRepositoryUnsupportedError) as caught:
            RepoAnalyzer(str(repo), claude_cli_manager=manager).extract_info()
    assert caught.value.feature == DESCRIPTION_ANALYSIS_FEATURE
    invoke.assert_not_called()
    manager.sync_api_key.assert_not_called()


def test_description_refusal_propagates_out_of_generation(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    """_generate_repo_description does not swallow the refusal into a stub."""
    from code_indexer.global_repos import meta_description_hook
    from code_indexer.server.services.claude_cli_manager import ClaudeCliManager

    _park(monkeypatch, SimpleNamespace(is_local=False))
    manager = MagicMock(spec=ClaudeCliManager)
    with pytest.raises(DelegatedRepositoryUnsupportedError):
        meta_description_hook._generate_repo_description(
            "my-repo", "https://example.invalid/r.git", str(repo), manager
        )
