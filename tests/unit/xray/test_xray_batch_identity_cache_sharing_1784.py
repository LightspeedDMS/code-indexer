"""Bug #1784 review blocker: identity-cache lifetime must span a WHOLE
xray_search_batch operation, not one cell.

`_run_xray_batch_job` (src/code_indexer/server/mcp/handlers/xray_batch.py)
constructs a fresh `XRaySearchEngine()` per cell (one cell = one repo x one
scan), and `XRaySearchEngine.__init__` constructs a fresh `RustNativeBackend`
per engine. The per-instance `_identity_cache` introduced by the earlier
Bug #1784 MAJOR-2 fix is therefore discarded for every cell, even though the
evaluator source is constant across all repos for a given scan. At the
documented batch ceiling (50 repos x 50 scans = 2,500 cells) this can still
spawn up to 2,500 `xray-cli --print-cache-identity` subprocesses.

This test drives the REAL code path end-to-end -- the real xray-cli binary
answers both `--print-cache-identity` and the actual `--dynlib` compile, no
subprocess result is faked. Only the cluster-cache backend is replaced with
an in-memory test double (a Protocol-typed stand-in for
XrayCachePostgresBackend -- no PostgreSQL in unit tests). The identity
subprocess call site (RustNativeBackend._run_cache_identity_subprocess, a
private instance method) is wrapped with a *counting spy that delegates
straight through to the real implementation* -- the same technique
test_rust_backend.py already uses to observe subprocess call counts (see
e.g. its test_get_cache_identity_info_instance_cache_avoids_second_
subprocess_call, which patches `subprocess.run` itself). Patching at the
method boundary here (rather than global `subprocess.run`) is necessary
because Phase 1's ripgrep driver also shells out via `subprocess.run` in the
same process and must run unmodified. Nothing about the identity/compile
control flow is altered by the spy; it only counts.

`Dict[str, Any]` is used for scan/repo payloads throughout this file because
it is byte-identical to `_run_xray_batch_job`'s own declared parameter types
(`resolved_repos: List[Dict[str, Any]]`, `scans: List[Dict[str, Any]]`) --
matching the production signature under test, not a type-safety shortcut.

Also confirms the required solo-mode invariant: with no cluster cache backend
configured, the identity subprocess must never be invoked at all.
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest
import yaml

if TYPE_CHECKING:
    from code_indexer.xray.rust_backend import RustNativeBackend, XrayCacheBackend


_VALID_EVALUATOR = (
    "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n    Vec::new()\n}\n"
)


def _require_xray_cli_binary() -> None:
    """Skip when the real xray-cli release binary is not built locally.

    Mirrors test_rust_backend.py's _require_xray_cli_binary(): this is a
    genuine, near-zero-mocking regression test that needs the real compiled
    binary to prove the fix at the actual OS process-exec layer.
    """
    from code_indexer.xray.rust_backend import _XRAY_CLI_DEFAULT

    if not _XRAY_CLI_DEFAULT.exists():
        pytest.skip(
            f"xray-cli binary not built at {_XRAY_CLI_DEFAULT}; "
            "run 'cargo build --release' inside rust/ to enable this test."
        )


class _FakeClusterCache:
    """Minimal XrayCacheBackend test double -- always a cluster miss.

    Stands in for XrayCachePostgresBackend (no PostgreSQL in unit tests).
    Its ONLY job is to make `self._xray_cache is not None` true so
    RustNativeBackend's pre-fill/post-fill paths (and therefore the identity
    subprocess) actually fire -- it never touches identity/compile logic
    itself, so it cannot mask a regression in the real formula.
    """

    def __init__(self) -> None:
        self.fetch_calls = 0
        self.store_calls = 0

    def fetch(self, source_hash: str, rustc_version: str) -> Optional[bytes]:
        self.fetch_calls += 1
        return None

    def store(
        self,
        source_hash: str,
        rustc_version: str,
        so_bytes: bytes,
        compile_ms: int = 0,
    ) -> None:
        self.store_calls += 1


def _make_repo_with_matching_file(base: Path, name: str) -> Path:
    """Tiny repo dir with one Python file whose content matches driver_regex='foo'."""
    repo = base / name
    repo.mkdir()
    (repo / "sample.py").write_text("def foo():\n    return 1\n")
    return repo


def _build_single_scan() -> List[Dict[str, Any]]:
    """One scan bundle, reused across every repo in a batch call below --
    the evaluator source is byte-identical for every resulting cell."""
    return [
        {
            "driver_regex": "foo",
            "evaluator_code": _VALID_EVALUATOR,
            "pattern_name": None,
            "pattern_params": None,
            "search_target": "content",
            "case_sensitive": True,
            "multiline": False,
            "pcre2": False,
        }
    ]


def _build_resolved_repos(tmp_path: Path, aliases: List[str]) -> List[Dict[str, Any]]:
    return [
        {"alias": alias, "path": _make_repo_with_matching_file(tmp_path, alias)}
        for alias in aliases
    ]


@contextlib.contextmanager
def _spy_on_identity_subprocess():
    """Patch RustNativeBackend._run_cache_identity_subprocess with a counting
    spy that delegates straight through to the real implementation (no faked
    output) -- see module docstring for why this boundary, not global
    subprocess.run, is patched. Yields the list of rust_code arguments passed
    to each real call.
    """
    from code_indexer.xray.rust_backend import RustNativeBackend

    original = RustNativeBackend._run_cache_identity_subprocess
    calls: List[str] = []

    def _counting(
        self: "RustNativeBackend",
        rust_code: str,
        timeout_seconds: float = 10,
        graph_mode: bool = False,
    ) -> "subprocess.CompletedProcess[str]":
        calls.append(rust_code)
        return original(self, rust_code, timeout_seconds, graph_mode=graph_mode)

    with patch.object(RustNativeBackend, "_run_cache_identity_subprocess", _counting):
        yield calls


def _run_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    aliases: List[str],
    job_id: str,
    cluster_cache: Optional["XrayCacheBackend"],
) -> Tuple[Dict[str, Any], List[str]]:
    """Shared test rig: run a batch (len(aliases) repos x the one shared
    scan) with the given cluster cache backend (or None for solo mode),
    spying on real identity-subprocess invocations.

    Returns (job_result, identity_subprocess_calls).
    """
    from code_indexer.server.mcp.handlers.xray_batch import _run_xray_batch_job

    # Sandbox the cache dir -- read by Python's own pre/post-fill logic AND
    # by the real xray-cli child subprocess (Popen with no env= override
    # inherits this process's environ).
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path / "cidx-data"))

    bjm = MagicMock()
    bjm.jobs = {job_id: MagicMock(cancelled=False)}

    with (
        patch(
            "code_indexer.xray.search_engine._get_cluster_cache",
            return_value=cluster_cache,
        ),
        _spy_on_identity_subprocess() as identity_calls,
    ):
        result = _run_xray_batch_job(
            resolved_repos=_build_resolved_repos(tmp_path, aliases),
            scans=_build_single_scan(),
            repo_errors=[],
            cidx_meta_path=tmp_path / "cidx-meta",
            max_results=None,
            timeout_seconds=120,
            job_id=job_id,
            bjm=bjm,
            progress_callback=lambda *a, **k: None,
        )

    return result, identity_calls


def test_identity_subprocess_invoked_once_per_unique_evaluator_across_batch_cells(
    tmp_path, monkeypatch
):
    """3 repos x 1 scan (same evaluator source) = 3 cells.

    RED (current code): each cell builds its own XRaySearchEngine with an
    empty RustNativeBackend._identity_cache -- expect 3 identity subprocess
    calls (once per cell) instead of 1 (once per unique evaluator source).

    GREEN (fixed code): the identity cache is shared across the whole batch
    operation, so exactly 1 subprocess call happens regardless of cell count.
    """
    _require_xray_cli_binary()
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")

    result, identity_calls = _run_batch(
        tmp_path,
        monkeypatch,
        aliases=["repo-0", "repo-1", "repo-2"],
        job_id="batch-job-1784",
        cluster_cache=_FakeClusterCache(),
    )

    assert result["repos_completed"] == 3, (
        f"all 3 cells must complete for this assertion to be meaningful: {result}"
    )
    assert len(identity_calls) == 1, (
        "Expected exactly ONE 'xray-cli --print-cache-identity' subprocess "
        f"invocation across 3 cells sharing the same evaluator source; got "
        f"{len(identity_calls)}. The identity cache must be shared across "
        "the WHOLE batch operation, not reset per cell (Bug #1784 review)."
    )


def test_solo_mode_never_invokes_identity_subprocess(tmp_path, monkeypatch):
    """Solo/CLI mode (no cluster cache backend) must invoke the identity
    subprocess ZERO times, regardless of the batch-level sharing fix."""
    _require_xray_cli_binary()
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")

    result, identity_calls = _run_batch(
        tmp_path,
        monkeypatch,
        aliases=["repo-a", "repo-b"],
        job_id="batch-job-solo",
        cluster_cache=None,
    )

    assert result["repos_completed"] == 2
    assert identity_calls == [], (
        "Solo mode (no cluster cache backend) must never invoke the "
        f"identity subprocess; got {len(identity_calls)} call(s)."
    )


# ---------------------------------------------------------------------------
# Sizing-dimension-mismatch blocker (final Bug #1784 review round):
# SharedIdentityCache is keyed on resolved evaluator SOURCE TEXT, but
# resolve_batch_evaluator() resolves a repo-scoped pattern override
# ({repo-alias}/{name}.yaml) BEFORE the cross-repo __any__/{name}.yaml
# fallback -- so one scan can yield a DIFFERENT source per repo. Sizing the
# cache to max(_IDENTITY_CACHE_MAX_ENTRIES, len(scans)) ignores this and can
# be exceeded by up to len(resolved_repos) x len(scans) distinct sources,
# reintroducing eviction-driven recomputation.
# ---------------------------------------------------------------------------

_SHARED_PATTERN_CODE = (
    "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n"
    '    vec![EvalFinding { pattern: "shared".to_string(), line: node.start_line,'
    " snippet: String::new() }]\n"
    "}\n"
)


def _override_pattern_code(i: int) -> str:
    """A Rust evaluator source that is byte-distinct per `i`."""
    return (
        "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n"
        f'    vec![EvalFinding {{ pattern: "override-{i}".to_string(), '
        "line: node.start_line, snippet: String::new() }]\n"
        "}\n"
    )


def _write_pattern(
    cidx_meta_path: Path, scope: str, pattern_name: str, evaluator_code: str
) -> None:
    """Write a minimal pattern YAML directly to cidx-meta/xray-patterns/{scope}/.

    Bypasses XrayPatternService.store_xray_pattern() (git-commit + coarse
    lock + full-field validation) -- resolve_and_prepare_pattern()/
    _load_pattern() only ever read `evaluator_code` (and optional
    `parameters`) off the parsed YAML, so a minimal file is sufficient and
    keeps this test focused on the resolution/sizing behaviour under test.
    """
    target_dir = cidx_meta_path / "xray-patterns" / scope
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / f"{pattern_name}.yaml").write_text(
        yaml.safe_dump({"name": pattern_name, "evaluator_code": evaluator_code}),
        encoding="utf-8",
    )


def _build_pattern_scan(pattern_name: str = "p") -> List[Dict[str, Any]]:
    """One scan bundle that resolves via pattern_name (not inline evaluator_code)."""
    return [
        {
            "driver_regex": "foo",
            "evaluator_code": None,
            "pattern_name": pattern_name,
            "pattern_params": None,
            "search_target": "content",
            "case_sensitive": True,
            "multiline": False,
            "pcre2": False,
        }
    ]


def _setup_repo_scoped_pattern_batch(
    tmp_path: Path, n_overrides: int
) -> Tuple[List[str], Path]:
    """Build aliases + pattern files for the sizing-mismatch scenario.

    Repo order: shared-start, override-0..override-{n_overrides-1},
    shared-end. Only the override-* aliases get their own repo-scoped
    pattern file; shared-start/shared-end fall back to __any__/p.yaml,
    resolving to the identical shared source.
    """
    aliases = (
        ["shared-start"]
        + [f"override-{i}" for i in range(n_overrides)]
        + ["shared-end"]
    )
    cidx_meta_path = tmp_path / "cidx-meta"
    _write_pattern(cidx_meta_path, "__any__", "p", _SHARED_PATTERN_CODE)
    for i in range(n_overrides):
        _write_pattern(cidx_meta_path, f"override-{i}", "p", _override_pattern_code(i))
    return aliases, cidx_meta_path


@pytest.mark.slow
def test_repo_scoped_pattern_overrides_size_cache_to_distinct_sources_not_scan_count(
    tmp_path, monkeypatch
):
    """34 distinct evaluator sources from ONE scan (33 overrides + 1 shared)
    must yield exactly 34 real identity-subprocess invocations.

    n_overrides=33 is the minimum-with-margin discriminating size: 33 is
    the smallest override count whose distinct-source total (33 overrides
    + 1 shared = 34) exceeds the 32-entry floor -- 33 distinct sources
    (32 overrides + 1 shared) would be the bare minimum that exceeds the
    floor at all, so 34 adds exactly one further source as safety margin.
    Going lower than n_overrides=33 shrinks that margin; going below
    n_overrides=32 drops back to distinct_sources <= 32 and the test would
    silently stop catching the bug.

    RED (current code): cache sized to max(32, len(scans)=1) == 32 <
    34 distinct sources -- the override sources evict the shared entry
    (LRU) before "shared-end" revisits it, forcing a duplicate recompute:
    35 invocations, not 34. See the module comment above for the general
    defect this reproduces (Bug #1784 sizing-dimension-mismatch).

    GREEN (fixed code): cache sized to the actual distinct-source count
    (34, via pre-resolving every cell up front) -- nothing evicted, exactly
    34 invocations.

    Marked @pytest.mark.slow: each distinct source triggers a real
    'xray-cli --print-cache-identity' subprocess spawn (~0.36s each), so
    even at this minimum discriminating size the test costs ~12s of real,
    irreducible subprocess work -- too close to fast-automation.sh's 15s
    per-test timeout to be safe under concurrent load (measured: 15.00s on
    an idle machine at the former n_overrides=40, exactly on the
    boundary). Mocking the subprocess would violate the anti-mock rule
    (this is the discriminating regression test for the real sizing
    formula).
    """
    _require_xray_cli_binary()
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")

    n_overrides = 33
    aliases, cidx_meta_path = _setup_repo_scoped_pattern_batch(tmp_path, n_overrides)

    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path / "cidx-data"))
    bjm = MagicMock()
    job_id = "batch-job-1784-sizing"
    bjm.jobs = {job_id: MagicMock(cancelled=False)}

    from code_indexer.server.mcp.handlers.xray_batch import _run_xray_batch_job

    with (
        patch(
            "code_indexer.xray.search_engine._get_cluster_cache",
            return_value=_FakeClusterCache(),
        ),
        _spy_on_identity_subprocess() as identity_calls,
    ):
        result = _run_xray_batch_job(
            resolved_repos=_build_resolved_repos(tmp_path, aliases),
            scans=_build_pattern_scan(),
            repo_errors=[],
            cidx_meta_path=cidx_meta_path,
            max_results=None,
            timeout_seconds=120,
            job_id=job_id,
            bjm=bjm,
            progress_callback=lambda *a, **k: None,
        )

    assert result["repos_completed"] == len(aliases), (
        f"all {len(aliases)} cells must complete for this assertion to be "
        f"meaningful: {result}"
    )
    distinct_sources = n_overrides + 1  # 33 overrides + 1 shared source
    assert len(identity_calls) == distinct_sources, (
        f"Expected exactly {distinct_sources} real 'xray-cli "
        f"--print-cache-identity' subprocess invocations (one per DISTINCT "
        f"evaluator source across the batch), got {len(identity_calls)}. "
        "The identity cache must be sized to the number of distinct "
        "evaluator sources produced by repo-scoped pattern resolution, not "
        "to len(scans) (Bug #1784 sizing-dimension-mismatch review "
        "blocker)."
    )
