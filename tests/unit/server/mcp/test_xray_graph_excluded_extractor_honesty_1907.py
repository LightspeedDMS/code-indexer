"""Bug #1907 -- the most dangerous defect in epic #1906.

Scoping `include_patterns` to one extractable language on a mixed-language
repository made `analyze_graph` report `fact_graph_complete: true` with
every degradation counter at zero, while the graph was missing every call
site in the excluded language. The excluded files never became candidates
in `_collect_graph_candidate_files`'s walk, so no Rust-side counter could
ever count them -- narrowing the scope did not improve completeness, it
HID the incompleteness. A method called only from the unread language got
no inbound edge and could be reported `is_definitely_dead_code() ==
Some(true)`: a false dead verdict on live code, told with a clean,
confident, wrong `ok: true, status: "ran_ok", fact_graph_complete: true`.

THE decisive fixture (`JAVA_UTIL`/`KOTLIN_CALLER`, reused verbatim from
`rust/xray-core/tests/bug_1908_kotlin_graph_extractor.rs`): a Java method
called ONLY from Kotlin. `include_patterns=["*.java"]` excludes the Kotlin
caller entirely -- BEFORE this fix, that produced the dishonest shape
above; AFTER, the response must say so honestly (`fact_graph_complete:
false`, a named reason, and which language went unread), while the
underlying graph still, correctly, lacks the Kotlin call site (this fix is
about HONESTY, not closing the gap -- that is the Kotlin extractor itself,
already landed).

These are genuine component tests against the REAL compiled xray-cli
release binary (skipped if not built) -- no subprocess mocking, mirroring
`test_analyze_graph_handler.py`'s own established convention.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, cast
from unittest.mock import patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers.xray_graph import _collect_graph_candidate_files
from code_indexer.xray.rust_backend import _XRAY_CLI_DEFAULT


def _require_xray_cli_binary() -> None:
    if not _XRAY_CLI_DEFAULT.exists():
        pytest.skip(
            f"xray-cli binary not built at {_XRAY_CLI_DEFAULT}; "
            "run 'cargo build --release' inside rust/ to enable this test."
        )


def _make_user(role: UserRole = UserRole.NORMAL_USER) -> User:
    return User(
        username="testuser",
        password_hash="not-a-real-credential",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _parse_response(result: Dict[str, Any]) -> Dict[str, Any]:
    return cast(Dict[str, Any], json.loads(result["content"][0]["text"]))


def _import_handler():
    from code_indexer.server.mcp.handlers.xray_graph import handle_analyze_graph

    return handle_analyze_graph


# Reused verbatim (neutral com.example.* naming, per this repository's
# public-disclosure discipline) from `rust/xray-core/tests/
# bug_1908_kotlin_graph_extractor.rs::java_method_called_only_from_kotlin_
# is_not_reported_dead` -- the SAME fixture already proves the graph
# binder resolves this cross-language edge correctly WHEN both files are
# fed to it. This test proves the separate, Python-side candidate-
# collection defect: the edge is never given a CHANCE to resolve once
# `include_patterns` drops the Kotlin file before Rust ever sees it.
JAVA_UTIL = """\
package com.example.app;

public class JavaUtil {
    private static String helper(String raw) {
        return raw.trim();
    }
}
"""

KOTLIN_CALLER = """\
package com.example.app

