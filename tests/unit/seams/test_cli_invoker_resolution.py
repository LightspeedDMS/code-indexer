"""The CLI invoker as the core resolves it: the orchestrator delegate.

The delegate (``plugins/cidx_orchestrator_invoker``) is registered on
``code_indexer.cli_invoker`` under its published entry name ``orchestrator``,
which the shared seam resolver accepts as the ``delegated`` mode. The factory
the core resolves builds an invoker that passes the CLI invoker conformance
suite against the testkit's fake orchestrator, and finds the core's repository
source in the SDK slot instead of importing the core.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Iterator, List

import pytest

from cidx_sdk.entry_points import CLI_INVOKER_ENTRY_POINT_GROUP
from cidx_sdk.seams.cli_invoker import REPOSITORY_SOURCE, GoldenRepositorySource
from cidx_sdk.seams.resolver import add_entry_points, remove_entry_points
from cidx_testkit.conformance.cli_invoker import (
    CliInvokerContract,
    CliInvokerSubject,
)
from cidx_testkit.fakes.orchestrator import (
    FAKE_PASSWORD,
    FakeOrchestrator,
    orchestrator_subject,
)

from code_indexer.server.services import cli_invoker_plugin
from code_indexer.server.startup import content_backend_wiring

_PLUGIN_NAME = "cidx_orchestrator_invoker_resolved"
_PLUGIN = (
    Path(__file__).resolve().parents[3]
    / "plugins"
    / "cidx_orchestrator_invoker"
    / "cidx_orchestrator_invoker"
    / "__init__.py"
)
#: The entry name the delegate is published under (pyproject.toml).
_PUBLISHED_NAME = "orchestrator"


def _load_plugin() -> ModuleType:
    spec = importlib.util.spec_from_file_location(_PLUGIN_NAME, _PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PLUGIN_NAME] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()


@dataclass(frozen=True)
class _Entry:
    name: str
    factory: Any

    def load(self) -> Any:
        return self.factory


@pytest.fixture
def resolved_factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setattr(cli_invoker_plugin, "entry_points", lambda **_: [])
    monkeypatch.delenv(cli_invoker_plugin.ENV_VAR, raising=False)
    add_entry_points(
        CLI_INVOKER_ENTRY_POINT_GROUP, [_Entry(_PUBLISHED_NAME, plugin.make_invoker)]
    )
    cli_invoker_plugin.reset_cache()
    try:
        factory = cli_invoker_plugin.get_invoker_factory()
        assert factory is plugin.make_invoker
        yield factory
    finally:
        remove_entry_points(CLI_INVOKER_ENTRY_POINT_GROUP, [_PUBLISHED_NAME])
        cli_invoker_plugin.reset_cache()


@pytest.fixture
def fake_orchestrator(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeOrchestrator]:
    with FakeOrchestrator() as service:
        monkeypatch.setenv("CIDX_ORCH_URL", service.url)
        monkeypatch.setenv("CIDX_ORCH_PASSWORD", FAKE_PASSWORD)
        monkeypatch.setenv("CIDX_ORCH_POLL_SECS", "0")
        monkeypatch.delenv("CIDX_ORCH_MODEL", raising=False)
        yield service


class TestResolvedOrchestratorInvoker(CliInvokerContract):
    @pytest.fixture
    def cli_subject(
        self,
        resolved_factory: Any,
        fake_orchestrator: FakeOrchestrator,
        tmp_path: Path,
    ) -> CliInvokerSubject:
        invoker = resolved_factory("opus", None)
        return orchestrator_subject(invoker, fake_orchestrator, str(tmp_path))


def test_the_published_delegate_resolves_golden_repositories(
    resolved_factory: Any,
) -> None:
    assert cli_invoker_plugin.invoker_resolves_golden_repositories() is True


def test_no_delegate_means_the_builtin_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_invoker_plugin, "entry_points", lambda **_: [])
    monkeypatch.delenv(cli_invoker_plugin.ENV_VAR, raising=False)
    cli_invoker_plugin.reset_cache()
    try:
        assert cli_invoker_plugin.get_invoker_factory() is None
    finally:
        cli_invoker_plugin.reset_cache()


def test_the_core_installs_its_repository_source_for_delegates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = REPOSITORY_SOURCE.get()
    assert isinstance(source, GoldenRepositorySource)
    seen: List[Any] = []

    def golden_repo_source(cwd: Any) -> Dict[str, str]:
        seen.append(cwd)
        return {"alias": "example"}

    monkeypatch.setattr(
        content_backend_wiring, "is_content_seam_delegated", lambda: True
    )
    monkeypatch.setattr(
        content_backend_wiring, "golden_repo_source", golden_repo_source
    )
    assert source.holds_repositories() is False
    assert source.golden_repository(tmp_path) == {"alias": "example"}
    assert seen == [tmp_path]
