"""RustNativeBackend: Rust-native xray evaluator backend (Story #1023).

Replaces PythonEvaluatorSandbox.run_batch() in the xray pipeline with a
Rust-native scanner backend.

Pipeline:
1. Validate Rust evaluator code via validate_rust_evaluator()
2. Write validated Rust code to a temp file
3. Invoke xray-cli subprocess with --dynlib, --json, --files flags
4. Parse JSON output and group findings by file path
5. Return List[(matches, errors, meta)] — one tuple per file spec

Error contract:
- ValidationError: all files get error tuples with error_type="ValidationError"
- Missing binary: all files get error tuples with error_type="BinaryNotFound"
- JSON error field set: all files get error tuples with the error message
- Subprocess non-zero exit + no parseable JSON: all files get error tuples
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_MAX_PARENT_TRAVERSAL_DEPTH = 10


def _find_project_root() -> Path:
    """Walk up from this file to find the project root (contains rust/ dir)."""
    path = Path(__file__).resolve().parent
    for _ in range(_MAX_PARENT_TRAVERSAL_DEPTH):
        if (path / "rust").is_dir():
            return path
        parent = path.parent
        if parent == path:
            break
        path = parent
    logger.warning(
        "RustNativeBackend: could not find rust/ directory within %d levels of %s;"
        " falling back to hardcoded project root",
        _MAX_PARENT_TRAVERSAL_DEPTH,
        Path(__file__),
    )
    return Path(__file__).resolve().parent.parent.parent.parent


_PROJECT_ROOT = _find_project_root()
_XRAY_CLI_DEFAULT = _PROJECT_ROOT / "rust" / "target" / "release" / "xray-cli"

# Type alias for the run_batch return type.
_BatchResult = List[
    Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]
]


def _error_tuple(
    file_path: str,
    error_type: str,
    error_message: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Build a single ([], [error_dict], None) result tuple."""
    return (
        [],
        [
            {
                "file_path": file_path,
                "line_number": 0,
                "error_type": error_type,
                "error_message": error_message,
            }
        ],
        None,
    )


def _error_all(
    file_specs: List[Dict[str, Any]],
    error_type: str,
    error_message: str,
) -> _BatchResult:
    """Return error tuples for every file spec with the given error."""
    return [
        _error_tuple(spec.get("file_path", ""), error_type, error_message)
        for spec in file_specs
    ]


