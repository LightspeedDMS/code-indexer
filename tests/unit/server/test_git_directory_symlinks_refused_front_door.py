# ruff: noqa: F811
"""A file whose RESOLVED location is inside the repository's ``.git`` is never
read, served, indexed or returned, whatever its own name: committed symlinks
such as ``link.py -> .git/config`` or ``gitdir -> .git`` are answered exactly
like missing files. Legitimate in-repository symlinks keep working.

Driven through a real app (repo_url_userinfo_env): the golden clone and the
user's activation carry the committed symlinks and a real ``origin`` whose
URL holds the secret in ``.git/config``.

Hosts, usernames and secrets are neutral placeholders.
"""

from __future__ import annotations

import json
import os
import pwd
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest
from fastapi.testclient import TestClient

from tests.unit.server.repo_url_userinfo_env import (  # noqa: F401 - fixtures
    GIT_DIR_LINK,
    GIT_FILE_LINKS,
    GLOBAL_ALIAS,
    OK_LINK,
    OK_PY_LINK,
    REPO,
    SECRET,
    USER,
    USER_ACTIVATION,
    activate_for_user,
    app,
    assert_no_userinfo,
    client,
    get,
    golden_clone_path,
    store_userinfo_origin,
    mcp_call,
)

_ALWAYS_MATCH_EVALUATOR = (
    "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n"
    '    vec![EvalFinding { pattern: "any".to_string(), line: node.start_line,'
    " snippet: String::new() }]\n"
    "}\n"
)


@pytest.fixture
def activation(app: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    path = activate_for_user(app, monkeypatch)
    # Activation stores no credential; reproduce a clone that predates
    # run-time credentials so the .git denial is tested against a real one.
    store_userinfo_origin(path)
    assert (path / "link.py").is_symlink()
    assert SECRET in (path / "link.py").read_text()
    return USER_ACTIVATION


def _masked(text: str, requested: str) -> str:
    return text.replace(requested, "<path>")


# --------------------------------------------------------- file read (guard)


@pytest.mark.parametrize("name", [*GIT_FILE_LINKS, f"{GIT_DIR_LINK}/config"])
def test_get_file_content_refuses_git_symlinks_like_missing_files(
    client: TestClient, app: Any, activation: str, name: str
) -> None:
    for repo in (GLOBAL_ALIAS, activation):
        refused = json.dumps(
            mcp_call(
                client,
                app,
                USER,
                "get_file_content",
                {"repository_alias": repo, "file_path": name},
            )
        )
        missing = json.dumps(
            mcp_call(
                client,
                app,
                USER,
                "get_file_content",
                {"repository_alias": repo, "file_path": "missing.py"},
            )
        )
        assert_no_userinfo(refused)
        assert _masked(refused, name) == _masked(missing, "missing.py")


# ---------------------------------------------------------------------- X-Ray


@pytest.fixture
def rust_toolchain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The evaluator compile needs the real Rust toolchain (the isolated
    app points HOME elsewhere); X-Ray's own cache goes to tmp_path."""
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    monkeypatch.setenv("CARGO_HOME", str(real_home / ".cargo"))
    monkeypatch.setenv("RUSTUP_HOME", str(real_home / ".rustup"))
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path / "xray-data"))


def _xray(
    client: TestClient,
    app: Any,
    tool: str,
    mode: str,
    pattern: str,
    await_seconds: int,
) -> Dict[str, Any]:
    body = mcp_call(
        client,
        app,
        USER,
        tool,
        {
            # X-Ray resolves golden/global repositories (not activations).
            "repository_alias": GLOBAL_ALIAS,
            "pattern": pattern,
            "evaluator_code": _ALWAYS_MATCH_EVALUATOR,
            "search_target": mode,
            "await_seconds": await_seconds,
        },
    )
    assert_no_userinfo(json.dumps(body))
    payload: Dict[str, Any] = json.loads(body["result"]["content"][0]["text"])
    return payload


