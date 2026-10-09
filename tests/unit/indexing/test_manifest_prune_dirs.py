"""A delegated manifest skips the directories discovery always discards.

workspace-mcp hashes every file it walks; a golden repo's own ``.code-indexer``
and ``node_modules`` are often most of the tree (one manifest of
agent-orchestrator: 57 s, 3.26 GB hashed, 512 tracked files). cidx now names
them in ``prune_dirs``. Pinned:

* the names come from discovery's OWN prune decision -- literal single-name
  excludes only, and none at all when force-include patterns exist;
* discovery from a pruned manifest finds EXACTLY the files an unpruned one does
  (so every point id is unchanged), and the pruned trees are never described;
* the list reaches the backend on every page, from both places that build a
  manifest (the indexing pass and the FTS rebuild), and a backend that predates
  the argument still works when there is nothing to prune;
* the local backend honours the argument identically, paging included.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from code_indexer.config import Config
from code_indexer.indexing.file_finder import FileFinder
from code_indexer.indexing.repo_manifest import RepoManifest
from code_indexer.server.storage.shared.content_backend import LocalContentBackend

FILES = {
    "src/app.py": "x = 1\n",
    "src/util/helpers.py": "y = 2\n",
    "node_modules/dep/index.py": "dep = 1\n",
    "pkg/node_modules/inner/mod.py": "inner = 1\n",
    "pkg/core.py": "core = 1\n",
    ".code-indexer/index/stray.py": "index = 1\n",
    "dist/bundle.py": "built = 1\n",
    "dist-old/legacy.py": "legacy = 1\n",
    "node_modules2/kept.py": "kept = 1\n",
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for rel, text in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def _config(root: Path, **overrides: Any) -> Config:
    return Config(codebase_dir=root, file_extensions=["py"], **overrides)


class _Recording(LocalContentBackend):
    """A delegated-shaped backend that records what it was asked to prune."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.described: List[str] = []

    @property
    def is_local(self) -> bool:
        return False

    def manifest(self, repo_path: Any, **kw: Any) -> Any:
        self.calls.append(kw)
        page = super().manifest(repo_path, **kw)
        self.described.extend(e.path for e in page.entries)
        return page


class _Predates:
    """A backend whose manifest() has no prune_dirs parameter at all."""

    is_local = False

    def manifest(self, repo_path: Any, *, cursor: Any = None, page_size: int = 1000):
        return LocalContentBackend().manifest(
            repo_path, cursor=cursor, page_size=page_size
        )


# ── which names ─────────────────────────────────────────────────────────────


def test_the_default_excludes_are_pruned_but_not_a_glob(repo: Path) -> None:
    names = FileFinder(_config(repo)).manifest_prune_dirs()
    assert {"node_modules", ".code-indexer", "dist"} <= set(names)
    assert "dist-*" not in names, "a glob cannot be pruned by name"


def test_a_multi_component_exclude_is_not_a_name(repo: Path) -> None:
    names = FileFinder(
        _config(repo, exclude_dirs=["help/Source", "vendor"])
    ).manifest_prune_dirs()
    assert names == ["vendor"]


def test_force_include_patterns_turn_pruning_off(repo: Path) -> None:
    """One can rescue a directory by an ancestor's name; a name prune cannot."""
    from code_indexer.config import OverrideConfig

    override = OverrideConfig(force_include_patterns=["pkg/**/*.py"])
    assert (
        FileFinder(_config(repo, override_config=override)).manifest_prune_dirs() == []
    )


# ── identity: the same file set, without reading the pruned trees ───────────


def test_discovery_from_a_pruned_manifest_is_unchanged(repo: Path) -> None:
    def discovered(prune: List[str]) -> List[str]:
        finder = FileFinder(_config(repo))
        backend = _Recording()
        finder.set_manifest(
            RepoManifest.build(repo, backend=backend, prune_dirs=prune), backend
        )
        return sorted(p.relative_to(repo).as_posix() for p in finder.find_files())

    prune = FileFinder(_config(repo)).manifest_prune_dirs()
    assert discovered(prune) == discovered([])
    assert "node_modules2/kept.py" in discovered(prune)