class RustNativeBackend:
    """Rust-native xray evaluator backend.

    Validates Rust evaluator code, compiles to a dynamic library,
    and runs the Rust xray scanner for parallel AST evaluation.

    Drop-in replacement for PythonEvaluatorSandbox.run_batch() in the
    XRaySearchEngine pipeline.
    """

    def __init__(self) -> None:
        """Initialise backend with default xray-cli path."""
        self._xray_cli_path: Path = _XRAY_CLI_DEFAULT

    def run_batch(
        self,
        *,
        evaluator_code: str,
        file_specs: List[Dict[str, Any]],
        worker_threads: int = 4,
        timeout_seconds: int = 60,
        on_process_spawned: Optional[Callable] = None,
        repo_path: Optional[str] = None,
    ) -> _BatchResult:
        """Drop-in replacement for PythonEvaluatorSandbox.run_batch().

        Args:
            evaluator_code: Rust evaluator source code containing
                ``fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding>`` function.
            file_specs: List of dicts with file_path, source, lang,
                match_positions.
            worker_threads: Ignored — Rust uses rayon auto-threading.
            timeout_seconds: Timeout for the xray-cli subprocess.
            on_process_spawned: Optional callback when subprocess starts.
            repo_path: Base path for resolving relative file paths.

        Returns:
            List of (matches, errors, meta) tuples, one per file spec.
        """
        if not file_specs:
            return []

        rust_code, validation_error = self._validate_rust_code(evaluator_code)
        if validation_error is not None:
            return _error_all(file_specs, "ValidationError", validation_error)

        if not self._xray_cli_path.exists():
            msg = (
                f"xray-cli binary not found at {self._xray_cli_path}. "
                "Run: cd rust && cargo build --release"
            )
            logger.error("RustNativeBackend: %s", msg)
            return _error_all(file_specs, "BinaryNotFound", msg)

        base = Path(repo_path) if repo_path else Path.cwd()
        abs_paths = [str(base / spec.get("file_path", "")) for spec in file_specs]

        stdout, invoke_error = self._invoke_xray_cli(
            rust_code, abs_paths, timeout_seconds, on_process_spawned
        )
        if invoke_error is not None:
            return _error_all(file_specs, "XRayCliError", invoke_error)

        output, parse_error = self._parse_json_output(stdout)
        if parse_error is not None:
            return _error_all(file_specs, "XRayCliError", parse_error)

        cli_error = output.get("error")
        if cli_error:
            logger.warning("RustNativeBackend: xray-cli error: %s", cli_error)
            return _error_all(file_specs, "XRayCliError", cli_error)

        return self._build_results(file_specs, abs_paths, output.get("findings", []))

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _validate_rust_code(self, evaluator_code: str) -> Tuple[str, Optional[str]]:
        """Validate Rust evaluator code. Returns (rust_code, error_msg).

        The evaluator_code is expected to already be valid Rust.  This method
        checks for required signature and forbidden constructs without any
        transformation — if valid, returns the code unchanged.
        """
        from code_indexer.xray.sandbox import validate_rust_evaluator  # noqa: PLC0415

        result = validate_rust_evaluator(evaluator_code)
        if not result.ok:
            msg = result.reason or "Rust validation failed"
            logger.warning("RustNativeBackend: %s", msg)
            return "", msg
        return evaluator_code, None

    def _invoke_xray_cli(
        self,
        rust_code: str,
        abs_paths: List[str],
        timeout_seconds: int,
        on_process_spawned: Optional[Callable],
    ) -> Tuple[str, Optional[str]]:
        """Write temp file, invoke xray-cli, return (stdout, error_msg)."""
        tmp_file = tempfile.NamedTemporaryFile(
            suffix=".rs", mode="w", delete=False, prefix="xray_eval_"
        )
        try:
            tmp_file.write(rust_code)
            tmp_file.close()
            tmp_path = tmp_file.name

            cmd = [
                str(self._xray_cli_path),
                "--dynlib",
                tmp_path,
                "--files",
                *abs_paths,
                "--json",
            ]

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            if on_process_spawned is not None:
                on_process_spawned(proc)

            try:
                stdout, stderr = proc.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                msg = f"xray-cli timed out after {timeout_seconds}s"
                logger.warning("RustNativeBackend: %s", msg)
                return "", msg

            if proc.returncode != 0 and not stdout.strip():
                msg = (
                    f"xray-cli exited with code {proc.returncode}: {stderr[:200]}"
                )
                logger.warning("RustNativeBackend: %s", msg)
                return "", msg
            return stdout or "", None
        except FileNotFoundError as exc:
            msg = f"xray-cli could not be executed: {exc}"
            logger.error("RustNativeBackend: %s", msg)
            return "", msg
        finally:
            Path(tmp_file.name).unlink(missing_ok=True)

    def _parse_json_output(self, stdout: str) -> Tuple[Dict[str, Any], Optional[str]]:
        """Parse JSON from xray-cli stdout. Returns (output_dict, error_msg)."""
        try:
            return json.loads(stdout), None
        except json.JSONDecodeError as exc:
            msg = f"xray-cli produced non-JSON output: {exc}"
            logger.warning("RustNativeBackend: %s", msg)
            return {}, msg

    def _build_results(
        self,
        file_specs: List[Dict[str, Any]],
        abs_paths: List[str],
        findings: List[Dict[str, Any]],
    ) -> _BatchResult:
        """Group findings by file and build result tuples per spec."""
        findings_by_abs: Dict[str, List[Dict[str, Any]]] = {}
        for finding in findings:
            fpath = finding.get("file", "")
            if fpath not in findings_by_abs:
                findings_by_abs[fpath] = []
            findings_by_abs[fpath].append(finding)

        results: _BatchResult = []
        for spec, abs_path in zip(file_specs, abs_paths):
            spec_findings = findings_by_abs.get(abs_path, [])
            if not spec_findings:
                results.append(([], [], None))
                continue
            matches = _build_matches(spec, spec_findings)
            results.append((matches, [], None))

        return results


def _build_matches(
    spec: Dict[str, Any],
    spec_findings: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Convert xray-cli findings to match dicts for one file spec."""
    source = spec.get("source", "")
    lang = spec.get("lang", "")
    rel_path = spec.get("file_path", "")
    source_lines = source.splitlines()

    matches: List[Dict[str, Any]] = []
    for finding in spec_findings:
        line_num = finding.get("line", 0)
        idx = line_num - 1
        line_content = source_lines[idx] if 0 <= idx < len(source_lines) else ""
        matches.append(
            {
                "line_number": line_num,
                "file_path": rel_path,
                "language": lang,
                "pattern": finding.get("pattern", ""),
                "snippet": finding.get("snippet", ""),
                "line_content": line_content,
            }
        )
    return matches
