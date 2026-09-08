"""R2-2 (Codex re-review): cross-language conformance test.

Verifies that Python's `validate_rust_evaluator()` (the "fast subset
pre-flight") never ACCEPTS an evaluator that Rust's REAL compile pipeline
(the authoritative validator, via `validator::validate_evaluator_source`
inside `compiler::compile_evaluator`) REJECTS, over a representative
corpus. That direction of divergence is the concrete bug this story
fixes: a user passes Python's pre-flight only to fail late and
confusingly at Rust's compile step.

Uses the REAL `xray-cli --compile-only` subcommand -- no mocks. Skipped
when the release binary isn't built.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from code_indexer.xray.sandbox import validate_rust_evaluator

REPO_ROOT = Path(__file__).parent.parent.parent.parent
XRAY_CLI = REPO_ROOT / "rust" / "target" / "release" / "xray-cli"

pytestmark = pytest.mark.skipif(
    not XRAY_CLI.exists(),
    reason="xray-cli binary not found -- run: cd rust && cargo build --release -p xray-cli",
)

# Representative corpus spanning: allowed macros (vec!/format!/matches!),
# an unlisted macro (assert!), a qualified allowed-name macro (evil::vec!),
# a macro_rules! definition, and two non-macro forbidden constructs
# (unsafe, std::process) included as a sanity check that both layers
# agree on already-well-covered ground.
_CONFORMANCE_CORPUS: list[tuple[str, str]] = [
    (
        "allowed_vec",
        "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }\n",
    ),
    (
        "allowed_format",
        'fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { let _ = format!("{}", node.kind); Vec::new() }\n',
    ),
    (
        "allowed_matches",
        'fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { let _ = matches!(node.kind.as_str(), "x" | "y"); Vec::new() }\n',
    ),
    (
        "unlisted_macro_assert",
        "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { assert!(node.start_line > 0); Vec::new() }\n",
    ),
    (
        "qualified_vec",
        "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { let _v: Vec<i32> = evil::vec![1]; Vec::new() }\n",
    ),
    (
        "macro_rules_definition",
        "macro_rules! innocuous { () => { 42 }; }\n"
        "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { let _ = innocuous!(); Vec::new() }\n",
    ),
    (
        "unsafe_block",
        "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { unsafe {} Vec::new() }\n",
    ),
    (
        "std_process",
        "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { std::process::exit(0); Vec::new() }\n",
    ),
]


def _rust_accepts(code: str, tmp_path: Path) -> bool:
    """Real compile-preflight via `xray-cli --compile-only` (no mocks).
    True means Rust's validator+compiler accepted the source
    (the JSON report's `error` field is null)."""
    src_file = tmp_path / "evaluator.rs"
    src_file.write_text(code, encoding="utf-8")
    result = subprocess.run(
        [str(XRAY_CLI), "--compile-only", "--dynlib", str(src_file)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"xray-cli --compile-only exited {result.returncode}: {result.stderr}"
    )
    payload = json.loads(result.stdout)
    return payload["error"] is None


@pytest.mark.parametrize("case_id,code", _CONFORMANCE_CORPUS)
def test_python_never_accepts_what_rust_rejects(
    case_id: str, code: str, tmp_path: Path
) -> None:
    """For every corpus item, if Python accepts it, Rust's real compile
    pipeline must ALSO accept it -- Python must never be MORE PERMISSIVE
    than Rust. This is exactly the cross-layer divergence class R2-2
    fixes (matches!/assert!/qualified-macro cases all previously
    diverged)."""
    python_result = validate_rust_evaluator(code)
    if python_result.ok:
        assert _rust_accepts(code, tmp_path), (
            f"[{case_id}] Python ACCEPTED this evaluator (ok=True) but "
            "Rust's real compile pipeline REJECTED it -- Python must never "
            "be more permissive than Rust."
        )


@pytest.mark.parametrize("pattern_name", ["catch-rethrow", "deep-nesting"])
def test_shipped_seed_patterns_compile_via_real_pipeline(
    pattern_name: str, tmp_path: Path
) -> None:
    """Permanent regression guard for the R2-2 finding: the stricter
    macro allowlist (before matches! token-inspection was added) broke
    the shipped catch-rethrow seed pattern, which uses matches! --
    reproduced live via `xray-cli --compile-only` returning
    "Line 8: `matches!` macro is not allowed". Resolves the REAL shipped
    pattern (with its real parameter consts prepended) through BOTH the
    Python pre-flight and the real Rust compile pipeline."""
    from code_indexer.server.services.xray_pattern_service import XrayPatternService

    cidx_meta = tmp_path / "cidx-meta"
    svc = XrayPatternService(cidx_meta)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(svc, "_git_commit", lambda **kw: None)
        svc.ensure_seed_patterns()
        evaluator_code, _params = svc.resolve_and_prepare_pattern(
            repo_alias="some-repo", pattern_name=pattern_name
        )

    python_result = validate_rust_evaluator(evaluator_code)
    assert python_result.ok is True, (
        f"Python must accept the shipped '{pattern_name}' seed pattern; got "
        f"reason={python_result.reason!r}"
    )
    assert _rust_accepts(evaluator_code, tmp_path), (
        f"Rust's real compile pipeline must accept the shipped "
        f"'{pattern_name}' seed pattern"
    )
