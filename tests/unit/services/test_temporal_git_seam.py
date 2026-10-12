"""Temporal indexing and queries run through the git seam on a pod without the repo.

Temporal indexing walks a repository's history (``git log``, one ``git diff``
per commit); temporal queries resolve ``at_commit`` and read pointer payloads'
files at a commit. On a pod that does not hold the repository none of that can
run locally, so it used to be refused. With a git backend (git-mcp) it is asked
of the backend -- in pages and batches -- and must produce what a local run
produces.

Pinned against a real repository held ELSEWHERE (git-mcp's volume), answered by
``LocalGitBackend`` run there, while the indexed path on this pod has no
``.git`` -- or does not exist at all:

* golden identity: the commit-document set (point ids, payloads, chunk texts)
  of a delegated run equals the local run's -- adds, modifies, deletes, pure
  and changed renames, binary files, CRLF and invalid UTF-8, a merge;
* call shape: history and diffs are ``ceil(N / page)`` calls across a page
  boundary, never one per commit;
* no mount: the indexed path does not exist on disk;
* queries: ``at_commit`` and pointer content answer identically;
* local unchanged: with no backend the same local git commands run as before.
"""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from code_indexer.config import Config, TemporalConfig
from code_indexer.server.storage.shared.git_backend import LocalGitBackend
from code_indexer.services.temporal import commit_aggregator
from code_indexer.services.temporal import temporal_git_seam as seam
from code_indexer.services.temporal import temporal_indexer as indexer_module
from code_indexer.services.temporal import temporal_search_service as tss
from code_indexer.services.temporal.embedders.base import TemporalEmbedder
from code_indexer.services.temporal.embedders.registry import (
    register_embedder,
    unregister_embedder_for_tests,
)
from code_indexer.services.temporal.temporal_indexer import TemporalIndexer
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from tests.unit.services.test_reconcile_git_seam_mountless import (
    _Elsewhere,
    _git,
    _git_mcp,
)

EMBEDDER = "fake-temporal-seam"
# Not the directory name: the project id (in every point id) comes from it.
ORIGIN = "https://git.example/group/router-service.git"
COMMITS = 9  # in _history


class _Subprocess:
    """``subprocess`` for ONE module under test, with its own ``run``.

    Patching ``module.subprocess.run`` would patch the shared ``subprocess``
    module -- every git call in the process, the test's own included.
    """

    def __init__(self, run: Any) -> None:
        self.run = run

    def __getattr__(self, name: str) -> Any:
        return getattr(subprocess, name)


