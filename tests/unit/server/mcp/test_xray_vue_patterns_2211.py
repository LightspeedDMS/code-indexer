"""Bug #2211: stored `language: vue` patterns report findings on .vue files.

Each example pattern scans the lines of the root node's text. It is stored
with `language: vue` and run by `pattern_name` through the real MCP
xray_search handler and the real REST X-Ray search route, over a neutral
single-file component, with the real engine and xray-cli.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest
import yaml

from tests.unit.server.mcp.xray_door_env_support import (
    REPO_ALIAS,
    DoorEnv,
    xray_door_env,
)

pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")

_AWAIT_SECONDS = 30
_COMPONENT = "Widget.vue"
_LOOP_LINE = 4
_TIMER_LINE = 19
_FILLER_ROWS = 8
_MAX_LINES = 20

_LOOP_KEY = """fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let mut out = Vec::new();
    for (index, text) in node.text().lines().enumerate() {
        if text.contains("v-for") && !text.contains("key") {
            out.push(EvalFinding {
                pattern: "example-vue-loop-key".to_string(),
                line: index + 1,
                snippet: text.trim().to_string(),
            });
        }
    }
    out
}
"""

_TIMER_CLEANUP = """fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let source = node.text();
    if source.contains("clearInterval") {
        return Vec::new();
    }
    source
        .lines()
        .enumerate()
        .filter(|(_, text)| text.contains("setInterval"))
        .map(|(index, text)| EvalFinding {
            pattern: "example-vue-timer-cleanup".to_string(),
            line: index + 1,
            snippet: text.trim().to_string(),
        })
        .collect()
}
"""

_LARGE_FILE = """fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let count = node.text().lines().count();
    if count <= MAX_LINES {
        return Vec::new();
    }
    vec![EvalFinding {
        pattern: "example-vue-large-file".to_string(),
        line: 1,
        snippet: format!("{} lines", count),
    }]
}
"""

# name -> (evaluator code, parameters, expected line)
_PATTERNS: Dict[str, Any] = {
    "example-vue-loop-key": (_LOOP_KEY, [], _LOOP_LINE),
    "example-vue-timer-cleanup": (_TIMER_CLEANUP, [], _TIMER_LINE),
    "example-vue-large-file": (
        _LARGE_FILE,
        [{"name": "MAX_LINES", "type": "usize", "default": _MAX_LINES}],
        1,
    ),
}


def _component_source() -> str:
    filler = "".join(f"    <p>row {i}</p>\n" for i in range(_FILLER_ROWS))
    return (
        "<template>\n"
        '  <div class="widget">\n'
        "    <ul>\n"
        '      <li v-for="item in items">{{ item.label }}</li>\n'
        "    </ul>\n"
        f"{filler}"
        "  </div>\n"
        "</template>\n"
        "<script>\n"
        "export default {\n"
        "  mounted() {\n"
        "    setInterval(() => { this.ticks += 1 }, 1000)\n"
        "  }\n"
        "}\n"
        "</script>\n"
    )


def _store_patterns(cidx_meta: Path) -> None:
    scope = cidx_meta / "xray-patterns" / "__any__"
    scope.mkdir(parents=True)
    for name, (code, parameters, _) in _PATTERNS.items():
        spec: Dict[str, Any] = {
            "name": name,
            "description": f"{name} over Vue single-file components",
            "language": "vue",
            "evaluator_code": code,
        }
        if parameters:
            spec["parameters"] = parameters
        (scope / f"{name}.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))


@pytest.fixture
def vue_env(tmp_path) -> Iterator[DoorEnv]:
    from tests.unit.xray.conftest import require_xray_cli_binary

    require_xray_cli_binary()
    repo = tmp_path / "repo"
    repo.mkdir()
    source = _component_source()
    lines = source.splitlines()
    assert "v-for" in lines[_LOOP_LINE - 1]
    assert "setInterval" in lines[_TIMER_LINE - 1]
    assert len(lines) > _MAX_LINES
    (repo / _COMPONENT).write_text(source, encoding="utf-8")
    with xray_door_env(tmp_path, repo) as env:
        _store_patterns(env.cidx_meta)
        yield env


def _assert_pattern_finding(result: Dict[str, Any], name: str) -> None:
    refusals = [
        e
        for e in result.get("evaluation_errors") or []
        if e.get("error_type") in ("UnsupportedLanguage", "UnparsedFiles")
    ]
    assert refusals == [], refusals
    found: List[Any] = [
        (m["pattern"], m["file_path"], m["line_number"], m["language"])
        for m in result.get("matches") or []
    ]
    assert found == [(name, _COMPONENT, _PATTERNS[name][2], "vue")], result


@pytest.mark.parametrize("name", sorted(_PATTERNS))
def test_mcp_xray_search_vue_pattern_reports_finding(vue_env, name):
    from code_indexer.server.mcp.handlers.xray import handle_xray_search

    body = vue_env.mcp(
        handle_xray_search,
        {
            "repository_alias": REPO_ALIAS,
            "pattern": "<template",
            "pattern_name": name,
            "search_target": "content",
            "await_seconds": _AWAIT_SECONDS,
        },
        "alice",
    )
    _assert_pattern_finding(body, name)


@pytest.mark.parametrize("name", sorted(_PATTERNS))
def test_rest_xray_search_vue_pattern_reports_finding(vue_env, name):
    result = vue_env.rest_search(
        {
            "repository_alias": REPO_ALIAS,
            "driver_regex": "<template",
            "pattern_name": name,
            "search_target": "content",
        },
        "bob",
    )
    _assert_pattern_finding(result, name)
