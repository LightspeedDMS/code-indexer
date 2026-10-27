"""Bug #2212: X-Ray front doors accept every extension the Rust engine supports.

Real handlers, real X-Ray engine, real xray-cli over a small repository with
one file per newly accepted extension. Only job infrastructure not under test
(BackgroundJobManager, JobTracker) is the existing recording-harness fake.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator
from unittest.mock import patch

import pytest

from tests.unit.server.mcp.xray_door_env_support import (
    REPO_ALIAS,
    DoorEnv,
    xray_door_env,
)
from tests.unit.server.services.search_event_recording_harness import (
    ALWAYS_MATCH_EVALUATOR,
    make_user,
)
from tests.unit.xray.new_language_fixtures_2212 import (
    MARKER,
    NEW_LANGUAGE_FILES,
    PYTHON_PARSEABLE,
    PYTHON_REFUSED,
    write_new_language_repo,
)

pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")

_AWAIT_SECONDS = 30


def _body(response: Dict[str, Any]) -> Dict[str, Any]:
    return dict(json.loads(response["content"][0]["text"]))


class TestDumpAstNewLanguages:
    def _dump(self, repo: Path, file_name: str) -> Dict[str, Any]:
        from code_indexer.server.mcp.handlers.xray import handle_xray_dump_ast

        # ^ why patch _resolve_repo_path, not a real registry; it is the golden-repo lookup, not the dump under test
        with patch(
            "code_indexer.server.mcp.handlers.xray._dump_ast._resolve_repo_path",
            return_value=str(repo),
        ):
            return _body(
                handle_xray_dump_ast(
                    {"repository_alias": REPO_ALIAS, "file_path": file_name},
                    make_user("alice"),
                )
            )

    @pytest.mark.parametrize("ext", sorted(PYTHON_PARSEABLE))
    def test_dump_ast_returns_tree_for_parseable_language(self, tmp_path, ext):
        repo = write_new_language_repo(tmp_path)
        name, _, label = NEW_LANGUAGE_FILES[ext]
        data = self._dump(repo, name)
        assert "error" not in data, data
        assert data["language"] == label
        assert data["ast_tree"]["type"]

    @pytest.mark.parametrize("ext", sorted(PYTHON_REFUSED))
    def test_dump_ast_refuses_language_explicitly(self, tmp_path, ext):
        repo = write_new_language_repo(tmp_path)
        name, _, label = NEW_LANGUAGE_FILES[ext]
        data = self._dump(repo, name)
        assert data.get("error") == "unsupported_language", data
        assert "AST dump not supported for this language" in data["message"]
        assert repr(label) in data["message"]
        assert "ast_tree" not in data


_EXPECTED = sorted((name, label) for name, _, label in NEW_LANGUAGE_FILES.values())


@pytest.fixture
def door_env(tmp_path) -> Iterator[DoorEnv]:
    from tests.unit.xray.conftest import require_xray_cli_binary

    require_xray_cli_binary()
    with xray_door_env(tmp_path, write_new_language_repo(tmp_path)) as env:
        yield env


def _assert_every_file_evaluated(result: Dict[str, Any]) -> None:
    unsupported = [
        e
        for e in result.get("evaluation_errors") or []
        if e.get("error_type") == "UnsupportedLanguage"
    ]
    assert unsupported == [], unsupported
    found = sorted((m["file_path"], m["language"]) for m in result["matches"])
    assert found == _EXPECTED, result


_SEARCH_ARGS = {
    "repository_alias": REPO_ALIAS,
    "pattern": MARKER,
    "evaluator_code": ALWAYS_MATCH_EVALUATOR,
    "search_target": "content",
    "await_seconds": _AWAIT_SECONDS,
}


class TestNewLanguageFrontDoors:
    def test_mcp_xray_search_evaluates_every_new_extension(self, door_env):
        from code_indexer.server.mcp.handlers.xray import handle_xray_search

        _assert_every_file_evaluated(
            door_env.mcp(handle_xray_search, _SEARCH_ARGS, "alice")
        )

    def test_rest_xray_search_evaluates_every_new_extension(self, door_env):
        result = door_env.rest_search(
            {
                "repository_alias": REPO_ALIAS,
                "driver_regex": MARKER,
                "evaluator_code": ALWAYS_MATCH_EVALUATOR,
                "search_target": "content",
            },
            "bob",
        )
        _assert_every_file_evaluated(result)

    def test_mcp_xray_explore_ast_debug_or_explicit_error(self, door_env):
        from code_indexer.server.mcp.handlers.xray import handle_xray_explore

        body = door_env.mcp(handle_xray_explore, _SEARCH_ARGS, "carol")
        _assert_every_file_evaluated(body)
        by_file = {m["file_path"]: m for m in body["matches"]}
        for ext in PYTHON_PARSEABLE:
            match = by_file[NEW_LANGUAGE_FILES[ext][0]]
            assert match["ast_debug"]["type"], match
            assert "ast_debug_error" not in match
        for ext in PYTHON_REFUSED:
            name, _, label = NEW_LANGUAGE_FILES[ext]
            match = by_file[name]
            assert "ast_debug" not in match, match
            assert (
                "AST dump not supported for this language" in match["ast_debug_error"]
            )
            assert repr(label) in match["ast_debug_error"]


_UPPER_CASE_FILES = {
    "Main.JAVA": (f"class Main {{ int {MARKER} = 1; }}\n", "java"),
    "b.PY": (f"{MARKER} = 2\n", "python"),
}


@pytest.fixture
def upper_env(tmp_path) -> Iterator[DoorEnv]:
    from tests.unit.xray.conftest import require_xray_cli_binary

    require_xray_cli_binary()
    repo = tmp_path / "repo"
    repo.mkdir()
    for name, (content, _) in _UPPER_CASE_FILES.items():
        (repo / name).write_text(content, encoding="utf-8")
    with xray_door_env(tmp_path, repo) as env:
        yield env


def _assert_upper_case_files_evaluated(result: Dict[str, Any]) -> None:
    refusals = [
        e
        for e in result.get("evaluation_errors") or []
        if e.get("error_type") in ("UnsupportedLanguage", "UnparsedFiles")
    ]
    assert refusals == [], refusals
    assert "unparsed_files" not in result, result
    found = sorted((m["file_path"], m["language"]) for m in result["matches"])
    assert found == sorted((n, lang) for n, (_, lang) in _UPPER_CASE_FILES.items())


class TestUpperCaseExtensionFrontDoors:
    def test_mcp_xray_search_evaluates_upper_case_extensions(self, upper_env):
        from code_indexer.server.mcp.handlers.xray import handle_xray_search

        _assert_upper_case_files_evaluated(
            upper_env.mcp(handle_xray_search, _SEARCH_ARGS, "alice")
        )

    def test_rest_xray_search_evaluates_upper_case_extensions(self, upper_env):
        result = upper_env.rest_search(
            {
                "repository_alias": REPO_ALIAS,
                "driver_regex": MARKER,
                "evaluator_code": ALWAYS_MATCH_EVALUATOR,
                "search_target": "content",
            },
            "bob",
        )
        _assert_upper_case_files_evaluated(result)
