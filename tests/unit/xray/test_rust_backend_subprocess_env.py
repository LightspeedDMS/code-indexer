"""xray-cli subprocesses run with a minimal, explicit environment.

xray-cli compiles caller-supplied Rust and loads it into its own process, so
whatever is in its environment is readable by that code. These tests prove the
server's secrets never reach it, that X-Ray still gets what it needs to work,
that no spawn of xray-cli can quietly go back to inheriting everything, and
that the scan path's compile is heap-bounded while the scan is not.
"""

from __future__ import annotations

import ast
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from code_indexer.xray import rust_backend
from code_indexer.xray.rust_backend import xray_subprocess_env

_SRC = Path(rust_backend.__file__).resolve().parents[1]  # src/code_indexer
_HELPER = "xray_subprocess_env"
_SPAWN_FUNCS = {"run", "Popen", "call", "check_call", "check_output"}
_DUMP_ENV = "import os,json;print(json.dumps(dict(os.environ)))"
_SENTINEL_VALUE = "sentinel-must-not-leak"


@pytest.fixture
def secret_keys(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """Server-style secrets in the parent, plus the keys X-Ray must keep."""
    keys = [
        "VOYAGE_API_KEY",
        "DATABASE_URL",
        "CIDX_SERVER_SECRET",
        f"FOO_{secrets.token_hex(4).upper()}_TOKEN",
    ]
    for key in keys:
        monkeypatch.setenv(key, _SENTINEL_VALUE)
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
    monkeypatch.setenv("HOME", os.environ.get("HOME", "/nonexistent"))
    monkeypatch.setenv("CIDX_DATA_DIR", "/srv/cidx-data")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    return keys


_REQUIRED = (
    "PATH",
    "HOME",
    "CIDX_DATA_DIR",
    "LC_ALL",
    "CIDX_XRAY_COMPILE_RLIMIT_DATA_BYTES",
)


def _assert_minimal(child_env: Dict[str, str], keys: List[str]) -> None:
    leaked = [key for key in keys if key in child_env]
    assert not leaked, f"secret env keys reached the xray-cli child: {leaked}"
    assert _SENTINEL_VALUE not in child_env.values()
    for key in _REQUIRED:
        assert key in child_env, f"{key} is required by xray-cli and was dropped"
    assert child_env["CIDX_XRAY_COMPILE_RLIMIT_DATA_BYTES"] == str(
        rust_backend._COMPILE_RLIMIT_DATA_BYTES
    )


# ---------------------------------------------------------------------------
# A real child sees none of the secrets
# ---------------------------------------------------------------------------


def test_a_real_child_spawned_with_the_helper_sees_no_secrets(secret_keys) -> None:
    out = subprocess.run(
        [sys.executable, "-c", _DUMP_ENV],
        capture_output=True,
        text=True,
        timeout=30,
        env=xray_subprocess_env(),
    )
    assert out.returncode == 0, out.stderr
    _assert_minimal(json.loads(out.stdout), secret_keys)


def test_the_real_xray_cli_spawn_path_sees_no_secrets(secret_keys) -> None:
    """Through `_run_xray_cli_process` itself -- the Popen every scan,
    compile-only and graph run goes through -- not just the helper."""
    backend = rust_backend.RustNativeBackend()
    stdout, error = backend._run_xray_cli_process(
        [sys.executable, "-c", _DUMP_ENV],
        timeout_seconds=30,
        on_process_spawned=None,
        acquire_compile_slot=False,
    )
    assert error is None, error
    _assert_minimal(json.loads(stdout), secret_keys)


def test_only_allowlisted_keys_are_passed(secret_keys, monkeypatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", _SENTINEL_VALUE)
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    env = xray_subprocess_env()
    allowed = set(rust_backend._XRAY_ENV_PASSTHROUGH_KEYS) | {
        rust_backend._COMPILE_RLIMIT_DATA_ENV
    }
    unexpected = [
        key
        for key in env
        if key not in allowed
        and not key.startswith(rust_backend._XRAY_ENV_PASSTHROUGH_PREFIXES)
    ]
    assert not unexpected, f"keys outside the allowlist: {unexpected}"


def test_the_allowlist_holds_no_secret_shaped_keys() -> None:
    """Catches a secret being added to the allowlist itself."""
    markers = ("KEY", "SECRET", "TOKEN", "PASSWORD", "URL", "CREDENTIAL", "AUTH")
    keys = rust_backend._XRAY_ENV_PASSTHROUGH_KEYS
    flagged = [key for key in keys if any(marker in key for marker in markers)]
    assert not flagged, f"secret-shaped keys in the xray env allowlist: {flagged}"
    assert set(keys) == {
        "PATH",
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LANGUAGE",
        "CIDX_DATA_DIR",
        "RUSTUP_HOME",
        "CARGO_HOME",
    }, "the allowlist changed -- review every added key for what it exposes"
    assert rust_backend._XRAY_ENV_PASSTHROUGH_PREFIXES == ("LC_",)


# ---------------------------------------------------------------------------
# Each of the three spawn sites passes env=
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.kwargs: Dict[str, Any] = {}

    def run(self, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        self.kwargs = kwargs
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")


def _assert_spawn_env(kwargs: Dict[str, Any], keys: List[str]) -> None:
    assert "env" in kwargs, "xray-cli spawned without env= inherits every secret"
    assert kwargs["env"] == xray_subprocess_env()
    _assert_minimal(kwargs["env"], keys)


def test_graph_extractor_extensions_spawn_passes_env(
    secret_keys, monkeypatch, tmp_path
) -> None:
    fake_cli = tmp_path / "xray-cli"
    fake_cli.write_text("")
    recorder = _Recorder()
    monkeypatch.setattr(rust_backend.subprocess, "run", recorder.run)
    rust_backend.get_graph_extractor_extensions(xray_cli_path=fake_cli)
    _assert_spawn_env(recorder.kwargs, secret_keys)


def test_cache_identity_spawn_passes_env(secret_keys, monkeypatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(rust_backend.subprocess, "run", recorder.run)
    rust_backend.RustNativeBackend()._run_cache_identity_subprocess("fn x() {}")
    _assert_spawn_env(recorder.kwargs, secret_keys)


class _FakePopen:
    seen: Dict[str, Any] = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _FakePopen.seen = kwargs
        self.pid = 4321
        self.returncode = 0

    def communicate(self, timeout: Any = None) -> Any:
        return ("{}", "")

    def wait(self, timeout: Any = None) -> int:
        return 0


def _popen_kwargs(monkeypatch, **flags: bool) -> Dict[str, Any]:
    monkeypatch.setattr(rust_backend.subprocess, "Popen", _FakePopen)
    rust_backend.RustNativeBackend()._run_xray_cli_process(
        ["xray-cli", "--json"], 30, None, **flags
    )
    return _FakePopen.seen


def test_compile_and_run_spawn_passes_env(secret_keys, monkeypatch) -> None:
    for flags in (
        {"acquire_compile_slot": True, "apply_compile_limits": False},  # scan
        {"acquire_compile_slot": True, "apply_compile_limits": True},  # compile
        {"acquire_compile_slot": False},  # --build-graph / --analyze-graph
    ):
        _assert_spawn_env(_popen_kwargs(monkeypatch, **flags), secret_keys)


# ---------------------------------------------------------------------------
# Guard: no xray-cli spawn without the helper
# ---------------------------------------------------------------------------


def _spawns_missing_helper(source: str) -> List[int]:
    """Line numbers of subprocess spawns that do not pass env=<helper>()."""
    bad = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_spawn = (
            isinstance(func, ast.Attribute)
            and func.attr in _SPAWN_FUNCS
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        ) or (
            isinstance(func, ast.Attribute)
            and func.attr in ("create_subprocess_exec", "create_subprocess_shell")
        )
        if not is_spawn:
            continue
        env = next((kw.value for kw in node.keywords if kw.arg == "env"), None)
        uses_helper = (
            isinstance(env, ast.Call)
            and isinstance(env.func, ast.Name)
            and env.func.id == _HELPER
        )
        if not uses_helper:
            bad.append(node.lineno)
    return bad


def _xray_spawning_modules() -> List[Path]:
    """Every X-Ray module, plus any module that drives xray-cli itself."""
    modules = sorted((_SRC / "xray").rglob("*.py"))
    for path in sorted(_SRC.rglob("*.py")):
        if path in modules:
            continue
        text = path.read_text(encoding="utf-8")
        if "resolve_xray_cli_path" in text or "_xray_cli_path" in text:
            modules.append(path)
    return modules


def test_every_xray_cli_spawn_uses_the_minimal_env() -> None:
    offenders = {}
    for path in _xray_spawning_modules():
        lines = _spawns_missing_helper(path.read_text(encoding="utf-8"))
        if lines:
            offenders[str(path.relative_to(_SRC))] = lines
    assert not offenders, (
        "xray-cli spawned without env=xray_subprocess_env(); the child would "
        f"inherit the server's secrets: {offenders}"
    )


def test_the_guard_actually_scans_rust_backend() -> None:
    assert Path(rust_backend.__file__).resolve() in _xray_spawning_modules()
    source = Path(rust_backend.__file__).read_text(encoding="utf-8")
    assert source.count(f"env={_HELPER}()") >= 3


def test_the_guard_catches_a_spawn_without_env() -> None:
    assert _spawns_missing_helper("import subprocess\nsubprocess.run(['x'])\n") == [2]
    assert _spawns_missing_helper(
        "import subprocess, os\nsubprocess.Popen(['x'], env=dict(os.environ))\n"
    ) == [2]
    assert (
        _spawns_missing_helper(
            "import subprocess\nsubprocess.run(['x'], env=xray_subprocess_env())\n"
        )
        == []
    )


# ---------------------------------------------------------------------------
# The scan path's compile is heap-bounded; the scan and graph runs are not
# ---------------------------------------------------------------------------


def test_compile_limit_override_is_resolved_from_env(monkeypatch) -> None:
    default = rust_backend._DEFAULT_COMPILE_RLIMIT_DATA_BYTES
    assert default == 1024 * 1024 * 1024
    monkeypatch.delenv(rust_backend._COMPILE_RLIMIT_DATA_ENV, raising=False)
    assert rust_backend._resolve_compile_rlimit_data_bytes() == default
    monkeypatch.setenv(rust_backend._COMPILE_RLIMIT_DATA_ENV, str(2 * default))
    assert rust_backend._resolve_compile_rlimit_data_bytes() == 2 * default
    for bad in ("0", "-5", "lots", " "):
        monkeypatch.setenv(rust_backend._COMPILE_RLIMIT_DATA_ENV, bad)
        assert rust_backend._resolve_compile_rlimit_data_bytes() == default


def test_scan_spawn_hands_xray_cli_the_compile_limit(monkeypatch) -> None:
    """The scan process itself runs without preexec limits (its scan is
    repository-sized); its compile is bounded through the env value xray-cli
    applies to the rustc child."""
    kwargs = _popen_kwargs(
        monkeypatch, acquire_compile_slot=True, apply_compile_limits=False
    )
    assert kwargs.get("preexec_fn") is None
    assert kwargs["env"]["CIDX_XRAY_COMPILE_RLIMIT_DATA_BYTES"] == str(
        rust_backend._COMPILE_RLIMIT_DATA_BYTES
    )


def test_graph_runs_get_no_memory_cap(monkeypatch) -> None:
    kwargs = _popen_kwargs(monkeypatch, acquire_compile_slot=False)
    assert kwargs.get("preexec_fn") is None


# ---------------------------------------------------------------------------
# Real end to end: compile + scan under the minimal env
# ---------------------------------------------------------------------------

_XRAY_CLI = rust_backend.resolve_xray_cli_path()
_HAS_TOOLCHAIN = _XRAY_CLI.exists() and shutil.which("rustc") is not None
_EVALUATOR = """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let mut findings = Vec::new();
    for m in node.descendants_of_kind("method_declaration") {
        findings.push(EvalFinding {
            pattern: "method-%s".to_string(),
            line: m.start_line,
            snippet: m.text().to_string(),
        });
    }
    findings
}
"""
_JAVA = "public class A {\n  void a() {}\n  void b() {}\n}\n"


def _scan(tmp_path: Path) -> Any:
    (tmp_path / "A.java").write_text(_JAVA, encoding="utf-8")
    spec = {
        "file_path": "A.java",
        "source": _JAVA,
        "lang": "java",
        "match_positions": [],
    }
    # A fresh evaluator every time, so the compile really runs instead of
    # being served from the cache.
    code = _EVALUATOR.replace("%s", secrets.token_hex(6))
    return rust_backend.RustNativeBackend().run_batch(
        evaluator_code=code,
        file_specs=[spec],
        repo_path=str(tmp_path),
        timeout_seconds=180,
    )


@pytest.mark.skipif(not _HAS_TOOLCHAIN, reason="xray-cli binary or rustc not available")
def test_real_compile_and_scan_work_under_the_minimal_env(
    secret_keys, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "repo").mkdir()
    results = _scan(tmp_path / "repo")
    assert len(results) == 1
    matches, errors, _meta = results[0]
    assert not errors, errors
    assert len(matches) == 2, matches


@pytest.mark.skipif(not _HAS_TOOLCHAIN, reason="xray-cli binary or rustc not available")
def test_real_scan_path_compile_is_heap_bounded(monkeypatch, tmp_path) -> None:
    """A heap bound far below what rustc needs must fail the scan path's
    compile as an ordinary error, proving the limit reaches rustc there."""
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(rust_backend, "_COMPILE_RLIMIT_DATA_BYTES", 32 * 1024 * 1024)
    (tmp_path / "repo").mkdir()
    results = _scan(tmp_path / "repo")
    assert len(results) == 1
    _matches, errors, _meta = results[0]
    assert errors, "a 32 MiB heap bound must stop rustc"
    assert errors[0]["error_type"] == "CompileError", errors
    assert "memory" in errors[0]["error_message"], errors