# ── the repository, held elsewhere ──────────────────────────────────────────


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def _history(repo: Path) -> None:
    """Adds, modifies, deletes, renames (pure and changed), binary, a merge."""
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "remote", "add", "origin", ORIGIN)
    (repo / "src").mkdir()
    (repo / "src/a.py").write_text("def a():\n    return 1\n")
    (repo / "src/b.py").write_text("def b():\n    return 2\n")
    (repo / "README.md").write_text("# agent router\n")
    (repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff\xfe")
    _commit(repo, "Initial | import\n\nWith a body | and pipes.")
    (repo / "src/a.py").write_bytes(b"def a():\r\n    return 10\r\n")
    (repo / "src/b.py").write_bytes(b"def b():\n    return '\xe9\xff'\n")
    _commit(repo, "CRLF and invalid UTF-8")
    (repo / "README.md").unlink()
    _commit(repo, "Delete the readme")
    _git(repo, "mv", "src/b.py", "src/c.py")
    _commit(repo, "Pure rename")
    _git(repo, "mv", "src/a.py", "src/d.py")
    with open(repo / "src/d.py", "ab") as f:
        f.write(b"\r\ndef d():\r\n    return 4\r\n")
    _commit(repo, "Rename with a change")
    (repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x01\x02")
    _commit(repo, "Binary change")
    _git(repo, "checkout", "-qb", "feature")
    (repo / "src/feature.py").write_text("FEATURE = True\n")
    _commit(repo, "Feature work")
    _git(repo, "checkout", "-q", "master")
    (repo / "src/c.py").write_text("def c():\n    return 3\n")
    _commit(repo, "Master work")
    _git(repo, "merge", "-q", "--no-ff", "feature", "-m", "Merge feature")


def _linear(repo: Path, commits: int) -> None:
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "remote", "add", "origin", ORIGIN)
    for i in range(commits):
        (repo / f"f{i}.py").write_text(f"VALUE = {i}\n")
        _commit(repo, f"commit {i}")


@pytest.fixture
def held(tmp_path: Path) -> Path:
    repo = tmp_path / "git-mcp" / "agent-router"
    repo.mkdir(parents=True)
    _history(repo)
    return repo


# ── a deterministic temporal embedder, no network ───────────────────────────


class _Embedder(TemporalEmbedder):
    name = EMBEDDER
    model_slug = "fake_temporal_seam"
    dimensions = 4
    overlap_percentage = 0.0

    def embed_commit_chunks(self, chunks: List[str]) -> List[List[float]]:
        return [
            [float(len(c)), float(sum(map(ord, c)) % 997), 1.0, 0.0] for c in chunks
        ]

    def embed_query(self, text: str) -> List[float]:
        return [float(len(text)), 0.0, 1.0, 0.0]


@pytest.fixture(autouse=True)
def embedder(monkeypatch: pytest.MonkeyPatch) -> Any:
    # The regular provider is only constructed for its metadata; never called.
    monkeypatch.setenv("VOYAGE_API_KEY", "not-used")
    register_embedder(EMBEDDER, lambda config: _Embedder())
    yield
    unregister_embedder_for_tests(EMBEDDER)


def _config_manager(codebase: Path, config_dir: Path) -> MagicMock:
    config = Config(codebase_dir=codebase, embedding_provider="voyage-ai")
    config.temporal = TemporalConfig(embedders=[EMBEDDER], active_embedder=EMBEDDER)
    manager = MagicMock()
    manager.load.return_value = config
    manager.get_config.return_value = config
    manager.config_path = config_dir / ".code-indexer" / "config.json"
    manager.config_path.parent.mkdir(parents=True, exist_ok=True)
    return manager


# ── what an index holds ─────────────────────────────────────────────────────


def _documents(index_dir: Path) -> Dict[str, str]:
    """Every temporal point, whole: id -> its stored record (payload, vector,
    chunk text), canonical JSON."""
    store = FilesystemVectorStore(base_path=index_dir, project_root=index_dir)
    documents: Dict[str, str] = {}
    for shard in sorted(p.name for p in index_dir.iterdir() if p.is_dir()):
        if not store.collection_exists(shard):
            continue
        points, _ = store.scroll_points(collection_name=shard, limit=100_000)
        for point in points:
            record = store.get_point(point["id"], shard)
            assert record is not None and record.get("chunk_text"), point["id"]
            documents[point["id"]] = json.dumps(record, sort_keys=True, default=str)
    return documents


def _index(codebase: Path, index_dir: Path, git_backend: Any = None) -> Any:
    store = FilesystemVectorStore(base_path=index_dir, project_root=codebase)
    indexer = TemporalIndexer(
        _config_manager(codebase, index_dir.parent),
        store,
        collection_name="code-indexer-temporal-seam",
        git_backend=git_backend,
    )
    result = indexer.index_commits()
    indexer.close()
    return result


# ── golden identity, end to end through the CLI ─────────────────────────────


def _cli_index(codebase: Path, monkeypatch: pytest.MonkeyPatch, *flags: str) -> Any:
    """``cidx index --index-commits`` run in *codebase*, a local-mode project."""
    from code_indexer.cli import index as index_command

    manager = _config_manager(codebase, codebase)
    manager.config_path.write_text("{}")
    monkeypatch.chdir(codebase)
    return CliRunner().invoke(
        index_command,
        ["--index-commits", *flags],
        obj={"config_manager": manager},
        catch_exceptions=False,
    )


_SEAMS = ("--content-backend=delegated", "--git-backend=delegated")


def test_delegated_history_indexes_the_commit_documents_a_local_run_does(
    held: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `cidx index --index-commits` a delegated refresh spawns.

    Refused before this change (``--content-backend/--git-backend delegated``):
    the history was "not on this machine". It is -- with git-mcp.
    """
    from code_indexer import cli as cli_module

    here = tmp_path / "golden-repos" / "agent-router"
    here.mkdir(parents=True)
    elsewhere = _Elsewhere(here, held)
    monkeypatch.setattr(
        cli_module, "_build_index_git_backend", lambda name: _git_mcp(elsewhere)
    )
    monkeypatch.setattr(
        cli_module,
        "_build_index_content_backend",
        lambda name: pytest.fail("temporal indexing reads no repository files"),
    )

    local = _cli_index(held, monkeypatch)
    delegated = _cli_index(here, monkeypatch, *_SEAMS)

    assert local.exit_code == 0, local.output
    assert delegated.exit_code == 0, delegated.output
    assert not (here / ".git").exists() and not (here / "src").exists()
    expected = _documents(held / ".code-indexer" / "index")
    assert len({pid.split(":")[2] for pid in expected}) == COMMITS, "every commit"
    assert _documents(here / ".code-indexer" / "index") == expected


def test_delegated_content_with_local_git_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No git seam, no repository here: nothing can read the history."""
    here = tmp_path / "golden-repos" / "agent-router"
    here.mkdir(parents=True)
    result = _cli_index(here, monkeypatch, "--content-backend=delegated")
    assert result.exit_code == 1, result.output
    assert "needs --git-backend delegated" in " ".join(result.output.split())


# ── no mount ────────────────────────────────────────────────────────────────


def test_indexing_through_the_seam_needs_no_repository_on_disk(
    held: Path, tmp_path: Path
) -> None:
    """The indexed path does not exist: every local read would fail naturally."""
    here = tmp_path / "pod" / "agent-router"
    _index(held, tmp_path / "local-index")

    result = _index(here, tmp_path / "pod-index", _git_mcp(_Elsewhere(here, held)))

    assert not here.exists()
    assert result.total_commits == COMMITS
    assert result.branches_indexed == ["master"]
    assert _documents(tmp_path / "pod-index") == _documents(tmp_path / "local-index")


# ── call shape ──────────────────────────────────────────────────────────────


class _Recording:
    """A delegated backend that records every call git-mcp would receive."""

    def __init__(self, backend: Any) -> None:
        self._backend = backend
        self.calls: List[Tuple[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._backend, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args[1] if len(args) > 1 else None))
            return target(*args, **kwargs)

        return call

    def count(self, name: str) -> int:
        return sum(1 for called, _ in self.calls if called == name)


HISTORY_PAGE = 3
DIFF_PAGE = 2


@pytest.mark.parametrize("commits", [5, 6, 7])
def test_history_and_diffs_are_paged_never_per_commit(
    commits: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Across the page boundary: ``ceil(N / page)`` calls, nothing per commit."""
    monkeypatch.setattr(seam, "HISTORY_PAGE_COMMITS", HISTORY_PAGE)
    monkeypatch.setattr(seam, "DIFF_PAGE_COMMITS", DIFF_PAGE)
    repo = tmp_path / "git-mcp" / "agent-router"
    repo.mkdir(parents=True)
    _linear(repo, commits)
    here = tmp_path / "pod" / "agent-router"
    backend = _Recording(_git_mcp(_Elsewhere(here, repo)))

    result = _index(here, tmp_path / "index", backend)

    assert result.total_commits == commits
    assert backend.count("log_raw") == math.ceil(commits / HISTORY_PAGE)
    assert backend.count("diff_commits") == math.ceil(commits / DIFF_PAGE)
    assert all(
        len(pairs) <= DIFF_PAGE
        for name, pairs in backend.calls
        if name == "diff_commits"
    )
    per_run = {"rev_parse": 2, "symbolic_ref": 1, "remote_get_url": 1}
    for name, count in per_run.items():
        assert backend.count(name) <= count, (name, backend.calls)
    assert {name for name, _ in backend.calls} <= {
        "log_raw",
        "diff_commits",
        *per_run,
    }, backend.calls


def test_history_respects_max_commits_and_since(held: Path) -> None:
    """The newest N, oldest first -- as ``git log --reverse -n N`` lists them."""
    local = TemporalIndexer.__new__(TemporalIndexer)
    local.codebase_dir = held
    delegated = TemporalIndexer.__new__(TemporalIndexer)
    delegated.codebase_dir = held
    delegated._git_backend = LocalGitBackend()

    for args in [(False, 4, None), (True, None, None), (True, 3, "2000-01-01")]:
        assert delegated._get_commit_history(*args) == local._get_commit_history(
            *args
        ), args


# ── local unchanged ─────────────────────────────────────────────────────────


def test_without_a_backend_the_same_local_git_commands_run(
    held: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No backend: exactly the argv local indexing ran before the seam."""
    argvs: List[List[str]] = []
    real_run = subprocess.run

    def spy(cmd: Sequence[str], *args: Any, **kwargs: Any) -> Any:
        argvs.append(list(cmd))
        assert kwargs.get("text") and kwargs.get("errors") == "replace", kwargs
        return real_run(cmd, *args, **kwargs)

    def no_seam(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the git seam was used without a backend")

    monkeypatch.setattr(indexer_module, "subprocess", _Subprocess(spy))
    monkeypatch.setattr(commit_aggregator, "subprocess", _Subprocess(spy))
    monkeypatch.setattr(indexer_module, "commit_history", no_seam)
    monkeypatch.setattr(indexer_module, "CommitDiffPages", no_seam)
    monkeypatch.setattr(indexer_module, "current_branch_name", no_seam)

    _index(held, tmp_path / "index")

    commits = [
        line.split()
        for line in _git(held, "log", "--reverse", "--format=%H %P").splitlines()
    ]
    expected = [
        [
            "git",
            "log",
            "--format=%H%x00%at%x00%an%x00%ae%x00%B%x00%P%x1e",
            "--reverse",
        ],
        *[
            [
                "git",
                "diff",
                "-M",
                "-U5",
                "--full-index",
                parents[0] if parents else commit_aggregator.EMPTY_TREE_SHA,
                sha,
            ]
            for sha, *parents in commits
        ],
        ["git", "branch", "--show-current"],
    ]
    assert sorted(argvs) == sorted(expected)
    assert argvs[0] == expected[0] and argvs[-1] == expected[-1]


# ── temporal queries ────────────────────────────────────────────────────────


@pytest.fixture
def server_seam(
    held: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> _Recording:
    """A server whose git is delegated; the snapshot path holds no repository."""
    snapshot = tmp_path / "golden-repos" / ".versioned" / "agent-router" / "v_1"
    backend = _Recording(_git_mcp(_Elsewhere(snapshot, held)))

    def no_git(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"local git spawned: {args[0] if args else kwargs}")

    monkeypatch.setattr(
        tss, "server_history_backend", lambda root: (backend, str(snapshot))
    )
    monkeypatch.setattr(tss, "subprocess", _Subprocess(no_git))
    return backend


def _local_timestamp(held: Path, ref: str) -> Any:
    try:
        return tss.resolve_commit_timestamp(held, ref)
    except ValueError as exc:
        return str(exc)


def test_at_commit_resolves_identically_through_the_seam(
    held: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tss, "server_history_backend", lambda root: None)
    head = _git(held, "rev-parse", "HEAD")
    refs = [head, head[:8], "master", "feature", "HEAD~3", "deadbeef", "no-such-ref"]
    local = {ref: _local_timestamp(held, ref) for ref in refs}
    assert isinstance(local[head], int) and "does not resolve" in local["deadbeef"]

    seamed = {
        ref: _local_timestamp_via(LocalGitBackend(), str(held), ref) for ref in refs
    }

    assert seamed == local


def _local_timestamp_via(backend: Any, repo: str, ref: str) -> Any:
    try:
        return seam.commit_timestamp(backend, repo, ref)
    except ValueError as exc:
        return str(exc)


def test_at_commit_on_a_delegated_server_asks_the_seam(
    held: Path, server_seam: _Recording
) -> None:
    head = _git(held, "rev-parse", "HEAD")
    expected = int(_git(held, "show", "-s", "--format=%ct", head))

    assert tss.resolve_commit_timestamp(Path("/nowhere"), head) == expected
    assert [name for name, _ in server_seam.calls] == ["rev_parse", "log_raw"]


def test_a_transport_failure_is_not_read_as_no_such_commit() -> None:
    class Down:
        def rev_parse(self, *args: Any, **kwargs: Any) -> str:
            raise subprocess.CalledProcessError(
                1, "git-mcp", stderr="connection refused"
            )

    with pytest.raises(ValueError, match="could not be resolved: .*git-mcp"):
        seam.commit_timestamp(Down(), "/repo", "c0ffee1")


def _pointer_payloads(held: Path) -> List[Dict[str, Any]]:
    log = [
        line.split()
        for line in _git(held, "log", "--reverse", "--format=%H %P").splitlines()
    ]
    first, second, third = log[0][0], log[1][0], log[2][0]
    added = [
        {"diff_type": "added", "path": p, "commit_hash": first}
        for p in ("src/a.py", "src/b.py", "logo.png", "README.md")
    ]
    return [
        {"reconstruct_from_git": True, **payload}
        for payload in [
            *added,
            {"diff_type": "added", "file_path": "src/b.py", "commit_hash": second},
            {"diff_type": "added", "path": "src/a.py", "commit_hash": second},
            {"diff_type": "deleted", "path": "README.md", "parent_commit_hash": second},
            {"diff_type": "deleted", "path": "README.md", "commit_hash": third},
            {"diff_type": "added", "path": "src/missing.py", "commit_hash": first},
            {"diff_type": "modified", "path": "src/a.py", "commit_hash": first},
        ]
    ]


def _service(root: Path) -> tss.TemporalSearchService:
    return tss.TemporalSearchService(config_manager=MagicMock(), project_root=root)


def test_pointer_content_reconstructs_identically_in_one_call(
    held: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payloads = _pointer_payloads(held)
    monkeypatch.setattr(tss, "server_history_backend", lambda root: None)
    local = [_service(held)._reconstruct_temporal_content(p) for p in payloads]
    assert "\ufffd" in local[4] and local[5] == "def a():\n    return 10\n", local

    snapshot = tmp_path / "snapshot"
    backend = _Recording(_git_mcp(_Elsewhere(snapshot, held)))
    monkeypatch.setattr(
        tss, "server_history_backend", lambda root: (backend, str(snapshot))
    )
    monkeypatch.setattr(
        tss, "subprocess", _Subprocess(lambda *a, **k: pytest.fail("local git"))
    )
    service = _service(snapshot)
    prefetched = service._prefetch_reconstructions(payloads)
    seamed = [service._reconstruct_temporal_content(p, prefetched) for p in payloads]

    assert seamed == local
    assert [name for name, _ in backend.calls] == ["show_objects"]


def test_a_query_reads_every_pointer_in_one_call(
    held: Path, server_seam: _Recording
) -> None:
    payloads = _pointer_payloads(held)
    results = [{"payload": dict(p, commit_timestamp=0), "score": 0.9} for p in payloads]

    filtered, _ = _service(Path("/nowhere"))._filter_by_time_range(
        results, "1970-01-01", "2100-12-31"
    )

    assert len(filtered) == len(payloads)
    assert [name for name, _ in server_seam.calls] == ["show_objects"]
    assert filtered[0].content == "def a():\n    return 1\n"


# ── the server's seam ───────────────────────────────────────────────────────


def test_only_a_running_server_with_delegated_git_has_a_history_seam(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    from code_indexer.server.utils import registry_factory

    def state(git_backend: Any) -> None:
        monkeypatch.setattr(
            registry_factory,
            "_running_server_app_state",
            lambda: SimpleNamespace(git_backend=git_backend),
        )

    monkeypatch.setattr(registry_factory, "_running_server_app_state", lambda: None)
    assert seam.server_history_backend(tmp_path) is None
    state(LocalGitBackend())
    assert seam.server_history_backend(tmp_path) is None
    delegated = _Recording(_git_mcp(_Elsewhere(tmp_path, tmp_path)))
    state(delegated)
    assert seam.server_history_backend(tmp_path) == (delegated, str(tmp_path))
