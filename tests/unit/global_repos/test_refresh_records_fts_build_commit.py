"""The refresh path must record the commit its FTS build was made at.

`cidx index` here builds semantic AND FTS, so this is the second place an FTS
index is produced. Without the recording, every refreshed repo reads as stale to
the backfill sweep and is rebuilt once per refresh, forever.
"""

from __future__ import annotations

from pathlib import Path

from code_indexer.global_repos.refresh_scheduler import _record_fts_build_commit
from code_indexer.server.services.fts_index_builder import FTS_INDEX_DIRNAME
from code_indexer.server.services.index_freshness import (
    BUILD_COMMIT_MARKER,
    build_commit_is_current,
)

_SHA = "a" * 40


def _repo(tmp_path: Path, *, with_index: bool) -> Path:
    repo = tmp_path / "repo"
    git = repo / ".git" / "refs" / "heads"
    git.mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/master\n")
    (repo / ".git" / "refs" / "heads" / "master").write_text(_SHA + "\n")
    if with_index:
        (repo / ".code-indexer" / FTS_INDEX_DIRNAME).mkdir(parents=True)
    return repo


def test_records_the_commit_next_to_the_fts_index(tmp_path: Path) -> None:
    repo = _repo(tmp_path, with_index=True)
    _record_fts_build_commit(repo)

    marker = repo / ".code-indexer" / FTS_INDEX_DIRNAME / BUILD_COMMIT_MARKER
    assert marker.read_text().strip() == _SHA


def test_a_refreshed_repo_then_reads_as_current(tmp_path: Path) -> None:
    """The behaviour that matters: without this the sweep rebuilds the repo once
    per refresh, in perpetuity."""
    repo = _repo(tmp_path, with_index=True)
    idx = repo / ".code-indexer" / FTS_INDEX_DIRNAME
    assert build_commit_is_current(repo, idx) is False  # unrecorded -> stale
    _record_fts_build_commit(repo)
    assert build_commit_is_current(repo, idx) is True


def test_no_index_means_no_marker_and_no_phantom_dir(tmp_path: Path) -> None:
    """A refresh that produced no FTS index must not leave a directory holding
    only a marker."""
    repo = _repo(tmp_path, with_index=False)
    _record_fts_build_commit(repo)
    assert not (repo / ".code-indexer" / FTS_INDEX_DIRNAME).exists()


def test_never_raises_on_a_bad_path(tmp_path: Path) -> None:
    """Recording is best-effort: it must never fail an index that just succeeded."""
    _record_fts_build_commit(tmp_path / "does-not-exist")


def test_the_refresh_path_actually_calls_it() -> None:
    """Wiring guard: the tests above drive the helper directly, which proves the
    helper works and nothing about whether the refresh path uses it.

    Checked against the parsed call graph rather than by matching source text -- a
    substring assertion would also pass on a mention inside a comment or docstring,
    which is exactly how a 'covered' claim turns out to guard nothing.
    """
    import ast
    import inspect

    from code_indexer.global_repos import refresh_scheduler

    tree = ast.parse(inspect.getsource(refresh_scheduler))

    callers = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "_record_fts_build_commit"
    }
    # the definition itself must not be mistaken for a caller
    callers.discard("_record_fts_build_commit")

    assert callers, (
        "_record_fts_build_commit is defined but never called -- the refresh path "
        "would leave every refreshed repo reading as stale"
    )
