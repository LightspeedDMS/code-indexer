"""Semantic search finds the index on a pod that does not hold the repository.

Found on the mount-less EKS pod: ``search_code`` returned nothing, from MCP and
from ``cidx query`` alike. The query was not at fault -- the index was empty.
Refresh runs ``cidx index --reconcile`` through the content and git seams, and
reconcile asked LOCAL git whether the directory was a repository. On a pod with
no ``.git`` the answer was no, so:

* every file's content id came from a local ``stat`` that failed, never matching
  the blob-hash id its chunks were indexed under -- every file read as modified;
* the non-git branch deleted every "modified" file's chunks before re-indexing;
* the re-index then dropped every file because ``Path.exists()`` is False on a
  pod without the files -- 0 processed, and an index of nothing.

Pinned against that shape: a real repository held elsewhere, an empty directory
here, and git and content backends that answer for one through the other.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pytest
from click.testing import CliRunner

from code_indexer.indexing.repo_manifest import RepoManifest
from code_indexer.server.storage.shared.content_backend import (
    ContentEntry,
    ContentPage,
    LocalContentBackend,
)
from code_indexer.server.storage.shared.git_backend import LocalGitBackend
from code_indexer.services.embedding_provider import (
    BatchEmbeddingResult,
    EmbeddingProvider,
    EmbeddingResult,
)
from code_indexer.services.git_topology_service import GitTopologyService

QUERY = "route an incoming request to an agent"
FILES = {f"src/mod{i}.py": i for i in (1, 2, 3)}
SHA = "c0ffee" + "0" * 34
VECTOR_DIM = 1024


# ── the pod's shape ─────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class _Elsewhere:
    """Maps the empty directory on this pod to the repository git-mcp holds."""

    def __init__(self, here: Path, there: Path) -> None:
        self.here, self.there = str(here), str(there)

    def __call__(self, path: Any) -> Path:
        text = str(path)
        if text.startswith(self.here):
            text = self.there + text[len(self.here) :]
        return Path(text)


def _git_mcp(elsewhere: _Elsewhere) -> LocalGitBackend:
    """Real git, run against the repository held elsewhere (git-mcp's view)."""

    class GitMcp(LocalGitBackend):
        @property
        def owns_credentials(self) -> bool:
            return True

        @staticmethod
        def _run(cmd: Sequence[str], cwd: Path, **kw: Any) -> Any:
            return LocalGitBackend._run(cmd, elsewhere(cwd), **kw)

    return GitMcp()


class _WorkspaceMcp(LocalContentBackend):
    """The repository's files, served from elsewhere (workspace-mcp's view)."""

    def __init__(self, elsewhere: _Elsewhere) -> None:
        self._elsewhere = elsewhere

    @property
    def is_local(self) -> bool:
        return False

    def manifest(self, repo_path: Any, **kw: Any) -> ContentPage:
        return super().manifest(self._elsewhere(repo_path), **kw)

    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Any:
        return super().fetch(self._elsewhere(repo_path), paths)


class _Embedder(EmbeddingProvider):
    """Deterministic vectors; no network."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(None)

    @staticmethod
    def _vector(text: str) -> List[float]:
        seed = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
        vec = np.random.default_rng(seed).random(VECTOR_DIM).astype(np.float32)
        return [float(x) for x in vec / np.linalg.norm(vec)]

    def get_embedding(
        self,
        text: str,
        model: Optional[str] = None,
        embedding_purpose: Optional[str] = None,
    ) -> List[float]:
        return self._vector(text)

    def get_embeddings_batch(
        self, texts: List[str], model: Optional[str] = None, **kw: Any
    ) -> List[List[float]]:
        return [self._vector(t) for t in texts]

    def get_embedding_with_metadata(
        self, text: str, model: Optional[str] = None, **kw: Any
    ) -> EmbeddingResult:
        return EmbeddingResult(
            embedding=self._vector(text),
            model="voyage-code-3",
            tokens_used=1,
            provider="voyage-ai",
        )

    def get_embeddings_batch_with_metadata(
        self, texts: List[str], model: Optional[str] = None, **kw: Any
    ) -> BatchEmbeddingResult:
        return BatchEmbeddingResult(
            embeddings=[self._vector(t) for t in texts],
            model="voyage-code-3",
            total_tokens_used=len(texts),
            provider="voyage-ai",
        )

    def health_check(self, *, test_api: bool = False) -> bool:
        return True

    def get_model_info(self) -> Dict[str, Any]:
        return {
            "name": "voyage-code-3",
            "provider": "voyage-ai",
            "dimensions": VECTOR_DIM,
        }

    def get_provider_name(self) -> str:
        return "voyage-ai"

    def get_current_model(self) -> str:
        return "voyage-code-3"

    def supports_batch_processing(self) -> bool:
        return True

    def _get_model_token_limit(self) -> int:
        return 120000

    def count_tokens(self, text: str) -> int:
        return len(text) // 4


@pytest.fixture
def pod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """A repository held elsewhere, and this pod's empty directory for it."""
    held = tmp_path / "git-mcp" / "agent-router"
    (held / "src").mkdir(parents=True)
    for rel, n in FILES.items():
        (held / rel).write_text(
            f'def route_request_{n}(req):\n    """{QUERY} number {n}."""\n'
            f"    return dispatch(req, agent={n})\n"
        )
    _git(held, "init", "-q", "-b", "master")
    _git(held, "add", ".")
    _git(held, "commit", "-qm", "init")

    here = tmp_path / "golden-repos" / "agent-router"
    here.mkdir(parents=True)
    elsewhere = _Elsewhere(here, held)

    from code_indexer import cli as cli_module
    from code_indexer.services import embedding_factory

    monkeypatch.setattr(
        embedding_factory.EmbeddingProviderFactory,
        "create",
        staticmethod(lambda *a, **k: _Embedder()),
    )
    monkeypatch.setattr(
        cli_module, "_build_index_git_backend", lambda name: _git_mcp(elsewhere)
    )
    monkeypatch.setattr(
        cli_module,
        "_build_index_content_backend",
        lambda name: _WorkspaceMcp(elsewhere),
    )
    monkeypatch.setenv("VOYAGE_API_KEY", "not-used")
    monkeypatch.chdir(here)
    return {"here": here, "held": held, "cli": cli_module.cli}


def _cidx(pod: Dict[str, Any], *args: str) -> str:
    result = CliRunner().invoke(pod["cli"], list(args), catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return str(result.output)


_SEAMS = ("--content-backend=delegated", "--git-backend=delegated")
_LAYOUT = "--new-collection-layout=chunks_db"


def _index(pod: Dict[str, Any], *extra: str) -> str:
    return _cidx(pod, "index", _LAYOUT, *_SEAMS, *extra)


def _refresh(pod: Dict[str, Any]) -> str:
    """What the refresh scheduler runs on a delegated pod."""
    return _index(pod, "--reconcile", "--ignore-resume-state")


def _search(pod: Dict[str, Any]) -> List[str]:
    """The server's search path: the vector store, queried from this pod."""
    from code_indexer.config import ConfigManager
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    config = ConfigManager.create_with_backtrack(pod["here"]).get_config()
    store = FilesystemVectorStore(
        pod["here"] / ".code-indexer" / "index", project_root=pod["here"]
    )
    embedder = _Embedder()
    results = store.search(
        query=QUERY,
        embedding_provider=embedder,
        collection_name=store.resolve_collection_name(config, embedder),
        limit=10,
    )
    assert isinstance(results, list)
    return sorted(r["payload"]["path"] for r in results)


# ── end to end ──────────────────────────────────────────────────────────────


def test_refresh_keeps_the_index_a_query_from_this_pod_reads(
    pod: Dict[str, Any],
) -> None:
    """The EKS failure: refresh left an empty index, so every search was empty."""
    _cidx(pod, "init", "--embedding-provider", "voyage-ai")
    assert not (pod["here"] / ".git").exists() and not (pod["here"] / "src").exists()
    _index(pod)
    assert _search(pod) == sorted(FILES), "the index pass itself must find the files"

    output = _refresh(pod)

    assert "up-to-date" in output, (
        f"an unchanged repository reconciled as changed:\n{output}"
    )
    assert _search(pod) == sorted(FILES)
    query_output = _cidx(pod, "query", QUERY, "--limit", "3")
    assert "No results found" not in query_output
    for rel in FILES:
        assert rel in query_output


def test_refresh_through_the_seams_reindexes_a_change_upstream(
    pod: Dict[str, Any],
) -> None:
    """A modified file is re-indexed from the content backend, not dropped."""
    _cidx(pod, "init", "--embedding-provider", "voyage-ai")
    _index(pod)
    with open(pod["held"] / "src/mod1.py", "a") as f:
        f.write("# routed elsewhere now\n")
    _git(pod["held"], "commit", "-qam", "change")

    output = _refresh(pod)

    assert "Files processed: 1" in output, output
    assert _search(pod) == sorted(FILES)


# ── the topology service asks the backend ───────────────────────────────────


class _Head:
    """A git backend answering only HEAD questions."""

    def __init__(self, branch: Optional[str] = "master", stderr: str = "") -> None:
        self.branch, self.stderr = branch, stderr

    def rev_parse(self, repo_path: str, *args: str, **kw: Any) -> str:
        if self.stderr:
            raise subprocess.CalledProcessError(128, "git-mcp", stderr=self.stderr)
        return SHA[:7] if "--short" in args else SHA

    def symbolic_ref(self, repo_path: str, short: bool = True, **kw: Any) -> str:
        if self.branch is None:
            raise subprocess.CalledProcessError(
                1, "git-mcp", stderr="connection refused"
            )
        return self.branch


def test_the_backend_makes_an_empty_directory_a_repository(tmp_path: Path) -> None:
    topology = GitTopologyService(tmp_path, git_backend=_Head())
    assert topology.is_git_available() is True
    assert topology.get_current_branch() == "master"
    assert topology._get_current_commit() == SHA


def test_the_backends_not_a_repository_is_believed(tmp_path: Path) -> None:
    topology = GitTopologyService(
        tmp_path, git_backend=_Head(stderr="fatal: not a git repository")
    )
    assert topology.is_git_available() is False
    assert topology.get_current_branch() is None


def test_a_failed_branch_read_is_no_branch_not_unknown(tmp_path: Path) -> None:
    """ "unknown" is not a branch; a caller would key visibility on it."""
    topology = GitTopologyService(tmp_path, git_backend=_Head(branch=None))
    assert topology.get_current_branch() is None


def test_a_failed_commit_read_is_no_commit(tmp_path: Path) -> None:
    topology = GitTopologyService(tmp_path, git_backend=_Head())
    topology._git_backend = _Head(stderr="git-mcp request failed: timeout")
    assert topology._get_current_commit() is None


def test_without_a_backend_the_local_probe_still_decides(tmp_path: Path) -> None:
    topology = GitTopologyService(tmp_path)
    assert topology.is_git_available() is False
    assert topology.get_current_branch() is None


# ── the manifest stands in for the local checkout ───────────────────────────


class _Listing:
    is_local = False

    def __init__(self, entries: Sequence[ContentEntry]) -> None:
        self.entries = tuple(entries)

    def manifest(self, repo_path: Any, **kw: Any) -> ContentPage:
        return ContentPage(entries=self.entries, next_cursor=None)

    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Any:
        raise AssertionError("membership and hashes must not fetch bytes")


ROOT = Path("/nonexistent/golden-repos/agent-router")
_ENTRIES = (
    ContentEntry(path="src/a.py", size=1, blob_sha="a" * 40, is_symlink=False),
    ContentEntry(path="src/link.py", size=1, blob_sha=None, is_symlink=True),
    ContentEntry(path="src/empty.py", size=0, blob_sha="e" * 40, is_symlink=False),
)


def _manifest() -> RepoManifest:
    return RepoManifest.build(ROOT, backend=_Listing(_ENTRIES))


def test_blob_hashes_are_the_ones_the_manifest_vouches_for() -> None:
    assert _manifest().blob_hashes() == {"src/a.py": "a" * 40, "src/empty.py": "e" * 40}


@pytest.mark.parametrize(
    "path", ["src/a.py", str(ROOT / "src/a.py"), ROOT / "src/empty.py", "src/link.py"]
)
def test_a_listed_path_is_present(path: Any) -> None:
    assert path in _manifest()


@pytest.mark.parametrize(
    "path", ["src/gone.py", "/elsewhere/src/a.py", "../src/a.py", 42]
)
def test_an_unlisted_path_is_not(path: Any) -> None:
    assert path not in _manifest()


def _indexer(manifest: Optional[RepoManifest], git_backend: Any = None) -> Any:
    from code_indexer.services.smart_indexer import SmartIndexer

    indexer = SmartIndexer.__new__(SmartIndexer)
    indexer.repo_manifest = manifest
    indexer.git_backend = git_backend
    return indexer


def test_a_pass_with_a_manifest_answers_existence_from_it() -> None:
    indexer = _indexer(_manifest())
    assert indexer._exists_in_this_pass(ROOT / "src/a.py") is True
    assert indexer._exists_in_this_pass(ROOT / "src/gone.py") is False


def test_a_pass_without_one_asks_the_disk(tmp_path: Path) -> None:
    (tmp_path / "here.py").write_text("x")
    indexer = _indexer(None)
    assert indexer._exists_in_this_pass(tmp_path / "here.py") is True
    assert indexer._exists_in_this_pass(tmp_path / "gone.py") is False


def _dirty_checkout(root: Path) -> Any:
    """A local repository with one modified file, and an indexer pointed at it."""
    from code_indexer.config import Config

    _git(root, "init", "-q", "-b", "master")
    (root / "a.py").write_text("x\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "x")
    (root / "a.py").write_text("y\n")
    indexer = _indexer(None)
    indexer.config = Config(codebase_dir=root)
    return indexer


def test_reconcile_takes_head_hashes_from_the_manifest(tmp_path: Path) -> None:
    """Even where local git could answer, a pass's manifest is the answer.

    Its hashes are of the working tree the content backend serves, so local
    git is not asked -- and its "modified" list cannot route a file away from
    the blob-hash id the chunks were indexed under.
    """
    indexer = _dirty_checkout(tmp_path)
    indexer.repo_manifest = _manifest()
    assert indexer._get_head_blob_hash_map() == _manifest().blob_hashes()
    assert indexer._get_modified_files_set() == set()


def test_reconcile_without_a_manifest_still_asks_local_git(tmp_path: Path) -> None:
    indexer = _dirty_checkout(tmp_path)

    assert indexer._get_head_blob_hash_map() == {
        "a.py": _git(tmp_path, "rev-parse", "HEAD:a.py")
    }
    assert indexer._get_modified_files_set() == {"a.py"}


def test_a_branch_change_upstream_is_indexed_through_the_seams(
    pod: Dict[str, Any],
) -> None:
    """The graph-optimized branch path diffs with LOCAL git; a backend skips it."""
    _cidx(pod, "init", "--embedding-provider", "voyage-ai")
    _index(pod)
    _git(pod["held"], "checkout", "-q", "-b", "feature")
    (pod["held"] / "src/mod4.py").write_text(f'"""{QUERY} on feature."""\n')
    _git(pod["held"], "add", ".")
    _git(pod["held"], "commit", "-qm", "feature")

    _index(pod)

    assert _search(pod) == sorted([*FILES, "src/mod4.py"])
