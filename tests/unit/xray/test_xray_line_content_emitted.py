"""line_content is emitted by xray-cli; Python no longer re-reads matched files.

Before, ``_build_matches`` re-opened every matched file after the scan just to
fill each match's ``line_content``. xray-cli already holds the source, so it
now emits the line itself. Python keeps the re-read ONLY as the fallback for an
older binary on a local repository; under a delegated content backend a binary
that omits the field is an error, never a silently blank column.

The real-binary tests prove the emitted value is byte-for-byte what the Python
re-read produced, on the same fixture, including line endings and invalid
UTF-8 that a naive line split would get wrong.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from code_indexer.server.startup import content_backend_wiring as wiring
from code_indexer.xray import rust_backend as rb
from code_indexer.xray.rust_backend import RustNativeBackend, _build_matches

#: A finding on every function, string and statement, plus one past the end
#: of the file, so in-range, multi-line-file and out-of-range lines all occur.
_LINES_EVALUATOR = """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let mut findings = Vec::new();
    for kind in ["function_definition", "string", "expression_statement"] {
        for n in node.descendants_of_kind(kind) {
            findings.push(EvalFinding { pattern: kind.to_string(), line: n.start_line, snippet: String::new() });
        }
    }
    findings.push(EvalFinding { pattern: "past-end".to_string(), line: 999, snippet: String::new() });
    findings
}
"""

#: Lines Python's splitlines() and a naive split disagree on, plus bytes that
#: are not UTF-8: the emitted line must match Python's reading exactly.
_TRICKY_SOURCE = (
    b"def a():\r\n"
    b"    return '\xff\xfe'\r\n"
    b"def b():\x0c    pass\n"
    b"def c(): return '\xe2\x80\xa8'\n"
    b"x = '\xc3\xa9t\xc3\xa9'\r"
    b"y = 1\n"
)


def _spec(rel: str = "a.py") -> Dict[str, Any]:
    return {"file_path": rel, "lang": "python", "match_positions": []}


def _finding(line: int, **extra: Any) -> Dict[str, Any]:
    return {"pattern": "p", "file": "", "line": line, "snippet": "s", **extra}


# --------------------------------------------------------------------------
# _build_matches
# --------------------------------------------------------------------------


def test_emitted_line_content_is_used_without_reading_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "a.py"
    src.write_text("on disk\n")

    # A spy, not a raiser: _build_matches logs and swallows a failed read, so
    # only a record of the call can show that none was attempted.
    reads: List[Path] = []
    original = Path.read_bytes

    def _spy(self: Path) -> bytes:
        reads.append(self)
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", _spy)
    matches = _build_matches(
        _spec(),
        [_finding(1, line_content="emitted")],
        abs_path=str(src),
        resolved_root=tmp_path.resolve(),
    )
    assert matches[0]["line_content"] == "emitted"
    assert reads == []


def test_an_older_binary_falls_back_to_reading_a_local_file(tmp_path: Path) -> None:
    src = tmp_path / "a.py"
    src.write_bytes(_TRICKY_SOURCE)
    expected = _TRICKY_SOURCE.decode("utf-8", errors="replace").splitlines()
    matches = _build_matches(
        _spec(),
        [_finding(n) for n in (0, 1, 2, 5, 99)],
        abs_path=str(src),
        resolved_root=tmp_path.resolve(),
    )
    assert [m["line_content"] for m in matches] == [
        "",
        expected[0],
        expected[1],
        expected[4],
        "",
    ]


def test_an_emitted_line_outside_the_root_is_still_withheld(tmp_path: Path) -> None:
    """Containment applies to the emitted value too: it is the last point
    before a finding's content leaves cidx."""
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("secret\n")
    (root / "link.py").symlink_to(outside)
    matches = _build_matches(
        _spec("link.py"),
        [_finding(1, line_content="secret")],
        abs_path=str(root / "link.py"),
        resolved_root=root.resolve(),
    )
    assert matches[0]["line_content"] == ""


# --------------------------------------------------------------------------
# run_batch: fail loud under delegation
# --------------------------------------------------------------------------


