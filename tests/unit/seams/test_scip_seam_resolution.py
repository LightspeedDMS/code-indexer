"""The SCIP seam as the core resolves it: the delegate through entry points.

``scip_backend='delegated-plugin'`` reaches its ``ScipBackend`` only through
the seam resolver (``server/services/scip_backend_plugin``). Here the delegate
is the testkit's fake sandbox-mcp -- two replicas sharing one result store --
found exactly as an installed delegate distribution is; it passes the SCIP
conformance suite as the core resolves it, and the core's plugin indexer
writes what it returns.

``local`` has no provider on the group: the local SCIP build is the
per-language subprocess indexers inside the SCIP feature's generator, not a
``ScipBackend`` object, so there is nothing to register under ``local``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Iterator, Tuple

import pytest

from cidx_sdk.seams.resolver import SeamResolver
from cidx_sdk.seams.scip import ScipBackend
from cidx_testkit.conformance.scip import (
    ScipBackendContract,
    ScipSubject,
    directory_writer,
)
from cidx_testkit.fakes.sandbox_mcp import (
    FakeSandboxMcp,
    FakeSandboxScipBackend,
    SandboxResultStore,
    SandboxState,
    register_as_delegate,
    unregister_delegate,
)

from cidx_scip.indexers.base import (
    DELEGATED_BACKPRESSURE_MARKER,
    UNSUPPORTED_LANGUAGE_EXIT_CODE,
)
from cidx_scip.indexers.plugin_backend import PluginScipIndexer
from cidx_scip.server import backend_plugin as scip_backend_plugin
from cidx_scip.server.backend_wiring import (
    build_scip_generator,
    validate_scip_backend_config,
)

_ALIAS = "example-repo"
_UNSUPPORTED = "example-unsupported"
PLUGIN = SimpleNamespace(scip_backend="delegated-plugin")


@pytest.fixture(autouse=True)
def _fresh_resolver(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No ambient install: only what a test registers."""
    monkeypatch.setattr(scip_backend_plugin, "entry_points", lambda **_: [])
    monkeypatch.delenv(scip_backend_plugin.ENV_VAR, raising=False)
    scip_backend_plugin.reset_cache()
    yield
    scip_backend_plugin.reset_cache()


@pytest.fixture
def sandbox(tmp_path: Path) -> Iterator[Tuple[SandboxState, Path]]:
    state = SandboxState(
        results=SandboxResultStore(chunk_bytes=256), inline_output_bytes=512
    )
    state.unsupported_languages.add(_UNSUPPORTED)
    repo = tmp_path / "golden-repos" / _ALIAS
    state.serve(str(repo), repo)
    state.serve(_ALIAS, repo)
    registered = register_as_delegate(
        FakeSandboxMcp(state, name="a"), FakeSandboxMcp(state, name="b")
    )
    try:
        yield state, repo
    finally:
        unregister_delegate(registered)


def _resolved() -> ScipBackend:
    """The delegate exactly as the core resolves it."""
    backend = scip_backend_plugin.resolve_scip_backend()
    assert isinstance(backend, ScipBackend)
    return backend


class TestResolvedScipBackend(ScipBackendContract):
    @pytest.fixture
    def scip_subject(self, sandbox: Tuple[SandboxState, Path]) -> ScipSubject:
        _state, repo = sandbox
        backend = scip_backend_plugin.resolve_scip_backend()
        assert isinstance(backend, FakeSandboxScipBackend)
        return ScipSubject(
            backend=backend,
            repo_path=str(repo),
            golden_alias=_ALIAS,
            write=directory_writer(repo),
            unsupported_language=_UNSUPPORTED,
        )


def test_the_delegated_plugin_mode_validates_and_builds_a_generator(
    sandbox: Tuple[SandboxState, Path],
) -> None:
    _state, repo = sandbox
    validate_scip_backend_config(PLUGIN)
    generator = build_scip_generator(PLUGIN, repo)
    assert generator is not None


def test_the_plugin_indexer_writes_what_the_delegate_returned(
    sandbox: Tuple[SandboxState, Path], tmp_path: Path
) -> None:
    _state, repo = sandbox
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("x = 1\n")
    backend = _resolved()
    indexer = PluginScipIndexer(backend, repo)
    out = tmp_path / "out"
    result = indexer.generate(repo, out, "pip", golden_alias=_ALIAS, language="python")
    assert result.is_success(), result.stderr
    assert result.output_file is not None
    expected = backend.build_index(
        ScipBackendContract.request(
            ScipSubject(backend, str(repo), _ALIAS, directory_writer(repo))
        )
    )
    assert result.output_file.read_bytes() == expected.index_bytes


def test_an_unsupported_language_reaches_the_core_as_unsupported(
    sandbox: Tuple[SandboxState, Path], tmp_path: Path
) -> None:
    _state, repo = sandbox
    backend = _resolved()
    result = PluginScipIndexer(backend, repo).generate(
        repo, tmp_path / "out", "x", golden_alias=_ALIAS, language=_UNSUPPORTED
    )
    assert result.exit_code == UNSUPPORTED_LANGUAGE_EXIT_CODE


def test_no_capacity_reaches_the_core_as_backpressure(
    sandbox: Tuple[SandboxState, Path], tmp_path: Path
) -> None:
    state, repo = sandbox
    state.busy = True
    backend = _resolved()
    result = PluginScipIndexer(backend, repo).generate(
        repo, tmp_path / "out", "pip", golden_alias=_ALIAS, language="python"
    )
    assert not result.is_success()
    assert DELEGATED_BACKPRESSURE_MARKER in result.stderr


def test_local_has_no_provider_on_the_group() -> None:
    """Nothing in this tree registers ``local`` on the SCIP group."""
    resolver = SeamResolver(seam="scip", entry_point_source=lambda _group: [])
    assert resolver.providers("local") == []