def test_the_pruned_trees_are_never_described(repo: Path) -> None:
    backend = _Recording()
    RepoManifest.build(
        repo,
        backend=backend,
        prune_dirs=FileFinder(_config(repo)).manifest_prune_dirs(),
    )
    for rel in backend.described:
        parts = rel.split("/")
        assert not {"node_modules", ".code-indexer", "dist"} & set(parts[:-1]), rel


# ── the list reaches the backend ────────────────────────────────────────────


def test_every_page_carries_the_list(repo: Path) -> None:
    backend = _Recording()
    RepoManifest.build(repo, backend=backend, page_size=2, prune_dirs=["node_modules"])
    assert len(backend.calls) > 1
    assert all(c.get("prune_dirs") == ["node_modules"] for c in backend.calls)


def test_a_backend_predating_the_argument_still_works_unpruned(repo: Path) -> None:
    """A plugin older than 0.3.0: no TypeError, just today's full manifest."""
    manifest = RepoManifest.build(
        repo, backend=_Predates(), prune_dirs=["node_modules"]
    )
    assert "src/app.py" in manifest.paths()
    assert "node_modules/dep/index.py" in manifest.paths()


def test_the_indexing_pass_sends_discoverys_list(repo: Path) -> None:
    from unittest.mock import MagicMock

    from code_indexer.services.git_aware_processor import GitAwareDocumentProcessor

    backend = _Recording()
    processor = GitAwareDocumentProcessor(
        _config(repo), MagicMock(), MagicMock(), content_backend=backend
    )
    processor.begin_manifest_pass()
    try:
        assert (
            backend.calls[0]["prune_dirs"]
            == processor.file_finder.manifest_prune_dirs()
        )
        assert "node_modules" in backend.calls[0]["prune_dirs"]
    finally:
        processor.end_manifest_pass()


def test_the_fts_rebuild_sends_the_list(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer import cli

    backend = _Recording()
    monkeypatch.setattr(cli, "_build_index_content_backend", lambda name: backend)

    cli._delegated_fts_source("delegated", repo, prune_dirs=["node_modules"])

    assert backend.calls and backend.calls[0]["prune_dirs"] == ["node_modules"]


def test_the_fts_command_passes_discoverys_list() -> None:
    """Source-level: the index command's FTS rebuild hands its finder's list on."""
    import ast
    import inspect

    from code_indexer import cli

    tree = ast.parse(inspect.getsource(cli))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", None) == "_delegated_fts_source"
    ]
    assert calls
    for call in calls:
        prune = {kw.arg: kw.value for kw in call.keywords}.get("prune_dirs")
        assert (
            prune is not None
            and getattr(prune.func, "attr", None) == "manifest_prune_dirs"
        )


# ── the local backend honours it identically ────────────────────────────────


def test_the_local_backend_pages_a_pruned_walk_exactly_once_in_order(
    repo: Path,
) -> None:
    pages: List[str] = []
    cursor = None
    while True:
        page = LocalContentBackend().manifest(
            repo, cursor=cursor, page_size=2, prune_dirs=["node_modules", "dist"]
        )
        pages.extend(e.path for e in page.entries)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    expected = sorted(
        p for p in FILES if not {"node_modules", "dist"} & set(p.split("/")[:-1])
    )
    assert pages == expected


def test_a_name_discovery_would_keep_somewhere_is_never_sent(repo: Path) -> None:
    """The names come from discovery's own decision, not a parallel rule: if it
    would keep ``vendor`` one level down, the backend must not prune it."""

    class _KeepsNestedVendor(FileFinder):
        def _should_prune_dir(self, dir_str: str) -> bool:
            if dir_str.endswith("/vendor"):
                return False
            return super()._should_prune_dir(dir_str)

    names = _KeepsNestedVendor(
        _config(repo, exclude_dirs=["vendor", "out"])
    ).manifest_prune_dirs()
    assert names == ["out"]