fun useJavaHelper(raw: String): String {
    return JavaUtil.helper(raw)
}
"""

# Reports every symbol the graph considers DEFINITELY dead -- the inverse
# of test_rust_backend_graph_mode.py's CROSS_FILE_DEAD_CODE_EVALUATOR
# (which reports "not dead"), because this bug is about a FALSE dead
# verdict, not a false not-dead one.
DEFINITELY_DEAD_EVALUATOR = """\
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    let mut i: u32 = 0;
    while i < 256 {
        if let Some(sym) = g.resolve_symbol(i) {
            if g.is_definitely_dead_code(i) == Some(true) {
                let sig = g.signature_for(i).unwrap_or("").to_string();
                result.findings.push(ReduceFinding {
                    pattern: "definitely_dead".to_string(),
                    message: sig.clone(),
                    involved: vec![sym],
                    signatures: vec![sig],
                });
            }
        }
        i += 1;
    }
    result
}
"""


def _write_mixed_fixture(repo_root: Path) -> None:
    pkg = repo_root / "com" / "example" / "app"
    pkg.mkdir(parents=True)
    (pkg / "JavaUtil.java").write_text(JAVA_UTIL)
    (pkg / "KotlinCaller.kt").write_text(KOTLIN_CALLER)


def _dead_helper_findings(response: Dict[str, Any]) -> list:
    return [
        f
        for f in response.get("findings", [])
        if any("helper" in str(sig) for sig in f.get("signatures", []))
    ]


@pytest.mark.asyncio
async def test_unscoped_analysis_correctly_resolves_the_cross_language_edge(
    tmp_path: Path,
) -> None:
    """Control/sanity case: WITHOUT any include/exclude narrowing, both
    files are real candidates, the graph is genuinely complete, and
    JavaUtil.helper is correctly NOT reported dead (it has a real Kotlin
    caller). This is the honest baseline the narrowed case below must be
    judged against -- if this control ever failed, the discriminating test
    below would be meaningless (it would be testing a fixture bug, not the
    candidate-collection honesty bug).
    """
    _require_xray_cli_binary()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_mixed_fixture(repo_root)

    handler = _import_handler()
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path",
        return_value=str(repo_root),
    ):
        result = await handler(
            {
                "repository_alias": "mixed-repo",
                "evaluator_code": DEFINITELY_DEAD_EVALUATOR,
            },
            _make_user(),
        )
    response = _parse_response(result)

    assert response["ok"] is True, f"expected success: {response}"
    assert response["fact_graph_complete"] is True
    assert response["degradation"]["files_excluded_with_extractor"] == 0
    assert response["degradation"]["files_excluded_without_extractor"] == 0
    assert response["languages_excluded_with_extractor"] == []
    assert _dead_helper_findings(response) == [], (
        "JavaUtil.helper has a real Kotlin caller when both files are "
        "fed to the graph -- it must never be reported dead here"
    )


@pytest.mark.asyncio
async def test_excluding_kotlin_via_include_patterns_is_honest_about_the_gap(
    tmp_path: Path,
) -> None:
    """THE decisive test (Bug #1907). `include_patterns=["*.java"]` drops
    KotlinCaller.kt before it ever becomes a candidate file, so the graph
    Rust builds is missing JavaUtil.helper's only real call site --
    `is_definitely_dead_code` reports it dead, exactly as documented in the
    bug (this half of the assertion holds both BEFORE and AFTER the fix:
    the fix is about HONESTY, not about closing the gap).

    Before this fix: `fact_graph_complete` read `true` here, with every
    degradation counter at zero -- a clean, confident, WRONG answer. After
    the fix: the response must say plainly that a file whose language HAS
    a graph extractor was excluded, name Kotlin as the unread language, and
    downgrade `fact_graph_complete` to `false` with a stated reason.
    """
    _require_xray_cli_binary()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_mixed_fixture(repo_root)

    handler = _import_handler()
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path",
        return_value=str(repo_root),
    ):
        result = await handler(
            {
                "repository_alias": "mixed-repo",
                "evaluator_code": DEFINITELY_DEAD_EVALUATOR,
                "include_patterns": ["*.java"],
            },
            _make_user(),
        )
    response = _parse_response(result)

    assert response["ok"] is True, f"expected success (analysis itself ran): {response}"

    # The gap itself: the excluded-but-extractable Kotlin file still makes
    # JavaUtil.helper look dead -- proving the mechanism the honesty fix is
    # reporting on is real, not merely theoretical.
    dead_helper = _dead_helper_findings(response)
    assert dead_helper, (
        "expected JavaUtil.helper to still be (incorrectly) reported dead "
        "-- the candidate walk excluded its only real caller. If this "
        "assertion fails, the fixture itself changed behavior and the "
        "honesty assertions below would not be testing a real gap."
    )

    # THE fix: the response must be honest about that gap.
    assert response["fact_graph_complete"] is False, (
        "narrowing include_patterns to *.java excluded KotlinCaller.kt, "
        "whose language (Kotlin) DOES have a graph extractor -- "
        "fact_graph_complete must never read true while that exclusion "
        "stands. This is the exact 'clean, confident, wrong' shape Bug "
        "#1907 exists to eliminate."
    )
    degradation = response["degradation"]
    assert degradation["files_excluded_with_extractor"] == 1, degradation
    assert degradation["files_excluded_without_extractor"] == 0, degradation
    assert response["languages_excluded_with_extractor"] == ["Kotlin"], (
        "the response must NAME which language went unread, not merely "
        "flag that something is incomplete"
    )
    assert "repo_index_incomplete" in (response.get("completeness_reasons") or []), (
        "the new counter must feed the EXISTING completeness_reasons list "
        "(#1897), never a parallel undiscoverable channel"
    )


@pytest.mark.asyncio
async def test_excluding_a_file_with_no_extractor_never_downgrades_completeness(
    tmp_path: Path,
) -> None:
    """Pins the OTHER half of the distinction this bug requires: excluding
    a file whose language has NO graph extractor at all (e.g. a stray
    `.md`) must be treated differently from excluding an extractor-backed
    file -- conflating the two would re-create Bug #1907 in a new shape
    (either falsely claiming completeness for a real gap, or falsely
    downgrading completeness for a file that could never have contributed
    a real call edge anyway).
    """
    _require_xray_cli_binary()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    pkg = repo_root / "com" / "example" / "app"
    pkg.mkdir(parents=True)
    (pkg / "JavaUtil.java").write_text(JAVA_UTIL)
    (repo_root / "README.md").write_text("# not a source file for graph mode\n")

    handler = _import_handler()
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path",
        return_value=str(repo_root),
    ):
        result = await handler(
            {
                "repository_alias": "java-only-repo",
                "evaluator_code": DEFINITELY_DEAD_EVALUATOR,
                "include_patterns": ["*.java"],
            },
            _make_user(),
        )
    response = _parse_response(result)

    assert response["ok"] is True, f"expected success: {response}"
    degradation = response["degradation"]
    assert degradation["files_excluded_with_extractor"] == 0, degradation
    assert degradation["files_excluded_without_extractor"] == 1, degradation
    assert response["languages_excluded_with_extractor"] == []
    # Excluding README.md could never have contributed a real call edge --
    # completeness must NOT be downgraded on its account.
    assert response["fact_graph_complete"] is True


def test_collect_graph_candidate_files_classifies_excluded_files_by_extractor_availability(
    tmp_path: Path,
) -> None:
    """Unit-level proof of the classification itself, isolated from the
    full pipeline: an extractor-backed excluded file (`.kt`) is counted
    separately from a non-extractor-backed excluded file (`.md`), and the
    languages list names the excluded extractor-backed language.
    """
    repo_root = tmp_path / "repo"
    pkg = repo_root / "com" / "example" / "app"
    pkg.mkdir(parents=True)
    (pkg / "JavaUtil.java").write_text(JAVA_UTIL)
    (pkg / "KotlinCaller.kt").write_text(KOTLIN_CALLER)
    (repo_root / "README.md").write_text("# not source\n")

    (
        results,
        collection_truncated,
        files_excluded_with_extractor,
        files_excluded_without_extractor,
        languages_excluded_with_extractor,
    ) = _collect_graph_candidate_files(
        repo_root,
        include_patterns=["*.java"],
        exclude_patterns=[],
        max_files=1000,
        extractor_extensions={"java": "Java", "kt": "Kotlin", "kts": "Kotlin"},
    )

    assert results == ["com/example/app/JavaUtil.java"]
    assert collection_truncated is False
    assert files_excluded_with_extractor == 1
    assert files_excluded_without_extractor == 1
    assert languages_excluded_with_extractor == ["Kotlin"]


def test_collect_graph_candidate_files_dedupes_languages_across_multiple_excluded_files(
    tmp_path: Path,
) -> None:
    """Two excluded `.kt`/`.kts` files must report "Kotlin" ONCE in
    `languages_excluded_with_extractor`, not twice -- the caller needs a
    list of distinct unread languages, not a per-file log.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "A.java").write_text(JAVA_UTIL)
    (repo_root / "B.kt").write_text(KOTLIN_CALLER)
    (repo_root / "C.kts").write_text(KOTLIN_CALLER)

    (
        results,
        _truncated,
        files_excluded_with_extractor,
        files_excluded_without_extractor,
        languages_excluded_with_extractor,
    ) = _collect_graph_candidate_files(
        repo_root,
        include_patterns=["*.java"],
        exclude_patterns=[],
        max_files=1000,
        extractor_extensions={"java": "Java", "kt": "Kotlin", "kts": "Kotlin"},
    )

    assert results == ["A.java"]
    assert files_excluded_with_extractor == 2
    assert files_excluded_without_extractor == 0
    assert languages_excluded_with_extractor == ["Kotlin"]