def _backend_returning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, findings: List[Dict[str, Any]]
) -> RustNativeBackend:
    binary = tmp_path / "xray-cli"
    binary.write_text("")
    backend = RustNativeBackend(xray_cache_backend=None)
    monkeypatch.setattr(backend, "_xray_cli_path", binary)
    monkeypatch.setattr(backend, "_validate_rust_code", lambda code: (code, None))
    output = {"findings": findings, "cached": True, "debug_messages": []}
    monkeypatch.setattr(
        backend, "_invoke_xray_cli", lambda *a, **k: (json.dumps(output), None)
    )
    return backend


class _Delegated:
    is_local = False


@pytest.mark.parametrize("delegated", [False, True])
def test_emitted_line_content_passes_through_either_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delegated: bool
) -> None:
    if delegated:
        monkeypatch.setattr(wiring, "server_content_backend", lambda: _Delegated())
    abs_path = str(tmp_path / "a.py")
    backend = _backend_returning(
        monkeypatch, tmp_path, [{**_finding(1, line_content="x = 1"), "file": abs_path}]
    )
    (tmp_path / "a.py").write_text("x = 1\n")
    results = backend.run_batch(
        evaluator_code="e", file_specs=[_spec()], repo_path=str(tmp_path)
    )
    assert results[0][0][0]["line_content"] == "x = 1"
    assert results[0][1] == []


def test_a_binary_without_line_content_is_an_error_under_delegation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wiring, "server_content_backend", lambda: _Delegated())

    def _no_read(*_a: Any, **_k: Any) -> bytes:
        raise AssertionError("a local repository read under delegation")

    monkeypatch.setattr(rb, "_build_matches", _no_read)
    backend = _backend_returning(
        monkeypatch, tmp_path, [{**_finding(1), "file": str(tmp_path / "a.py")}]
    )
    results = backend.run_batch(
        evaluator_code="e", file_specs=[_spec()], repo_path=str(tmp_path)
    )
    assert len(results) == 1
    matches, errors, _ = results[0]
    assert matches == []
    assert errors[0]["file_path"] == ""  # a whole-batch failure, not "no matches"
    assert errors[0]["error_type"] == "XRayCliError"
    assert "line_content" in errors[0]["error_message"]


# --------------------------------------------------------------------------
# the real binary
# --------------------------------------------------------------------------


def _scan(
    repo: Path, monkeypatch: pytest.MonkeyPatch, *, strip_line_content: bool
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Run the real pipeline; optionally strip line_content from xray-cli's
    JSON first, which is exactly what an older binary produced."""
    from code_indexer.xray.search_engine import XRaySearchEngine

    emitted: List[Dict[str, Any]] = []
    original = RustNativeBackend._parse_json_output

    def _capture(self: RustNativeBackend, stdout: str) -> Any:
        output, error = original(self, stdout)
        for finding in output.get("findings", []):
            emitted.append(dict(finding))
            if strip_line_content:
                finding.pop("line_content", None)
        return output, error

    monkeypatch.setattr(RustNativeBackend, "_parse_json_output", _capture)
    result = XRaySearchEngine().run(
        repo_path=repo,
        driver_regex=r"\.py$",
        evaluator_code=_LINES_EVALUATOR,
        search_target="filename",
    )
    assert result["evaluation_errors"] == []
    return result["matches"], emitted


def test_real_binary_emits_line_content_identical_to_the_python_reread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")
    from tests.unit.xray.conftest import require_xray_cli_binary

    require_xray_cli_binary()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tricky.py").write_bytes(_TRICKY_SOURCE)
    (repo / "plain.py").write_text("import os\n\nprint(os.sep)\n")

    emitted_run, emitted = _scan(repo, monkeypatch, strip_line_content=False)
    reread_run, _ = _scan(repo, monkeypatch, strip_line_content=True)

    assert emitted and all("line_content" in f for f in emitted)
    assert emitted_run == reread_run
    # And against an independent oracle: Python's own reading of the bytes.
    lines = _TRICKY_SOURCE.decode("utf-8", errors="replace").splitlines()
    tricky = [m for m in emitted_run if m["file_path"] == "tricky.py"]
    assert tricky
    for match in tricky:
        idx = match["line_number"] - 1
        assert match["line_content"] == (lines[idx] if 0 <= idx < len(lines) else "")
    assert any("\ufffd" in m["line_content"] for m in tricky)
    assert any(m["line_number"] == 999 and m["line_content"] == "" for m in tricky)
