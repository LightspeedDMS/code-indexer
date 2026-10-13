"""scip_backend='delegated-plugin': the SCIP compile step runs in a
``code_indexer.scip_backend`` plugin, or startup/wiring fails LOUD.

Every way the plugin can be missing or broken must raise -- never a local build
(the pod has no toolchain and no repository) and never the ``delegated``
(sandbox_scip) path. And installing the plugin must not change ``delegated``:
deployments opt in by value, so a cidx image that ships the plugin is inert
until the config says ``delegated-plugin``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.scip.delegation_api import ScipBuildOutcome, ScipBuildRequest
from code_indexer.scip.indexers.delegated import DelegatedIndexer
from code_indexer.scip.indexers.factory import (
    DELEGATED_SCIP_BACKENDS,
    ScipBackendFactory,
    is_delegated_scip_backend,
)
from code_indexer.scip.indexers.plugin_backend import PluginScipIndexer
from code_indexer.scip.protobuf import scip_pb2
from code_indexer.server.services import backend_plugin_loader, scip_backend_plugin
from code_indexer.server.services import scip_lifecycle
from code_indexer.server.services.scip_lifecycle import (
    ScipDelegationOutcome,
    funnel_scip_generation,
)
from code_indexer.server.startup.scip_backend_wiring import (
    build_scip_generator,
    build_scip_generator_for_golden_repo,
    validate_scip_backend_config,
)

PLUGIN = SimpleNamespace(scip_backend="delegated-plugin")


@pytest.fixture(autouse=True)
def _fresh_plugin(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv(scip_backend_plugin.ENV_VAR, raising=False)
    scip_backend_plugin.reset_cache()
    yield
    scip_backend_plugin.reset_cache()


def _index_bytes() -> bytes:
    index = scip_pb2.Index()
    index.metadata.project_root = "file:///workspace"
    index.metadata.tool_info.name = "scip-typescript"
    return index.SerializeToString()


class _FakeBackend:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.requests: List[ScipBuildRequest] = []

    def build_index(self, request: ScipBuildRequest) -> ScipBuildOutcome:
        self.requests.append(request)
        data = _index_bytes()
        return ScipBuildOutcome(
            status="ok", index_bytes=data, sha256=hashlib.sha256(data).hexdigest()
        )

    def is_available(self) -> bool:
        return self.available


class _FakeEntryPoint:
    def __init__(self, factory: Any, *, fails: bool = False) -> None:
        self.name = "sandbox"
        self._factory = factory
        self._fails = fails

    def load(self) -> Any:
        if self._fails:
            raise ImportError("cidx_content_backend.scip: no module named requests")
        return self._factory


def _install_entry_points(
    monkeypatch: pytest.MonkeyPatch, entries: List[_FakeEntryPoint]
) -> List[str]:
    asked: List[str] = []
    registry: Dict[str, List[_FakeEntryPoint]] = {"code_indexer.scip_backend": entries}

    class _Groups(Dict[str, List[_FakeEntryPoint]]):
        def get(self, group: str, default: Any = None) -> Any:
            asked.append(group)
            return super().get(group, default)

    def _entry_points(group: Any = None) -> Any:
        if group is None:
            return _Groups(registry)
        asked.append(group)
        return registry.get(group, [])

    monkeypatch.setattr(backend_plugin_loader, "entry_points", _entry_points)
    return asked


def test_the_seam_names() -> None:
    assert scip_backend_plugin.ENV_VAR == "CIDX_SCIP_BACKEND"
    assert scip_backend_plugin.ENTRY_POINT_GROUP == "code_indexer.scip_backend"
    assert DELEGATED_SCIP_BACKENDS == ("delegated", "delegated-plugin")
    assert ScipBackendFactory._VALID_BACKENDS == (
        "local",
        "delegated",
        "delegated-plugin",
    )
    assert is_delegated_scip_backend("delegated-plugin")
    assert is_delegated_scip_backend("delegated")
    assert not is_delegated_scip_backend("local")
    assert not is_delegated_scip_backend(None)


# ── fail loud ────────────────────────────────────────────────────────────────


def test_no_plugin_installed_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_entry_points(monkeypatch, [])
    with pytest.raises(RuntimeError, match="no SCIP backend plugin is installed"):
        validate_scip_backend_config(PLUGIN)


def test_plugin_that_fails_to_load_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_entry_points(monkeypatch, [_FakeEntryPoint(_FakeBackend, fails=True)])
    with pytest.raises(RuntimeError, match="could not be loaded or constructed"):
        validate_scip_backend_config(PLUGIN)


def test_factory_that_raises_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    def make() -> Any:
        raise ValueError("CIDX_SCIP_SANDBOX_MCP_URL is not set")

    _install_entry_points(monkeypatch, [_FakeEntryPoint(make)])
    with pytest.raises(RuntimeError, match="CIDX_SCIP_SANDBOX_MCP_URL is not set"):
        validate_scip_backend_config(PLUGIN)


def test_non_backend_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_entry_points(monkeypatch, [_FakeEntryPoint(object)])
    with pytest.raises(RuntimeError, match="has no build_index/is_available"):
        validate_scip_backend_config(PLUGIN)


def test_unavailable_backend_fails_wiring(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_entry_points(
        monkeypatch, [_FakeEntryPoint(lambda: _FakeBackend(available=False))]
    )
    with pytest.raises(RuntimeError, match="reports itself unavailable"):
        build_scip_generator(PLUGIN, tmp_path, golden_alias="svc")


def test_bad_env_override_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(scip_backend_plugin.ENV_VAR, "no_such_module:make")
    with pytest.raises(RuntimeError, match="cannot import module"):
        validate_scip_backend_config(PLUGIN)


# ── selection ────────────────────────────────────────────────────────────────


def test_env_override_wins_over_the_entry_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "fake_scip_plugin.py").write_text(
        "class B:\n"
        "    def build_index(self, request): ...\n"
        "    def is_available(self): return True\n"
        "def make():\n"
        "    return B()\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(scip_backend_plugin.ENV_VAR, "fake_scip_plugin:make")
    asked = _install_entry_points(monkeypatch, [_FakeEntryPoint(object)])
    validate_scip_backend_config(PLUGIN)
    assert asked == []


def test_delegated_plugin_wires_plugin_indexer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend = _FakeBackend()
    _install_entry_points(monkeypatch, [_FakeEntryPoint(lambda: backend)])
    gen = build_scip_generator(
        PLUGIN, tmp_path, golden_alias="svc", allow_degenerate=True
    )
    indexers = set(id(i) for i in gen._indexers.values())
    assert len(indexers) == 1
    assert all(isinstance(i, PluginScipIndexer) for i in gen._indexers.values())
    assert gen.golden_alias == "svc"
    assert gen.allow_degenerate is True


def test_golden_repo_is_alias_addressed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_entry_points(monkeypatch, [_FakeEntryPoint(_FakeBackend)])
    gen = build_scip_generator_for_golden_repo(
        PLUGIN, tmp_path, golden_repo_manager=None, alias="svc"
    )
    assert gen.golden_alias == "svc"
    assert gen.allow_degenerate is True


@patch("requests.get")
def test_installed_plugin_does_not_change_delegated(
    mock_get: MagicMock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cidx image bump that ships the plugin must be inert for `delegated`."""
    mock_get.return_value = MagicMock(status_code=200)
    asked = _install_entry_points(monkeypatch, [_FakeEntryPoint(_FakeBackend)])
    cfg = SimpleNamespace(
        scip_backend="delegated",
        scip_sandbox=SimpleNamespace(endpoint="http://sb", api_key="k"),
    )
    gen = build_scip_generator(cfg, tmp_path, golden_alias="svc")
    assert all(isinstance(i, DelegatedIndexer) for i in gen._indexers.values())
    assert asked == []


