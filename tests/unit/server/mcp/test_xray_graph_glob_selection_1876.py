"""Bug #1876: analyze_graph uses the shared gitwildmatch selector.

These are selection assertions rather than only a bounded-walk assertion:
the graph builder must receive exactly the files selected by the documented
glob policy.
"""

from pathlib import Path

from code_indexer.server.mcp.handlers.xray_graph import _collect_graph_candidate_files


def _build_java_tree(repo_root: Path) -> None:
    for relative_path in (
        "Root.java",
        "src/TopLevel.java",
        "src/nested/Deep.java",
        "src/test/java/MavenTest.java",
        "test/RootTest.java",
    ):
        path = repo_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("class Example {}\n")


def test_graph_selector_double_star_matches_root_level_java(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _build_java_tree(repo_root)

    paths, truncated, _with_ext, _without_ext, _langs = _collect_graph_candidate_files(
        repo_root,
        ["**/*.java"],
        [],
        max_files=100,
        extractor_extensions={"java": "Java"},
    )

    assert truncated is False
    assert "Root.java" in paths


def test_graph_selector_src_star_does_not_match_nested_java(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _build_java_tree(repo_root)

    paths, truncated, _with_ext, _without_ext, _langs = _collect_graph_candidate_files(
        repo_root,
        ["src/*.java"],
        [],
        max_files=100,
        extractor_extensions={"java": "Java"},
    )

    assert truncated is False
    assert paths == ["src/TopLevel.java"]


def test_graph_selector_test_exclude_covers_maven_test_tree(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _build_java_tree(repo_root)

    paths, truncated, _with_ext, _without_ext, _langs = _collect_graph_candidate_files(
        repo_root,
        ["**/*.java"],
        ["*/test/*"],
        max_files=100,
        extractor_extensions={"java": "Java"},
    )

    assert truncated is False
    assert paths == ["Root.java", "src/TopLevel.java", "src/nested/Deep.java"]
