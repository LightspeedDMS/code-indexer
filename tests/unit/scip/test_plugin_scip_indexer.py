"""PluginScipIndexer: a plugin's index BYTES become ``output_dir/index.scip``,
verified, with DelegatedIndexer's success/failure semantics -- and every way a
backend can misbehave is a loud FAILED project, never a local build.

Also pins ``code_indexer.scip.delegation_api``, the surface the out-of-tree
plugin (evo-sandbox ``cidx_content_backend.scip``) imports.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, List, Optional

import pytest

from code_indexer.scip import delegation_api as api
from code_indexer.scip.delegation_api import (
    ScipBuildOutcome,
    ScipBuildRequest,
    ScipExecutionBackend,
)
from code_indexer.scip.generator import (
    GenerationResult,
    ProjectGenerationResult,
    indexer_result_is_unsupported_language,
)
from code_indexer.scip.indexers.base import IndexerStatus
from code_indexer.scip.indexers.plugin_backend import PluginScipIndexer
from code_indexer.server.services.scip_resolution_queue import _detect_backpressure

INDEX = b"\x0a\x04scip-index-bytes\x00\xff"


def _ok(data: bytes = INDEX, **kw: Any) -> ScipBuildOutcome:
    fields = dict(
        status="ok",
        index_bytes=data,
        sha256=hashlib.sha256(data).hexdigest(),
        indexer_name="scip-typescript",
        indexer_version="0.3.14",
        duration_ms=1500,
    )
    fields.update(kw)
    return ScipBuildOutcome(**fields)  # type: ignore[arg-type]


class _Backend:
    def __init__(self, outcome: Any = None, *, raises: Optional[Exception] = None):
        self.outcome = outcome
        self.raises = raises
        self.requests: List[ScipBuildRequest] = []
        self.available: Any = True

    def build_index(self, request: ScipBuildRequest) -> Any:
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        return self.outcome

    def is_available(self) -> bool:
        if isinstance(self.available, Exception):
            raise self.available
        return bool(self.available)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "golden-repos" / "svc"
    root.mkdir(parents=True)
    return root


def _run(backend: _Backend, repo: Path, project: str = "web", **kw: Any):
    out = repo / ".code-indexer" / "scip" / project
    indexer = PluginScipIndexer(backend, repo)
    result = indexer.generate(
        repo / project,
        out,
        kw.pop("build_system", "npm"),
        golden_alias=kw.pop("golden_alias", "svc"),
        language=kw.pop("language", "typescript"),
        **kw,
    )
    return result, out


# ── public API ────────────────────────────────────────────────────────────────


def test_public_api_surface_is_stable() -> None:
    assert set(api.__all__) == {
        "DEFAULT_SCIP_BUILD_TIMEOUT_SECONDS",
        "SCIP_STATUSES",
        "STATUS_FAILED",
        "STATUS_OK",
        "STATUS_UNSUPPORTED",
        "SandboxMcpClient",
        "SandboxMcpError",
        "ScipBuildOutcome",
        "ScipBuildRequest",
        "ScipExecutionBackend",
        "sha256_hex",
    }
    assert api.SCIP_STATUSES == ("ok", "failed", "unsupported")
    assert api.sha256_hex(b"abc") == hashlib.sha256(b"abc").hexdigest()


def test_protocol_is_runtime_checkable() -> None:
    assert isinstance(_Backend(), ScipExecutionBackend)
    assert not isinstance(object(), ScipExecutionBackend)


# ── success ──────────────────────────────────────────────────────────────────


def test_ok_writes_verified_bytes_under_output_dir(repo: Path) -> None:
    backend = _Backend(_ok())
    result, out = _run(backend, repo)
    assert result.status is IndexerStatus.SUCCESS
    assert result.output_file == out / "index.scip"
    assert (out / "index.scip").read_bytes() == INDEX
    assert result.exit_code == 0
    assert result.degenerate is False
    assert result.duration_seconds == 1.5
    assert "scip-typescript@0.3.14" in result.stdout
    # nothing but the index is left behind
    assert sorted(p.name for p in out.iterdir()) == ["index.scip"]


def test_request_carries_cidx_discovery(repo: Path) -> None:
    backend = _Backend(_ok())
    _run(backend, repo, project="apps/web", allow_degenerate=True)
    (req,) = backend.requests
    assert req.repo_path == str(repo)
    assert req.golden_alias == "svc"
    assert req.project_path == "apps/web"
    assert req.build_system == "npm"
    assert req.language == "typescript"
    assert req.allow_degenerate is True
    assert req.timeout_seconds == api.DEFAULT_SCIP_BUILD_TIMEOUT_SECONDS


def test_root_project_is_dot(repo: Path) -> None:
    backend = _Backend(_ok())
    PluginScipIndexer(backend, repo).generate(
        repo,
        repo / ".code-indexer" / "scip",
        "npm",
        golden_alias="svc",
        language="typescript",
    )
    assert backend.requests[0].project_path == "."


def test_degenerate_is_carried(repo: Path) -> None:
    result, _ = _run(
        _Backend(_ok(degenerate=True, degraded_reason="no node_modules")), repo
    )
    assert result.is_success()
    assert result.degenerate is True


def test_uppercase_sha_accepted(repo: Path) -> None:
    result, _ = _run(
        _Backend(_ok(sha256=hashlib.sha256(INDEX).hexdigest().upper())), repo
    )
    assert result.is_success()


def test_ok_replaces_stale_index(repo: Path) -> None:
    out = repo / ".code-indexer" / "scip" / "web"
    out.mkdir(parents=True)
    (out / "index.scip").write_bytes(b"old")
    result, _ = _run(_Backend(_ok()), repo)
    assert result.is_success()
    assert (out / "index.scip").read_bytes() == INDEX


# ── unsupported / failed ───────────────────────────────────────────────────────


def test_unsupported_is_the_benign_exit_69_skip(repo: Path) -> None:
    result, out = _run(
        _Backend(
            ScipBuildOutcome(status="unsupported", error="no indexer for 'cobol'")
        ),
        repo,
    )
    assert result.status is IndexerStatus.FAILED
    assert result.exit_code == 69
    assert result.output_file is None
    assert "no indexer for 'cobol'" in result.stderr
    assert indexer_result_is_unsupported_language(result)
    assert not (out / "index.scip").exists()


def test_failed_is_a_real_failure(repo: Path) -> None:
    result, _ = _run(
        _Backend(
            ScipBuildOutcome(
                status="failed", error="scip-java exited 1", log="BUILD FAILED"
            )
        ),
        repo,
    )
    assert result.status is IndexerStatus.FAILED
    assert result.exit_code not in (0, 69)
    assert "scip-java exited 1" in result.stderr
    assert result.stdout == "BUILD FAILED"
    assert not indexer_result_is_unsupported_language(result)
    assert not _detect_backpressure(_generation(result))


def test_backpressure_failure_is_retried_by_the_build_queue(repo: Path) -> None:
    result, _ = _run(
        _Backend(
            ScipBuildOutcome(
                status="failed", error="SandboxCapacityExceeded", backpressure=True
            )
        ),
        repo,
    )
    assert result.status is IndexerStatus.FAILED
    assert _detect_backpressure(_generation(result))


def test_failure_removes_stale_index(repo: Path) -> None:
    out = repo / ".code-indexer" / "scip" / "web"
    out.mkdir(parents=True)
    (out / "index.scip").write_bytes(b"previous build")
    result, _ = _run(_Backend(ScipBuildOutcome(status="failed", error="x")), repo)
    assert result.is_failure()
    assert not (out / "index.scip").exists()


# ── contract violations: all loud FAILED ──────────────────────────────────────


@pytest.mark.parametrize(
    "outcome, fragment",
    [
        (None, "not a ScipBuildOutcome"),
        ({"status": "ok"}, "not a ScipBuildOutcome"),
        (ScipBuildOutcome(status="weird"), "unknown status"),
        (ScipBuildOutcome(status="ok", sha256="00"), "without index bytes"),
        (
            ScipBuildOutcome(status="ok", index_bytes=b"", sha256="00"),
            "without index bytes",
        ),
        (_ok(sha256="0" * 64), "sha256 mismatch"),
        (_ok(sha256=None), "sha256 mismatch"),
    ],
)
def test_contract_violation_fails_loud(repo: Path, outcome: Any, fragment: str) -> None:
    result, out = _run(_Backend(outcome), repo)
    assert result.status is IndexerStatus.FAILED
    assert result.output_file is None
    assert fragment in result.stderr
    assert not (out / "index.scip").exists()


def test_backend_exception_fails_loud(repo: Path) -> None:
    result, _ = _run(_Backend(raises=ConnectionError("sandbox-mcp down")), repo)
    assert result.status is IndexerStatus.FAILED
    assert "ConnectionError: sandbox-mcp down" in result.stderr


def test_workspace_id_is_refused_without_calling_the_backend(repo: Path) -> None:
    backend = _Backend(_ok())
    result = PluginScipIndexer(backend, repo).generate(
        repo / "web", repo / "out", "npm", workspace_id="W-1", language="typescript"
    )
    assert result.is_failure()
    assert "workspace_id" in result.stderr
    assert backend.requests == []


def test_missing_language_is_refused(repo: Path) -> None:
    backend = _Backend(_ok())
    result, _ = _run(backend, repo, language=None)
    assert result.is_failure()
    assert backend.requests == []


def test_project_outside_repo_is_refused(repo: Path) -> None:
    backend = _Backend(_ok())
    result = PluginScipIndexer(backend, repo).generate(
        repo.parent / "other",
        repo / "out",
        "npm",
        golden_alias="svc",
        language="typescript",
    )
    assert result.is_failure()
    assert "not under repo root" in result.stderr
    assert backend.requests == []


def test_unremovable_stale_index_fails_before_dispatch(repo: Path) -> None:
    blocker = repo / "blocker"
    blocker.write_text("a file, not a dir")
    backend = _Backend(_ok())
    result = PluginScipIndexer(backend, repo).generate(
        repo / "web", blocker / "web", "npm", golden_alias="svc", language="typescript"
    )
    assert result.is_failure()
    assert "could not remove stale" in result.stderr
    assert backend.requests == []


def test_failed_write_fails_loud_and_leaves_nothing(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import code_indexer.scip.indexers.plugin_backend as mod

    def _boom(src: Any, dst: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(mod.os, "replace", _boom)
    result, out = _run(_Backend(_ok()), repo)
    assert result.is_failure()
    assert "could not write the delegated index" in result.stderr
    assert list(out.iterdir()) == []


# ── availability ─────────────────────────────────────────────────────────────


def test_is_available_delegates_and_never_raises(repo: Path) -> None:
    backend = _Backend()
    indexer = PluginScipIndexer(backend, repo)
    assert indexer.is_available() is True
    backend.available = False
    assert indexer.is_available() is False
    backend.available = RuntimeError("boom")
    assert indexer.is_available() is False
    assert indexer.get_version() is None


def _generation(result: Any) -> GenerationResult:
    return GenerationResult(
        total_projects=1,
        successful_projects=0,
        failed_projects=1,
        project_results=[ProjectGenerationResult(project=None, indexer_result=result)],  # type: ignore[arg-type]
        duration_seconds=0.0,
    )