def test_local_never_loads_the_plugin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(scip_backend_plugin.ENV_VAR, "no_such_module:make")
    validate_scip_backend_config(SimpleNamespace(scip_backend="local"))
    build_scip_generator(SimpleNamespace(scip_backend="local"), tmp_path)


def test_factory_requires_backend_and_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="execution_backend is required"):
        ScipBackendFactory.create_indexer_map("delegated-plugin", repo_root=tmp_path)
    with pytest.raises(ValueError, match="repo_root is required"):
        ScipBackendFactory.create_indexer_map(
            "delegated-plugin", execution_backend=_FakeBackend()
        )


# ── funnel + end to end ────────────────────────────────────────────────────────


class _CaptureQueue:
    def __init__(self) -> None:
        self.builds: List[Any] = []

    def enqueue(self, build: Any) -> bool:
        self.builds.append(build)
        return True


def test_funnel_enqueues_for_delegated_plugin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(scip_lifecycle, "get_current_commit", lambda _p: "sha1")
    q = _CaptureQueue()
    out = funnel_scip_generation(
        alias="svc",
        repo_root=tmp_path,
        config=PLUGIN,
        golden_repo_manager=object(),
        build_queue=q,  # type: ignore[arg-type]
        generator_builder=lambda *a, **k: SimpleNamespace(generate=lambda: None),
    )
    assert out is ScipDelegationOutcome.ENQUEUED
    assert q.builds[0].dedupe_key == "svc@sha1"


def test_activated_repo_stays_local_under_delegated_plugin(tmp_path: Path) -> None:
    out = funnel_scip_generation(
        alias="u-repo", repo_root=tmp_path, config=PLUGIN, activated_repo=True
    )
    assert out is ScipDelegationOutcome.NOT_DELEGATED


def test_generator_builds_the_scip_db_from_plugin_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end: discovery -> plugin -> bytes under the index root -> DB."""
    repo = tmp_path / "golden-repos" / "svc"
    (repo / "web").mkdir(parents=True)
    (repo / "web" / "package.json").write_text(json.dumps({"name": "web"}))
    (repo / "web" / "tsconfig.json").write_text("{}")
    backend = _FakeBackend()
    _install_entry_points(monkeypatch, [_FakeEntryPoint(lambda: backend)])

    gen = build_scip_generator_for_golden_repo(
        PLUGIN, repo, golden_repo_manager=None, alias="svc"
    )
    result = gen.generate()

    assert result.successful_projects == 1, [
        r.indexer_result.stderr for r in result.project_results
    ]
    (req,) = backend.requests
    assert req.project_path == "web"
    assert req.golden_alias == "svc"
    assert req.repo_path == str(repo)
    scip_dir = repo / ".code-indexer" / "scip" / "web"
    assert (scip_dir / "index.scip").read_bytes() == _index_bytes()
    assert any(p.name.endswith(".scip.db") for p in scip_dir.iterdir())