def _stored_job_bodies(client: TestClient, app: Any, job_id: str) -> List[str]:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        detail = get(client, app, USER, f"/api/jobs/{job_id}")
        if detail.status_code == 200 and detail.json().get("status") in (
            "completed",
            "failed",
            "cancelled",
        ):
            listing = get(client, app, USER, "/api/jobs?limit=100")
            return [detail.text, listing.text]
        time.sleep(0.2)
    raise AssertionError(f"X-Ray job {job_id} did not finish within 60s")


@pytest.fixture
def persistent_loop(client: TestClient) -> Iterator[None]:
    """One long-lived event loop for every request of the test, as in a
    running server (a queued X-Ray job completes on the request's loop)."""
    import anyio.from_thread

    with anyio.from_thread.start_blocking_portal() as portal:
        client.portal = portal
        try:
            yield
        finally:
            client.portal = None


@pytest.mark.parametrize("tool", ["xray_search", "xray_explore"])
@pytest.mark.parametrize(
    "mode,pattern", [("filename", "link|ok"), ("content", "example|url")]
)
def test_xray_never_reads_git_resolving_symlinks(
    client: TestClient,
    app: Any,
    activation: str,
    rust_toolchain: None,
    persistent_loop: None,
    tool: str,
    mode: str,
    pattern: str,
) -> None:
    inline = _xray(client, app, tool, mode, pattern, await_seconds=30)
    assert "error" not in inline, inline
    queued = _xray(client, app, tool, mode, pattern, await_seconds=0)
    assert queued.get("job_id"), queued
    bodies = [json.dumps(inline), json.dumps(queued)]
    bodies += _stored_job_bodies(client, app, queued["job_id"])
    for body in bodies:
        assert_no_userinfo(body)
        for name in GIT_FILE_LINKS:
            assert f'"{name}"' not in body, body
    if mode == "filename":
        matched = {m["file_path"] for m in inline["matches"]}
        assert OK_PY_LINK in matched, inline


def test_graph_candidates_never_include_git_resolving_symlinks(app: Any) -> None:
    """analyze_graph's candidate walk (the files handed to the graph
    extractor) skips files resolving into .git, keeps legitimate links."""
    from code_indexer.server.mcp.handlers.xray_graph import (
        _collect_graph_candidate_files,
    )

    files, *_ = _collect_graph_candidate_files(
        golden_clone_path(app),
        [],
        [],
        max_files=1000,
        extractor_extensions={"py": "python", "md": "markdown"},
    )
    assert not set(files) & set(GIT_FILE_LINKS), files
    assert {OK_PY_LINK, "main.py"} <= set(files), files


# ----------------------------------------------------------------------- Wiki


@pytest.fixture
def wiki(app: Any, activation: str) -> Dict[str, str]:
    """Wiki enabled on the golden repository and on the user's activation;
    returns the two URL prefixes."""
    from code_indexer.server.wiki import routes as wiki_routes

    app.state.golden_repo_manager.set_wiki_enabled(REPO, True)
    app.state.activated_repo_manager.set_wiki_enabled(USER, activation, True)
    wiki_routes._reset_wiki_cache()
    return {"golden": f"/wiki/{REPO}", "user": f"/wiki/u/{USER}/{activation}"}


WIKI_GIT_PATHS = [
    "link.md",
    "link.txt",
    "linknoext",
    "link",
    f"{GIT_DIR_LINK}/config",
    "_assets/link.md",
    "_assets/link.txt",
]


@pytest.mark.parametrize("prefix_key", ["golden", "user"])
@pytest.mark.parametrize("path", WIKI_GIT_PATHS)
def test_wiki_answers_git_symlinks_like_missing_pages(
    client: TestClient, app: Any, wiki: Dict[str, str], prefix_key: str, path: str
) -> None:
    prefix = wiki[prefix_key]
    refused = get(client, app, USER, f"{prefix}/{path}")
    missing = get(client, app, USER, f"{prefix}/missing-page.md")
    assert_no_userinfo(refused.text)
    assert (refused.status_code, refused.text) == (
        missing.status_code,
        missing.text,
    )


