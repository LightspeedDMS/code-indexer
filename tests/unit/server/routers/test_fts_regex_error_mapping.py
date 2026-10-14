"""Regression tests: FTS regex-compile failures map to HTTP 400, not 500.

Unsupported term-level regex constructs -- anchors (``^`` ``$``) and word
boundaries (``\\b``) -- are rejected by Tantivy's DFA/term-regex engine, which
surfaces as a ``ValueError("Invalid regex pattern ...")`` out of
``TantivyIndexManager.search()``. That is a client input error and must be
returned as HTTP 400 at the ``POST /api/query`` boundary -- previously it
escaped as a 500 ("FTS search failed: Invalid regex pattern ...").

These tests drive the real endpoint via a FastAPI ``TestClient`` and stub the
Tantivy manager so no real index is required. A companion slow test
(``test_fts_regex_anchor_rejected_by_engine``) proves the *source* ValueError
against a real Tantivy index.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies
from code_indexer.server.routers.inline_query import register_query_routes


def _build_client(monkeypatch, tmp_path: Path, search_side_effect):
    """Build a TestClient wired to reach the FTS search branch.

    ``search_side_effect`` is raised by the stubbed
    ``TantivyIndexManager.search`` so we can exercise the error mapping without
    a real index.
    """
    app = FastAPI()

    username = "tester"
    # Activated-repo manager: one non-global repo whose FTS index dir exists.
    activated_repos_dir = tmp_path / "activated-repos"
    repo_alias = "myrepo"
    fts_dir = (
        activated_repos_dir / username / repo_alias / ".code-indexer" / "tantivy_index"
    )
    fts_dir.mkdir(parents=True, exist_ok=True)

    activated_repo_manager = MagicMock()
    activated_repo_manager.list_activated_repositories.return_value = [
        {
            "user_alias": repo_alias,
            "username": username,
            "is_global": False,
            "repo_url": "",
        }
    ]
    activated_repo_manager.activated_repos_dir = str(activated_repos_dir)
    # The FTS-availability check resolves the repo through the containment
    # helper (get_activated_repo_path), not a hand-built join of
    # activated_repos_dir -- an unconfigured MagicMock return would read as
    # "FTS index not available".
    activated_repo_manager.get_activated_repo_path.side_effect = lambda user, alias: (
        str(activated_repos_dir / user / alias)
    )

    semantic_query_manager = MagicMock()

    # Stub TantivyIndexManager (imported lazily inside the handler) so .search
    # raises our controlled exception.
    class _StubManager:
        def __init__(self, *_args, **_kwargs):
            pass

        def open_for_search(self):
            return None

        def search(self, *_args, **_kwargs):
            raise search_side_effect

    monkeypatch.setattr(
        "code_indexer.services.tantivy_index_manager.TantivyIndexManager",
        _StubManager,
    )

    register_query_routes(
        app,
        semantic_query_manager=semantic_query_manager,
        activated_repo_manager=activated_repo_manager,
    )

    app.dependency_overrides[dependencies.get_current_user] = lambda: SimpleNamespace(
        username=username
    )

    return TestClient(app, raise_server_exceptions=False), repo_alias


def test_anchored_regex_pattern_maps_to_400_not_500(monkeypatch, tmp_path):
    """An anchored/unsupported regex -> HTTP 400 with an actionable message."""
    # This is exactly what TantivyIndexManager.search() raises for an anchored
    # pattern the term-regex engine rejects.
    err = ValueError(
        "Invalid regex pattern '^config': Regex parse error: unsupported anchor"
    )
    client, repo_alias = _build_client(monkeypatch, tmp_path, err)

    resp = client.post(
        "/api/query",
        json={
            "query_text": "^config",
            "repository_alias": repo_alias,
            "search_mode": "fts",
            "regex": True,
        },
    )

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    # Structured detail with an actionable suggestion about term-level regex.
    assert detail["error"] == "Invalid FTS query"
    assert "^config" in detail["message"]
    assert "word boundaries" in detail["suggestion"]
    assert "anchors" in detail["suggestion"]


def test_word_boundary_regex_pattern_maps_to_400(monkeypatch, tmp_path):
    """A ``\\b`` word-boundary pattern is also a 400, not a 500."""
    err = ValueError(
        r"Invalid regex pattern '\bfoo\b': Regex parse error: "
        "unsupported word boundary"
    )
    client, repo_alias = _build_client(monkeypatch, tmp_path, err)

    resp = client.post(
        "/api/query",
        json={
            "query_text": r"\bfoo\b",
            "repository_alias": repo_alias,
            "search_mode": "fts",
            "regex": True,
        },
    )

    assert resp.status_code == 400, resp.text


def test_genuine_server_error_still_maps_to_500(monkeypatch, tmp_path):
    """Non-ValueError faults (real server errors) must still be 500.

    Guards against over-broadening the 400 mapping to swallow genuine
    infrastructure failures.
    """
    err = RuntimeError("tantivy index file corrupted on disk")
    client, repo_alias = _build_client(monkeypatch, tmp_path, err)

    resp = client.post(
        "/api/query",
        json={
            "query_text": "config",
            "repository_alias": repo_alias,
            "search_mode": "fts",
            "regex": True,
        },
    )

    assert resp.status_code == 500, resp.text
    assert "FTS search failed" in resp.json()["detail"]


@pytest.mark.slow
def test_fts_regex_anchor_rejected_by_engine(tmp_path):
    """The SOURCE: a real Tantivy index rejects an anchored regex with ValueError.

    Confirms the term-regex engine genuinely refuses anchors, so the 400
    mapping above is triggered by a real condition, not a synthetic one.
    """
    from code_indexer.services.tantivy_index_manager import TantivyIndexManager

    manager = TantivyIndexManager(tmp_path / "tantivy_index")
    manager.initialize_index(create_new=True)
    manager.add_document(
        {
            "path": "src/config.py",
            "content": "class configuration:\n    pass",
            "content_raw": "class Configuration:\n    pass",
            "identifiers": ["Configuration"],
            "line_start": 1,
            "line_end": 2,
            "language": "python",
        }
    )
    manager.commit()

    with pytest.raises(ValueError, match="Invalid regex pattern"):
        manager.search(query_text="^config", use_regex=True, limit=10)


@pytest.mark.slow
def test_fts_regex_uppercase_matches_lowercased_index(tmp_path):
    """Case-insensitivity: an uppercase regex matches the lowercased index.

    Before the fix, an uppercase pattern like ``Config`` returned 0 results
    against the lowercased ``content`` field. With escape-aware lowercasing it
    now matches.
    """
    from code_indexer.services.tantivy_index_manager import TantivyIndexManager

    manager = TantivyIndexManager(tmp_path / "tantivy_index")
    manager.initialize_index(create_new=True)
    manager.add_document(
        {
            "path": "src/config.py",
            "content": "class configuration:\n    pass",
            "content_raw": "class Configuration:\n    pass",
            "identifiers": ["Configuration"],
            "line_start": 1,
            "line_end": 2,
            "language": "python",
        }
    )
    manager.commit()

    # Uppercase token pattern against the default (case-insensitive) field.
    results = manager.search(
        query_text="Config.*",
        use_regex=True,
        snippet_lines=0,
        limit=10,
    )
    assert len(results) > 0, "uppercase regex should match the lowercased index"
