"""issue-64616: description generation honours the CLI invoker plugin.

Root cause: the golden-repo add hook (``meta_description_hook.on_repo_added``)
always went to the local ``claude`` CLI through ClaudeCliManager. Every other
analysis path dispatches through ``cli_invoker_plugin`` -- under which the
deployment must NOT run the CLI locally -- so on a delegated pod with the
orchestrator plugin installed, every new repo logged "Claude CLI not available
on PATH" at ERROR. The image and the config were right; this one path never
consulted the seam.

Now:
* with a plugin configured the hook leaves the description to the lifecycle
  path (which dispatches through the plugin) and never touches the local CLI;
* with no plugin the hook behaves exactly as before (local default parity);
* ``content_backend=delegated`` without a plugin fails ONCE, at startup,
  naming the fix, instead of once per repo for the life of the server --
  unless ``golden_description_generation_enabled`` is off, the documented
  opt-out, which must keep starting a plugin-less delegated pod.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos import meta_description_hook
from code_indexer.server.services import cli_invoker_plugin
from code_indexer.server.services.cli_invoker_plugin import (
    ENV_VAR,
    CliInvokerPluginError,
    require_invoker_for_delegated_content,
)
from code_indexer.server.startup import service_init
from code_indexer.server.utils.config_manager import (
    IndexingConfig,
    ServerConfig,
    golden_description_generation_enabled,
)

_HOOK = "code_indexer.global_repos.meta_description_hook"


def _factory(analysis_model: str, soft_timeout_seconds: Optional[int]) -> Any:
    return MagicMock()


def _set_plugin(monkeypatch: pytest.MonkeyPatch, factory: Any) -> None:
    monkeypatch.setattr(cli_invoker_plugin, "get_invoker_factory", lambda: factory)


class TestStartupCheck:
    def test_delegated_without_plugin_fails_naming_the_fix(self, monkeypatch) -> None:
        _set_plugin(monkeypatch, None)

        with pytest.raises(CliInvokerPluginError) as raised:
            require_invoker_for_delegated_content(
                "delegated", description_generation_enabled=True
            )

        message = str(raised.value)
        assert ENV_VAR in message
        assert cli_invoker_plugin.ENTRY_POINT_GROUP in message

    def test_delegated_with_plugin_starts(self, monkeypatch) -> None:
        _set_plugin(monkeypatch, _factory)

        require_invoker_for_delegated_content(
            "delegated", description_generation_enabled=True
        )

    def test_delegated_without_plugin_starts_when_generation_is_opted_out(
        self, monkeypatch
    ) -> None:
        def _must_not_run() -> Any:
            raise AssertionError("an opted-out deployment must not need the plugin")

        monkeypatch.setattr(cli_invoker_plugin, "get_invoker_factory", _must_not_run)

        require_invoker_for_delegated_content(
            "delegated", description_generation_enabled=False
        )

    def test_local_default_never_resolves_the_plugin(self, monkeypatch) -> None:
        def _must_not_run() -> Any:
            raise AssertionError("local content must not resolve the plugin")

        monkeypatch.setattr(cli_invoker_plugin, "get_invoker_factory", _must_not_run)

        require_invoker_for_delegated_content(
            "local", description_generation_enabled=True
        )


class TestGenerationFlag:
    def _config(self, tmp_path: Path, indexing: Optional[IndexingConfig]) -> Any:
        config = ServerConfig(server_dir=str(tmp_path))
        config.indexing_config = indexing
        return config

    def test_defaults_to_enabled(self, tmp_path: Path) -> None:
        assert golden_description_generation_enabled(
            self._config(tmp_path, IndexingConfig())
        )

    def test_missing_indexing_section_is_enabled(self, tmp_path: Path) -> None:
        assert golden_description_generation_enabled(self._config(tmp_path, None))

    def test_no_config_is_enabled(self) -> None:
        assert golden_description_generation_enabled(None)

    def test_opt_out_is_honoured(self, tmp_path: Path) -> None:
        indexing = IndexingConfig(golden_description_generation_enabled=False)

        assert not golden_description_generation_enabled(
            self._config(tmp_path, indexing)
        )


def _initialize_services_body() -> list[ast.stmt]:
    tree = ast.parse(Path(service_init.__file__).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "initialize_services":
            return node.body
    raise AssertionError("initialize_services not found in service_init.py")


def _is_call_to(node: ast.AST, name: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    )


class TestStartupWiring:
    """initialize_services() does real DB/filesystem bootstrap, so -- as the
    other service_init wiring guards do -- this parses the real source. It
    checks data flow, not text: the check is an unconditional statement of
    initialize_services (dead or guarded code fails), it runs before the
    golden repo manager is built, and both inputs come from ``server_config``.
    """

    def test_the_check_runs_unconditionally_before_the_golden_manager(
        self,
    ) -> None:
        body = _initialize_services_body()
        check_at = [
            i
            for i, stmt in enumerate(body)
            if isinstance(stmt, ast.Expr)
            and _is_call_to(stmt.value, "require_invoker_for_delegated_content")
        ]
        manager_at = [
            i
            for i, stmt in enumerate(body)
            if isinstance(stmt, ast.Assign)
            and _is_call_to(stmt.value, "GoldenRepoManager")
        ]

        assert len(check_at) == 1, "the startup check must be one top-level call"
        assert manager_at and check_at[0] < manager_at[0]

        stmt = body[check_at[0]]
        assert isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
        call = stmt.value
        (backend,) = call.args
        assert _is_call_to(backend, "content_backend_name")
        assert isinstance(backend, ast.Call)
        assert [ast.unparse(a) for a in backend.args] == ["server_config"]
        (flag,) = call.keywords
        assert flag.arg == "description_generation_enabled"
        assert _is_call_to(flag.value, "golden_description_generation_enabled")
        assert isinstance(flag.value, ast.Call)
        assert [ast.unparse(a) for a in flag.value.args] == ["server_config"]


@pytest.fixture
def golden(tmp_path: Path) -> Path:
    (tmp_path / "cidx-meta").mkdir()
    (tmp_path / "example-repo").mkdir()
    return tmp_path


def _add(golden: Path) -> None:
    meta_description_hook.on_repo_added(
        repo_name="example-repo",
        repo_url="https://example.com/example-repo.git",
        clone_path=str(golden / "example-repo"),
        golden_repos_dir=str(golden),
    )


class TestAddHook:
    def test_with_plugin_the_local_cli_is_never_consulted(
        self, golden: Path, monkeypatch
    ) -> None:
        _set_plugin(monkeypatch, _factory)

        with patch(f"{_HOOK}.get_claude_cli_manager") as get_cli:
            _add(golden)  # must not raise "Claude CLI not available"

        get_cli.assert_not_called()
        assert not (golden / "cidx-meta" / "example-repo.md").exists()

    def test_without_plugin_the_local_cli_path_is_unchanged(
        self, golden: Path, monkeypatch
    ) -> None:
        _set_plugin(monkeypatch, None)
        cli = MagicMock()
        cli.check_cli_available.return_value = False

        with patch(f"{_HOOK}.get_claude_cli_manager", return_value=cli):
            with pytest.raises(RuntimeError, match="Claude CLI not available"):
                _add(golden)