@pytest.mark.parametrize("prefix_key", ["golden", "user"])
def test_wiki_index_and_legit_symlink(
    client: TestClient, app: Any, wiki: Dict[str, str], prefix_key: str
) -> None:
    prefix = wiki[prefix_key]
    index = get(client, app, USER, f"{prefix}/")
    assert_no_userinfo(index.text)
    assert f'{prefix}/ok"' in index.text, index.text
    assert f'{prefix}/link"' not in index.text, index.text
    ok = get(client, app, USER, f"{prefix}/{OK_LINK}")
    assert ok.status_code == 200, ok.text
    assert "example" in ok.text
    assert_no_userinfo(ok.text)
    # The article's sidebar lists no .git-resolving article either.
    assert f'{prefix}/link"' not in ok.text, ok.text


def test_wiki_never_serves_a_page_cached_before_the_rule(
    client: TestClient, app: Any, wiki: Dict[str, str]
) -> None:
    """A page cached for a .git-resolving path (e.g. before this rule
    existed) is never served."""
    from code_indexer.server.wiki.wiki_cache import WikiCache

    cache = WikiCache(app.state.golden_repo_manager.db_path)
    cache.ensure_tables()
    link = golden_clone_path(app) / "link.md"
    cache.put_article(REPO, "link", f"<p>{SECRET}</p>", "link", link)
    try:
        refused = get(client, app, USER, f"{wiki['golden']}/link.md")
        assert refused.status_code == 404, refused.text
        assert_no_userinfo(refused.text)
    finally:
        import sqlite3

        with sqlite3.connect(app.state.golden_repo_manager.db_path) as conn:
            conn.execute(
                "DELETE FROM wiki_cache WHERE repo_alias = ? AND article_path = ?",
                (REPO, "link"),
            )


def test_wiki_cache_keeps_nothing_from_git(
    client: TestClient, app: Any, wiki: Dict[str, str]
) -> None:
    import sqlite3

    for prefix in wiki.values():
        for path in ["", *WIKI_GIT_PATHS, OK_LINK]:
            get(client, app, USER, f"{prefix}/{path}")
    with sqlite3.connect(app.state.golden_repo_manager.db_path) as conn:
        rows = conn.execute(
            "SELECT rendered_html, title FROM wiki_cache"
            " UNION ALL SELECT sidebar_json, '' FROM wiki_sidebar_cache"
        ).fetchall()
    assert not [r for r in rows if SECRET in (r[0] or "")], rows


# ------------------------------------------------------------------- Indexing


def _server_config(repo: Path) -> Any:
    from code_indexer.config import Config
    from code_indexer.server.utils.server_managed_provider_settings import (
        enforce_server_managed_provider_settings,
    )

    config = Config(codebase_dir=repo)
    enforce_server_managed_provider_settings(config)
    return config


def test_server_indexing_never_reads_git_resolving_symlinks(app: Any) -> None:
    from code_indexer.indexing.file_finder import FileFinder
    from code_indexer.indexing.fixed_size_chunker import FixedSizeChunker
    from code_indexer.indexing.processor import DocumentProcessor

    repo = golden_clone_path(app)
    config = _server_config(repo)
    found = {p.name for p in FileFinder(config).find_files()}
    assert not found & set(GIT_FILE_LINKS), found
    assert not [n for n in found if n in ("config", "HEAD")], found
    assert OK_LINK in found and "README.md" in found

    processor = DocumentProcessor(
        config,
        embedding_provider=None,  # type: ignore[arg-type]
        vector_store_client=None,  # type: ignore[arg-type]
    )
    candidates = [repo / name for name in (*GIT_FILE_LINKS, OK_LINK)]
    kept = processor._filter_paths_within_codebase_root(candidates)
    assert kept == [repo / OK_LINK]

    chunker = FixedSizeChunker(config)
    with pytest.raises(ValueError):
        chunker.chunk_file(repo / "link.py", repo_root=repo)
    assert chunker.chunk_file(repo / OK_LINK, repo_root=repo)
